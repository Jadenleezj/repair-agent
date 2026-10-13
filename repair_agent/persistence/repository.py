from __future__ import annotations

import logging

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from repair_agent.domain.statuses import OrderStatus, DeliveryTaskStatus


# 返回 UTC 时区的 ISO 时间字符串，用于持久化事件和审计时间。
def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    """Business/audit persistence; LangGraph checkpoints use separate tables."""

    # 初始化业务数据库、兼容迁移与演示数据，并建立线程锁。
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        from repair_agent.persistence.migrations import migrate_order_domain
        migrate_order_domain(self.conn)
        self._setup()
        self._migrate_legacy_schema()
        self.seed_demo_data()

    # 创建业务、审批、事件、幂等执行及演示数据表；启用 WAL。
    def _setup(self) -> None:
        with self.lock, self.conn:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS repair_cases (
                    case_id TEXT PRIMARY KEY,
                    requester_id TEXT NOT NULL,
                    raw_request TEXT NOT NULL,
                    order_id TEXT,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    comment TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demo_orders (
                    order_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    projection_status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demo_delivery_tasks (
                    order_id TEXT PRIMARY KEY,
                    task_status TEXT NOT NULL,
                    courier_id TEXT NOT NULL,
                    task_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demo_operation_events (
                    order_id TEXT PRIMARY KEY,
                    availability TEXT NOT NULL,
                    records_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_executions (
                    idempotency_key TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    playbook_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    # 迁移旧演示状态，保留已有工单与执行记录。
    def _migrate_legacy_schema(self) -> None:
        with self.lock, self.conn:
            # 旧演示状态迁移只影响模拟业务表，保留工单、审批和执行记录。
            task_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(demo_delivery_tasks)")}
            if "task_id" not in task_columns:
                self.conn.execute("ALTER TABLE demo_delivery_tasks ADD COLUMN task_id TEXT")
            self.conn.execute("UPDATE demo_delivery_tasks SET task_id='DT-' || order_id WHERE task_id IS NULL")
            self.conn.execute("UPDATE demo_orders SET status='CREATE' WHERE status IN ('CREATED','ASSIGNED')")
            self.conn.execute("UPDATE demo_orders SET projection_status='CREATE' WHERE projection_status IN ('CREATED','ASSIGNED')")
            self.conn.execute("UPDATE demo_orders SET status='DELIVERING' WHERE status='PICKED_UP'")
            self.conn.execute("UPDATE demo_orders SET projection_status='DELIVERING' WHERE projection_status='PICKED_UP'")
            self.conn.execute("UPDATE demo_delivery_tasks SET task_status='COMPLETE' WHERE task_status='DELIVERED'")
            self.conn.execute("UPDATE demo_delivery_tasks SET task_status='ASSIGNED' WHERE task_status='DELIVERING'")
            self.conn.execute("UPDATE demo_delivery_tasks SET task_status='CREATE' WHERE task_status='CREATED'")
            for row in self.conn.execute("SELECT order_id,records_json FROM demo_operation_events").fetchall():
                records = json.loads(row[1])
                if any(item.get('event') == 'PICKUP_SCAN' for item in records):
                    for item in records:
                        if item.get('event') == 'PICKUP_SCAN': item['event'] = 'TASK_ACCEPTED'
                    self.conn.execute("UPDATE demo_operation_events SET records_json=? WHERE order_id=?",
                                      (json.dumps(records,ensure_ascii=False),row[0]))

    # 仅插入缺失的演示数据，不覆盖已修复的数据；包含成功、缺证据和误报场景。
    def seed_demo_data(self) -> None:
        orders = [
            ("W1001", OrderStatus.DELIVERING, 7, OrderStatus.DELIVERING),
            ("W1002", OrderStatus.DELIVERING, 3, OrderStatus.DELIVERING),
            ("W1003", OrderStatus.DELIVERED, 11, OrderStatus.DELIVERED),
            ("W2001", OrderStatus.CREATE, 4, OrderStatus.CREATE),
            ("W3001", OrderStatus.DELIVERED, 9, OrderStatus.DELIVERING),
        ]
        tasks = [
            ("W1001", DeliveryTaskStatus.COMPLETE, "COURIER-18", "DT-W1001"),
            ("W1002", DeliveryTaskStatus.COMPLETE, "COURIER-23", "DT-W1002"),
            ("W1003", DeliveryTaskStatus.COMPLETE, "COURIER-08", "DT-W1003"),
            ("W2001", DeliveryTaskStatus.ASSIGNED, "COURIER-12", "DT-W2001"),
            ("W3001", DeliveryTaskStatus.COMPLETE, "COURIER-05", "DT-W3001"),
        ]
        events = [
            ("W1001", "AVAILABLE", json.dumps([
                {"event": "PROOF_OF_DELIVERY_SCAN", "result": "SUCCESS", "operator": "COURIER-18"}
            ])),
            ("W1002", "UNAVAILABLE", "[]"),
            ("W1003", "AVAILABLE", json.dumps([
                {"event": "PROOF_OF_DELIVERY_SCAN", "result": "SUCCESS", "operator": "COURIER-08"}
            ])),
            ("W2001", "AVAILABLE", json.dumps([
                {"event": "TASK_ACCEPTED", "result": "SUCCESS", "operator": "COURIER-12"}
            ])),
            ("W3001", "AVAILABLE", "[]"),
        ]
        with self.lock, self.conn:
            # 旧订单库保留 amount、fulfillment_created 两个必填字段。
            # 使用列名写入并为新增模拟订单提供兼容值，已有订单保持原样。
            columns = ['order_id','status','version','projection_status']
            existing = {row[1] for row in self.conn.execute('PRAGMA table_info(demo_orders)')}
            extras = [name for name in ['amount','fulfillment_created'] if name in existing]
            columns.extend(extras)
            seeded_orders = [(*order, *([0] * len(extras))) for order in orders]
            self.conn.executemany(
                f"INSERT OR IGNORE INTO demo_orders({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                seeded_orders,
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO demo_delivery_tasks(order_id,task_status,courier_id,task_id) VALUES (?, ?, ?, ?)", tasks
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO demo_operation_events(order_id,availability,records_json) VALUES (?, ?, ?)", events
            )

    # 重置演示业务数据与执行幂等记录；不删除工单、审批和检查点。
    def reset_demo_data(self) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM demo_orders")
            self.conn.execute("DELETE FROM demo_delivery_tasks")
            self.conn.execute("DELETE FROM demo_operation_events")
            self.conn.execute("DELETE FROM repair_executions")
        self.seed_demo_data()

    # 保存原始请求及初始状态，并追加工单创建事件。
    def create_case(self, case_id: str, requester_id: str, raw_request: str) -> None:
        now = utc_now()
        state = {"case_id": case_id, "requester_id": requester_id, "raw_request": raw_request}
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO repair_cases VALUES (?, ?, ?, NULL, 'CREATED', ?, ?, ?)",
                (case_id, requester_id, raw_request, json.dumps(state), now, now),
            )
        self.add_event(case_id, "CASE_CREATED", {"requester_id": requester_id})

    # 将图状态序列化为 JSON，更新工单状态、订单号和更新时间。
    def save_case_state(self, case_id: str, state: dict[str, Any]) -> None:
        serializable = json.loads(json.dumps(state, ensure_ascii=False, default=str))
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE repair_cases SET order_id=?,status=?,state_json=?,updated_at=? WHERE case_id=?",
                (state.get("order_id"), state.get("status", "UNKNOWN"),
                 json.dumps(serializable, ensure_ascii=False), utc_now(), case_id),
            )

    # 按 ID 查询工单并反序列化状态，不存在时返回 None。
    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM repair_cases WHERE case_id=?", (case_id,)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["state"] = json.loads(value.pop("state_json"))
        return value

    # 返回最近创建的工单摘要，按 limit 限制数量。
    def list_cases(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT case_id,order_id,status,requester_id,created_at,updated_at "
                "FROM repair_cases ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # 追加带时间戳的业务事件，将事件载荷保存为 JSON。
    def add_event(self, case_id: str, event_type: str, payload: dict[str, Any]) -> None:
        logging.getLogger(__name__).info('audit event=%s', event_type, extra={'event_fields': {'case_id': case_id}})
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO case_events(case_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (case_id, event_type, json.dumps(payload, ensure_ascii=False, default=str), utc_now()),
            )

    # 记录审批人、结论和备注，并追加审批事件；此方法不负责执行修复。
    def add_approval(self, case_id: str, actor_id: str, approved: bool, comment: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO approvals(case_id,actor_id,approved,comment,created_at) VALUES(?,?,?,?,?)",
                (case_id, actor_id, int(approved), comment, utc_now()),
            )
        self.add_event(case_id, "APPROVAL_DECIDED", {
            "actor_id": actor_id, "approved": approved, "comment": comment
        })

    # 按事件顺序读取工单业务时间线；节点细节另见 state.audit。
    def timeline(self, case_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT event_type,payload_json,created_at FROM case_events "
                "WHERE case_id=? ORDER BY id", (case_id,)
            ).fetchall()
        return [{"event_type": r["event_type"], "payload": json.loads(r["payload_json"]),
                 "created_at": r["created_at"]} for r in rows]

    # 增加订单版本以模拟审批后数据变化，返回是否找到并更新记录。
    def bump_order_version(self, order_id: str) -> bool:
        """Fault injection used to demonstrate stale approval/TOCTOU protection."""
        with self.lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE demo_orders SET version=version+1 WHERE order_id=?", (order_id,)
            )
        return cursor.rowcount == 1
