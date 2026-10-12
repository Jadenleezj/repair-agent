from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


IssueType = Literal[
    "DELIVERY_TASK_WAYBILL_MISMATCH",
    "PICKUP_SCAN_NOT_APPLIED",
    "DISPATCH_PROJECTION_STALE",
    "UNKNOWN",
]
Decision = Literal[
    "AUTO_EXECUTE", "NEED_APPROVAL", "REJECT", "NEED_MORE_INFORMATION"
]


class Evidence(TypedDict):
    evidence_id: str
    source: str
    fact_type: str
    value: Any
    status: Literal["FOUND", "NOT_FOUND", "UNAVAILABLE", "UNAUTHORIZED"]
    authoritative: bool


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
    waybill_id: str | None
    issue_type: IssueType
    claims: list[str]
    missing_information: list[str]
    evidence: list[Evidence]
    knowledge_hits: list[dict[str, Any]]
    knowledge_artifact: dict[str, Any]
    missing_evidence: list[str]
    queried_tools: list[str]
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


class CreateCaseRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    requester_id: str = Field(default="demo-user", min_length=1, max_length=100)


class ApprovalRequest(BaseModel):
    actor_id: str = Field(default="demo-approver", min_length=1, max_length=100)
    approved: bool
    comment: str = Field(default="", max_length=1000)


class TriageResult(BaseModel):
    waybill_id: str | None
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
    next_tool: Literal[
        "GET_WAYBILL", "GET_DELIVERY_TASK", "QUERY_OPERATION_EVENTS",
        "GET_DISPATCH_PROJECTION",
        "QUERY_LOG_PLATFORM", "QUERY_ONLINE_DATA", "QUERY_CONFIG_CENTER",
        "QUERY_SCHEDULER", "QUERY_HIVE_TABLE",
    ] | None
    reason: str


class CaseResponse(BaseModel):
    case_id: str
    status: str
    state: dict[str, Any]
    pending_approval: dict[str, Any] | None = None
