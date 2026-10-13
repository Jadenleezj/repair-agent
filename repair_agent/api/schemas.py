"""HTTP 请求与响应校验结构。"""
from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field

class CreateCaseRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    requester_id: str = Field(default="demo-user", min_length=1, max_length=100)


class ApprovalRequest(BaseModel):
    actor_id: str = Field(default="demo-approver", min_length=1, max_length=100)
    approved: bool
    comment: str = Field(default="", max_length=1000)


class CaseResponse(BaseModel):
    case_id: str
    status: str
    state: dict[str, Any]
    pending_approval: dict[str, Any] | None = None
