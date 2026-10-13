"""Agent 节点状态、模型结构化输出与决策类型。"""
from __future__ import annotations
from typing import Any, Literal, TypedDict
from pydantic import BaseModel, Field
from repair_agent.domain.models import IssueType, Evidence

Decision = Literal[
    "AUTO_EXECUTE", "NEED_APPROVAL", "REJECT", "NEED_MORE_INFORMATION"
]


class ConfidenceBreakdown(TypedDict):
    intent: float
    evidence_completeness: float
    evidence_consistency: float
    historical_match: float
    playbook_success_rate: float
    final: float


class RepairState(TypedDict, total=False):
    case_id: str
    requester_id: str
    raw_request: str
    order_id: str | None
    issue_type: IssueType
    claims: list[str]
    missing_information: list[str]
    evidence: list[Evidence]
    knowledge_hits: list[dict[str, Any]]
    knowledge_artifact: dict[str, Any]
    missing_evidence: list[str]
    queried_tools: list[str]
    tool_call_messages: list[dict[str, Any]]
    pending_tool_call: dict[str, Any] | None
    next_tool: str | None
    investigation_round: int
    root_cause: str | None
    diagnosis_evidence_ids: list[str]
    playbook_id: str | None
    confidence: float
    confidence_breakdown: ConfidenceBreakdown
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    decision: Decision
    decision_reasons: list[str]
    approved: bool | None
    approval_actor: str | None
    diagnosed_version: int | None
    execution_result: dict[str, Any] | None
    verification_result: dict[str, Any] | None
    status: str
    audit: list[str]


class TriageResult(BaseModel):
    order_id: str | None
    issue_type: IssueType
    claims: list[str]
    missing_information: list[str] = []


class DiagnosisResult(BaseModel):
    root_cause: str | None
    evidence_ids: list[str]
    playbook_id: str | None
    historical_match: float = Field(ge=0, le=1)
    evidence_consistency: float = Field(ge=0, le=1)


class ToolPlanResult(BaseModel):
    """旧 JSON 工具规划格式；当前远程规划使用原生 tool_calls。"""
    next_tool: Literal[
        "GET_ORDER", "GET_DELIVERY_TASK", "QUERY_OPERATION_EVENTS",
        "GET_DISPATCH_PROJECTION",
        "QUERY_LOG_PLATFORM", "QUERY_ONLINE_DATA", "QUERY_CONFIG_CENTER",
        "QUERY_SCHEDULER", "QUERY_HIVE_TABLE",
    ] | None
    reason: str
