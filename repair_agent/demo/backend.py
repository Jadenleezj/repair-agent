from __future__ import annotations

import json
from typing import Any

from repair_agent.domain.models import Evidence
from repair_agent.domain.playbooks import get_playbook
from repair_agent.persistence.repository import Repository, utc_now
from repair_agent.domain.statuses import OrderStatus, DeliveryTaskStatus, verify_business_state


class MockRepairBackend:
    """模拟业务后端：封装数据查询与修复事务，不属于 Agent 执行层。"""

    # 注入业务仓储，供只读查询和受控修复共用。
    def __init__(self, repository: Repository):
        self.repo = repository

    # 统一封装工具结果，包含证据 ID、事实类型、查询状态及权威性标记。
    def _evidence(
        self, tool: str, fact_type: str, value: Any,
        status: str = "FOUND", authoritative: bool = True,
    ) -> Evidence:
        return {
            "evidence_id": f"EV-{tool}", "source": tool.lower(),
            "fact_type": fact_type, "value": value, "status": status,
            "authoritative": authoritative,
        }  # type: ignore[return-value]

    # 按白名单工具名称读取订单、任务、事件或看板，返回结构化证据；未知工具抛错。
    def call_read_tool(self, tool: str, order_id: str) -> Evidence:
        with self.repo.lock:
            if tool == "GET_ORDER":
                row = self.repo.conn.execute(
                    "SELECT * FROM demo_orders WHERE order_id=?", (order_id,)
                ).fetchone()
                return self._evidence(
                    tool, "ORDER_SNAPSHOT", dict(row) if row else None,
                    "FOUND" if row else "NOT_FOUND",
                )
            if tool == "GET_DELIVERY_TASK":
                row = self.repo.conn.execute(
                    "SELECT * FROM demo_delivery_tasks WHERE order_id=?", (order_id,)
                ).fetchone()
                value = dict(row) if row else None
                if row:
                    # 一个任务可以包含多张订单；Complete 的事实由全部成员状态判断。
                    members = self.repo.conn.execute(
                        "SELECT t.order_id,w.status FROM demo_delivery_tasks t LEFT JOIN demo_orders w "
                        "ON t.order_id=w.order_id WHERE t.task_id=?", (row['task_id'],)
                    ).fetchall()
                    value['member_order_ids'] = [member[0] for member in members]
                    value['all_orders_delivered'] = bool(members) and all(
                        member[1] == OrderStatus.DELIVERED for member in members
                    )
                return self._evidence(
                    tool, "DELIVERY_TASK", value,
                    "FOUND" if row else "NOT_FOUND",
                )
            if tool == "QUERY_OPERATION_EVENTS":
                row = self.repo.conn.execute(
                    "SELECT * FROM demo_operation_events WHERE order_id=?", (order_id,)
                ).fetchone()
                if not row:
                    return self._evidence(tool, "OPERATION_EVENTS", [], "NOT_FOUND", False)
                if row["availability"] != "AVAILABLE":
                    return self._evidence(tool, "OPERATION_EVENTS", [], "UNAVAILABLE", False)
                return self._evidence(
                    tool, "OPERATION_EVENTS", json.loads(row["records_json"]),
                    authoritative=False,
                )
            if tool == "GET_DISPATCH_PROJECTION":
                row = self.repo.conn.execute(
                    "SELECT projection_status FROM demo_orders WHERE order_id=?",
                    (order_id,),
                ).fetchone()
                return self._evidence(
                    tool, "DISPATCH_PROJECTION", {"status": row[0]} if row else None,
                    "FOUND" if row else "NOT_FOUND",
                )
        raise ValueError(f"Unknown read tool: {tool}")

    # 通过只读工具取得订单快照；查询失败或不存在返回 None。
    def get_order(self, order_id: str) -> dict[str, Any] | None:
        evidence = self.call_read_tool("GET_ORDER", order_id)
        return evidence["value"] if evidence["status"] == "FOUND" else None

    # 后端再次核实有效作业记录，不能仅凭 Agent 提出的状态修改请求执行。
    def has_successful_event(self, order_id, event):
        row = self.repo.conn.execute(
            "SELECT availability,records_json FROM demo_operation_events WHERE order_id=?", (order_id,)
        ).fetchone()
        return bool(row and row[0] == 'AVAILABLE' and any(
            item.get('event') == event and item.get('result') == 'SUCCESS'
            for item in json.loads(row[1])
        ))

    # 与单张订单修复共用事务，仅所有成员 Delivered 时将整组任务置为 Complete。
    def complete_task_if_delivered(self, task_id):
        members = self.repo.conn.execute(
            "SELECT w.status FROM demo_delivery_tasks t LEFT JOIN demo_orders w "
            "ON t.order_id=w.order_id WHERE t.task_id=?", (task_id,)
        ).fetchall()
        if members and all(row[0] == OrderStatus.DELIVERED for row in members):
            self.repo.conn.execute("UPDATE demo_delivery_tasks SET task_status=? WHERE task_id=?",
                                   (DeliveryTaskStatus.COMPLETE,task_id))

    # 执行白名单修复；事务内校验版本与前提，按幂等键复用结果，并保存执行记录。
    def execute_playbook(
        self, *, case_id: str, order_id: str, playbook_id: str,
        expected_version: int, idempotency_key: str,
    ) -> dict[str, Any]:
        playbook = get_playbook(playbook_id)
        if playbook is None:
            return {"success": False, "reason": "PLAYBOOK_NOT_ALLOW_LISTED"}

        with self.repo.lock, self.repo.conn:
            # 多个 Worker 使用不同连接，先领取 SQLite 写事务再读取版本和任务成员。
            # 保证前提检查、单张订单修改和任务聚合在同一事务中完成。
            if not self.repo.conn.in_transaction:
                self.repo.conn.execute('BEGIN IMMEDIATE')
            previous = self.repo.conn.execute(
                "SELECT result_json,case_id,playbook_id FROM repair_executions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous:
                if previous["case_id"] != case_id or previous["playbook_id"] != playbook_id:
                    return {"success": False, "reason": "IDEMPOTENCY_KEY_CONFLICT"}
                result = json.loads(previous[0])
                binding = result.get("request_binding")
                if binding is not None and binding != {"order_id": order_id, "expected_version": expected_version}:
                    return {"success": False, "reason": "IDEMPOTENCY_KEY_CONFLICT"}
                result["idempotent_replay"] = True
                return result

            order = self.repo.conn.execute(
                "SELECT * FROM demo_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            task = self.repo.conn.execute(
                "SELECT * FROM demo_delivery_tasks WHERE order_id=?", (order_id,)
            ).fetchone()
            if not order:
                result = {"success": False, "reason": "ORDER_NOT_FOUND"}
            elif order["version"] != expected_version:
                result = {"success": False, "reason": "ORDER_VERSION_CHANGED"}
            elif playbook.action == "APPLY_DELIVERY_COMPLETION":
                # 已称为 Complete 的任务若还有其他未送达成员，不能用单张订单修复掩盖矛盾。
                other_unfinished = self.repo.conn.execute(
                    "SELECT count(*) FROM demo_delivery_tasks t LEFT JOIN demo_orders w ON w.order_id=t.order_id "
                    "WHERE t.task_id=? AND t.order_id<>? AND (w.status IS NULL OR w.status<>?)",
                    (task['task_id'] if task else '',order_id,OrderStatus.DELIVERED)
                ).fetchone()[0]
                if order["status"] != OrderStatus.DELIVERING or not task or task["task_status"] not in {
                    DeliveryTaskStatus.ASSIGNED, DeliveryTaskStatus.COMPLETE
                } or not self.has_successful_event(order_id, 'PROOF_OF_DELIVERY_SCAN') or (
                    task['task_status'] == DeliveryTaskStatus.COMPLETE and other_unfinished
                ):
                    result = {"success": False, "reason": "PRECONDITION_FAILED"}
                else:
                    self.repo.conn.execute(
                        "UPDATE demo_orders SET status=?,projection_status=?,"
                        "version=version+1 WHERE order_id=?", (OrderStatus.DELIVERED,OrderStatus.DELIVERED,order_id),
                    )
                    result = {"success": True, "action": playbook.action,
                              "new_version": expected_version + 1}
                    self.complete_task_if_delivered(task['task_id'])
            elif playbook.action == "APPLY_PICKUP_SCAN":
                if order["status"] != OrderStatus.CREATE or not task or task["task_status"] != DeliveryTaskStatus.ASSIGNED \
                        or not self.has_successful_event(order_id, 'TASK_ACCEPTED'):
                    result = {"success": False, "reason": "PRECONDITION_FAILED"}
                else:
                    self.repo.conn.execute(
                        "UPDATE demo_orders SET status=?,projection_status=?,"
                        "version=version+1 WHERE order_id=?", (OrderStatus.DELIVERING,OrderStatus.DELIVERING,order_id),
                    )
                    result = {"success": True, "action": playbook.action,
                              "new_version": expected_version + 1}
            elif playbook.action == "REBUILD_DISPATCH_PROJECTION":
                self.repo.conn.execute(
                    "UPDATE demo_orders SET projection_status=status,version=version+1 "
                    "WHERE order_id=?", (order_id,),
                )
                result = {"success": True, "action": playbook.action,
                          "new_version": expected_version + 1}
            else:
                result = {"success": False, "reason": "UNSUPPORTED_ACTION"}

            result["request_binding"] = {"order_id": order_id, "expected_version": expected_version}
            self.repo.conn.execute(
                "INSERT INTO repair_executions VALUES(?,?,?,?,?)",
                (idempotency_key, case_id, playbook_id,
                 json.dumps(result, ensure_ascii=False), utc_now()),
            )
            return result

    # 按幂等键返回后端执行记录，供请求响应丢失后的结果核实。
    def execution_status(self,idempotency_key):
        with self.repo.lock:
            row=self.repo.conn.execute('SELECT * FROM repair_executions WHERE idempotency_key=?',(idempotency_key,)).fetchone()
        if not row:return {'status':'NOT_FOUND'}
        return {'status':'FOUND','record':{'case_id':row['case_id'],'playbook_id':row['playbook_id'],
                'result':json.loads(row['result_json'])}}

    # 通过独立只读查询检查修复后的业务条件，返回逐项检查及总体成功标记。
    def verify(self, order_id: str, playbook_id: str) -> dict[str, Any]:
        order = self.get_order(order_id)
        task_ev = self.call_read_tool("GET_DELIVERY_TASK", order_id)
        task = task_ev["value"] if task_ev["status"] == "FOUND" else None
        return verify_business_state(order, task, playbook_id)
