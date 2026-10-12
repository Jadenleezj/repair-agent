from __future__ import annotations

import json
import re
from typing import Any

from .backend_client import RepairBackendClient
from .platform_tools import MockPlatformTools, PLATFORM_TOOLS
from .models import Evidence
from .repository import Repository


WAYBILL_PATTERN = re.compile(r"\bW\d{4,}\b", re.IGNORECASE)


# 从文本提取 W 开头的运单号；统一转大写，未匹配时返回 None。
def extract_waybill_id(text: str) -> str | None:
    match = WAYBILL_PATTERN.search(text)
    return match.group(0).upper() if match else None


class ToolService:
    """Read-only dispatch tools and allow-listed state-repair tools."""

    # 注入业务仓储，供只读查询和受控修复共用。
    def __init__(self, repository: Repository, backend: RepairBackendClient | None = None):
        self.guard = None
        self.artifacts = None
        self.repo = repository
        self.backend = backend or RepairBackendClient.local(repository)
        self.platforms = MockPlatformTools(repository)

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
        from .reliability import DependencyUnavailable, RunStopped
        try:
            operation=lambda: self._read_unbounded(tool,waybill_id)
            # 业务读取的 HTTP 重试已在客户端实现，避免叠加重试次数。
            result=self.guard.call(tool,operation,0) if self.guard else operation()
        except RunStopped:raise
        except Exception as exc:
            if tool not in PLATFORM_TOOLS:raise
            result=self._evidence(tool,PLATFORM_TOOLS[tool][1],
                {'error':type(exc).__name__,'message':str(exc)[:300]},'UNAVAILABLE',False)
        if self.artifacts:
            bounded=self.artifacts.bound(result)
            if bounded is not result and self.guard:self.guard.metrics.increment('tools.oversized')
            return bounded
        return result

    # 读取平台或业务后端的原始证据，容量与输出限制由上层统一处理。
    def _read_unbounded(self, tool: str, waybill_id: str) -> Evidence:
        if tool == "QUERY_ONLINE_DATA":
            waybill = self.backend.read("GET_WAYBILL", waybill_id)
            task = self.backend.read("GET_DELIVERY_TASK", waybill_id)
            return self._evidence(tool, "ONLINE_DATA_SNAPSHOT",
                {"waybill": waybill["value"], "delivery_task": task["value"],
                 "environment": "configured-repair-backend"},
                waybill["status"], waybill["status"] == "FOUND")
        if tool in PLATFORM_TOOLS:
            return self.platforms.query(tool, waybill_id)
        return self.backend.read(tool, waybill_id)

    # 通过只读工具取得运单快照；查询失败或不存在返回 None。
    def get_waybill(self, waybill_id: str) -> dict[str, Any] | None:
        evidence = self.call_read_tool("GET_WAYBILL", waybill_id)
        return evidence["value"] if evidence["status"] == "FOUND" else None

    # 将白名单方案映射为固定后端接口，传递版本和幂等键；不直接修改业务数据库。
    def execute_playbook(
        self, *, case_id: str, waybill_id: str, playbook_id: str,
        expected_version: int, idempotency_key: str,
    ) -> dict[str, Any]:
        return self.backend.execute(
            case_id=case_id, waybill_id=waybill_id, playbook_id=playbook_id,
            expected_version=expected_version, idempotency_key=idempotency_key,
        )

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

