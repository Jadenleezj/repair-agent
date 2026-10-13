"""跨 Agent、工具和 API 共用的业务事实类型。"""
from __future__ import annotations
from typing import Any, Literal, TypedDict

IssueType = Literal[
    "DELIVERY_TASK_ORDER_MISMATCH",
    "PICKUP_SCAN_NOT_APPLIED",
    "DISPATCH_PROJECTION_STALE",
    "UNKNOWN",
]


class Evidence(TypedDict):
    evidence_id: str
    source: str
    fact_type: str
    value: Any
    status: Literal["FOUND", "NOT_FOUND", "UNAVAILABLE", "UNAUTHORIZED"]
    authoritative: bool
