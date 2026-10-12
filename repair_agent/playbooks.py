from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True)
class Playbook:
    playbook_id: str
    name: str
    issue_type: str
    action: str
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    auto_execute_allowed: bool
    required_evidence: tuple[str, ...]
    success_rate: float
    max_affected_waybills: int = 1

    # 把 Playbook 元数据转换为字典，供 API 展示。
    def to_dict(self) -> dict:
        value = asdict(self)
        value["required_evidence"] = list(self.required_evidence)
        # 对外展示固定修复接口元数据，调用地址不会由模型生成。
        from .backend_client import REPAIR_ENDPOINTS
        value["repair_api"] = {"method": "POST", "path": REPAIR_ENDPOINTS[self.playbook_id]}
        return value


PLAYBOOKS: dict[str, Playbook] = {
    "APPLY_DELIVERY_COMPLETION_V1": Playbook(
        "APPLY_DELIVERY_COMPLETION_V1", "根据签收作业补齐运单送达状态",
        "DELIVERY_TASK_WAYBILL_MISMATCH", "APPLY_DELIVERY_COMPLETION",
        "MEDIUM", False,
        ("WAYBILL_SNAPSHOT", "DELIVERY_TASK", "OPERATION_EVENTS"), 0.98,
    ),
    "APPLY_PICKUP_SCAN_V1": Playbook(
        "APPLY_PICKUP_SCAN_V1", "根据有效揽收作业补齐派送中状态",
        "PICKUP_SCAN_NOT_APPLIED", "APPLY_PICKUP_SCAN",
        "HIGH", False,
        ("WAYBILL_SNAPSHOT", "DELIVERY_TASK", "OPERATION_EVENTS"), 0.94,
    ),
    "REBUILD_DISPATCH_PROJECTION_V1": Playbook(
        "REBUILD_DISPATCH_PROJECTION_V1", "重建派送看板读模型",
        "DISPATCH_PROJECTION_STALE", "REBUILD_DISPATCH_PROJECTION",
        "LOW", True,
        ("WAYBILL_SNAPSHOT", "DISPATCH_PROJECTION"), 0.995,
    ),
}


# 查找白名单修复方案；未知或空 ID 返回 None。
def get_playbook(playbook_id: str | None) -> Playbook | None:
    return PLAYBOOKS.get(playbook_id or "")
