"""Agent 使用的修复接口客户端：固定端点、结构校验和明确失败结果。"""
from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import Evidence
from .playbooks import get_playbook
from .repository import Repository

REPAIR_ENDPOINTS = {
    'APPLY_DELIVERY_COMPLETION_V1': '/repairs/delivery-completion',
    'APPLY_PICKUP_SCAN_V1': '/repairs/pickup-scan',
    'REBUILD_DISPATCH_PROJECTION_V1': '/repairs/dispatch-projection',
}
READ_TOOLS = {'GET_WAYBILL','GET_DELIVERY_TASK','QUERY_OPERATION_EVENTS','GET_DISPATCH_PROJECTION'}

class RepairCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    case_id: str = Field(min_length=1, max_length=100)
    waybill_id: str = Field(pattern=r'^W[0-9]{4,}$')
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1,max_length=200)

class BackendResult(BaseModel):
    success: bool = Field(strict=True)
    reason: str | None = None
    action: str | None = None
    new_version: int | None = None
    idempotent_replay: bool = False

class RepairBackendClient:
    # 注入 HTTP 客户端，外部后端由环境配置；不会在接口失败时转为本地 SQL 修复。
    def __init__(self, base_url: str, *, api_key: str | None = None, transport=None):
        headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
        self.client = httpx.Client(base_url=base_url.rstrip('/'), headers=headers,
                                   transport=transport, timeout=10, follow_redirects=False)

    # 默认使用 HTTP MockTransport 调用本地模拟后端，便于离线演示相同接口契约。
    @classmethod
    def local(cls, repository: Repository):
        from .mock_backend import MockRepairBackend
        from .backend_api import handle_mock_request
        backend = MockRepairBackend(repository)
        return cls('http://mock-repair-backend', transport=httpx.MockTransport(
            lambda request: handle_mock_request(backend, request)))

    # 向只读接口获取当前事实；外部查询失败返回不可用证据，避免混入本地模拟事实。
    def read(self, tool: str, waybill_id: str) -> Evidence:
        if tool not in READ_TOOLS: raise ValueError(f"Unknown read tool: {tool}")
        try:
            response = self.client.get(f"/evidence/{tool}", params={"waybill_id": waybill_id})
            response.raise_for_status()
            from pydantic import TypeAdapter
            return TypeAdapter(Evidence).validate_python(response.json())
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            facts = {"GET_WAYBILL": "WAYBILL_SNAPSHOT", "GET_DELIVERY_TASK": "DELIVERY_TASK",
                "QUERY_OPERATION_EVENTS": "OPERATION_EVENTS", "GET_DISPATCH_PROJECTION": "DISPATCH_PROJECTION"}
            unauthorized = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}
            return {"evidence_id": f"EV-{tool}", "source": "repair_backend", "fact_type": facts[tool],
                "value": {"error": type(exc).__name__},
                "status": "UNAUTHORIZED" if unauthorized else "UNAVAILABLE", "authoritative": False}

    # 固定方案决定 URL；模型和用户无法指定任意地址、SQL 或更新字段。
    def execute(self, *, case_id: str, waybill_id: str, playbook_id: str,
                expected_version: int, idempotency_key: str) -> dict:
        if not get_playbook(playbook_id) or playbook_id not in REPAIR_ENDPOINTS:
            return {"success": False, "reason": "PLAYBOOK_NOT_ALLOW_LISTED"}
        try:
            command = RepairCommand(case_id=case_id, waybill_id=waybill_id,
                expected_version=expected_version, idempotency_key=idempotency_key)
        except ValidationError:
            return {"success": False, "reason": "INVALID_REPAIR_PARAMETERS"}
        try:
            response = self.client.post(REPAIR_ENDPOINTS[playbook_id], json=command.model_dump(),
                headers={"Idempotency-Key": idempotency_key})
            response.raise_for_status()
            result = BackendResult.model_validate(response.json()).model_dump(exclude_none=True)
            result["endpoint"] = REPAIR_ENDPOINTS[playbook_id]
            return result
        except httpx.TimeoutException:
            return {"success": False, "reason": "BACKEND_OUTCOME_UNKNOWN", "outcome_unknown": True}
        except httpx.HTTPError:
            return {"success": False, "reason": "BACKEND_HTTP_ERROR", "outcome_unknown": True}
        except (ValueError, ValidationError):
            return {"success": False, "reason": "INVALID_BACKEND_RESPONSE", "outcome_unknown": True}

    # 流式限制响应体大小，避免远端返回海量 JSON 在反序列化前耗尽内存。


    # 将底层 HTTP 超时收紧到任务剩余预算；HTTP 超时是分阶段限制，节点后仍会检查总时长。


    # 幂等键查询只读执行记录，查询不到不代表可以重发写操作。


    # 释放 HTTP 连接池，随运行时一起关闭。
    def close(self):
        self.client.close()
