from __future__ import annotations

import json
import re
from typing import Any

from .models import Evidence
from .playbooks import get_playbook
from .repository import Repository, utc_now


WAYBILL_PATTERN = re.compile(r"\bW\d{4,}\b", re.IGNORECASE)


# 从文本提取 W 开头的运单号；统一转大写，未匹配时返回 None。
def extract_waybill_id(text: str) -> str | None:
    match = WAYBILL_PATTERN.search(text)
    return match.group(0).upper() if match else None


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

    # 按白名单工具名称读取运单、任务、事件或看板，返回结构化证据；未知工具抛错。
    def call_read_tool(self, tool: str, waybill_id: str) -> Evidence:
        with self.repo.lock:
            if tool == "GET_WAYBILL":
                row = self.repo.conn.execute(
                    "SELECT * FROM demo_waybills WHERE waybill_id=?", (waybill_id,)
                ).fetchone()
                return self._evidence(
                    tool, "WAYBILL_SNAPSHOT", dict(row) if row else None,
                    "FOUND" if row else "NOT_FOUND",
                )
            if tool == "GET_DELIVERY_TASK":
                row = self.repo.conn.execute(
                    "SELECT * FROM demo_delivery_tasks WHERE waybill_id=?", (waybill_id,)
                ).fetchone()
                return self._evidence(
                    tool, "DELIVERY_TASK", dict(row) if row else None,
                    "FOUND" if row else "NOT_FOUND",
                )
            if tool == "QUERY_OPERATION_EVENTS":
                row = self.repo.conn.execute(
                    "SELECT * FROM demo_operation_events WHERE waybill_id=?", (waybill_id,)
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
                    "SELECT projection_status FROM demo_waybills WHERE waybill_id=?",
                    (waybill_id,),
                ).fetchone()
                return self._evidence(
                    tool, "DISPATCH_PROJECTION", {"status": row[0]} if row else None,
                    "FOUND" if row else "NOT_FOUND",
                )
        raise ValueError(f"Unknown read tool: {tool}")

    # 通过只读工具取得运单快照；查询失败或不存在返回 None。
    def get_waybill(self, waybill_id: str) -> dict[str, Any] | None:
        evidence = self.call_read_tool("GET_WAYBILL", waybill_id)
        return evidence["value"] if evidence["status"] == "FOUND" else None

    # 执行白名单修复；事务内校验版本与前提，按幂等键复用结果，并保存执行记录。
    def execute_playbook(
        self, *, case_id: str, waybill_id: str, playbook_id: str,
        expected_version: int, idempotency_key: str,
    ) -> dict[str, Any]:
        playbook = get_playbook(playbook_id)
        if playbook is None:
            return {"success": False, "reason": "PLAYBOOK_NOT_ALLOW_LISTED"}

        with self.repo.lock, self.repo.conn:
            previous = self.repo.conn.execute(
                "SELECT result_json,case_id,playbook_id FROM repair_executions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous:
                if previous["case_id"] != case_id or previous["playbook_id"] != playbook_id:
                    return {"success": False, "reason": "IDEMPOTENCY_KEY_CONFLICT"}
                result = json.loads(previous[0])
                binding = result.get("request_binding")
                if binding is not None and binding != {"waybill_id": waybill_id, "expected_version": expected_version}:
                    return {"success": False, "reason": "IDEMPOTENCY_KEY_CONFLICT"}
                result["idempotent_replay"] = True
                return result

            waybill = self.repo.conn.execute(
                "SELECT * FROM demo_waybills WHERE waybill_id=?", (waybill_id,)
            ).fetchone()
            task = self.repo.conn.execute(
                "SELECT * FROM demo_delivery_tasks WHERE waybill_id=?", (waybill_id,)
            ).fetchone()
            if not waybill:
                result = {"success": False, "reason": "WAYBILL_NOT_FOUND"}
            elif waybill["version"] != expected_version:
                result = {"success": False, "reason": "WAYBILL_VERSION_CHANGED"}
            elif playbook.action == "APPLY_DELIVERY_COMPLETION":
                if waybill["status"] != "DELIVERING" or not task or task["task_status"] != "DELIVERED":
                    result = {"success": False, "reason": "PRECONDITION_FAILED"}
                else:
                    self.repo.conn.execute(
                        "UPDATE demo_waybills SET status='DELIVERED',projection_status='DELIVERED',"
                        "version=version+1 WHERE waybill_id=?", (waybill_id,),
                    )
                    result = {"success": True, "action": playbook.action,
                              "new_version": expected_version + 1}
            elif playbook.action == "APPLY_PICKUP_SCAN":
                if waybill["status"] != "ASSIGNED" or not task or task["task_status"] != "DELIVERING":
                    result = {"success": False, "reason": "PRECONDITION_FAILED"}
                else:
                    self.repo.conn.execute(
                        "UPDATE demo_waybills SET status='DELIVERING',projection_status='DELIVERING',"
                        "version=version+1 WHERE waybill_id=?", (waybill_id,),
                    )
                    result = {"success": True, "action": playbook.action,
                              "new_version": expected_version + 1}
            elif playbook.action == "REBUILD_DISPATCH_PROJECTION":
                self.repo.conn.execute(
                    "UPDATE demo_waybills SET projection_status=status,version=version+1 "
                    "WHERE waybill_id=?", (waybill_id,),
                )
                result = {"success": True, "action": playbook.action,
                          "new_version": expected_version + 1}
            else:
                result = {"success": False, "reason": "UNSUPPORTED_ACTION"}

            result["request_binding"] = {"waybill_id": waybill_id, "expected_version": expected_version}
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
    def verify(self, waybill_id: str, playbook_id: str) -> dict[str, Any]:
        waybill = self.get_waybill(waybill_id)
        task_ev = self.call_read_tool("GET_DELIVERY_TASK", waybill_id)
        task = task_ev["value"] if task_ev["status"] == "FOUND" else None
        if waybill is None:
            return {"success": False, "checks": {"waybill_exists": False}}
        if playbook_id == "APPLY_DELIVERY_COMPLETION_V1":
            checks = {
                "waybill_delivered": waybill["status"] == "DELIVERED",
                "task_delivered": bool(task and task["task_status"] == "DELIVERED"),
            }
        elif playbook_id == "APPLY_PICKUP_SCAN_V1":
            checks = {
                "waybill_delivering": waybill["status"] == "DELIVERING",
                "task_delivering": bool(task and task["task_status"] == "DELIVERING"),
            }
        elif playbook_id == "REBUILD_DISPATCH_PROJECTION_V1":
            checks = {"projection_matches_source": waybill["projection_status"] == waybill["status"]}
        else:
            checks = {"known_playbook": False}
        return {"success": all(checks.values()), "checks": checks}

