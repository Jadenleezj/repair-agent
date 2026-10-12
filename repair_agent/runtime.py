from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any
from langgraph.checkpoint.sqlite import SqliteSaver
from .graph import build_graph, resume_after_approval
from .reasoning import Reasoner, reasoner_from_env
from .repository import Repository
from .services import ToolService
from .knowledge import KnowledgeStore
from .backend_client import RepairBackendClient


class RepairRuntime:
    # 初始化业务仓储、推理器和持久化检查点，编译工作流。
    def __init__(self, db_path: str | Path, reasoner: Reasoner | None = None, knowledge_path: str | Path | None = None):
        self.db_path = str(db_path)
        self.repository = Repository(self.db_path)
        backend_url = os.getenv("REPAIR_BACKEND_BASE_URL")
        backend = RepairBackendClient(backend_url, api_key=os.getenv("REPAIR_BACKEND_API_KEY")) if backend_url else RepairBackendClient.local(self.repository)
        self.tools = ToolService(self.repository, backend=backend)
        self.knowledge = KnowledgeStore(knowledge_path or os.getenv("REPAIR_KNOWLEDGE_DB") or (self.db_path + ".knowledge.db"))
        self.reasoner = reasoner or reasoner_from_env()
        self.checkpoint_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self.checkpoint_conn)
        self.graph = build_graph(self.tools, reasoner=self.reasoner, knowledge=self.knowledge, checkpointer=self.checkpointer)
        self.invoke_lock = threading.RLock()

    # 工单 ID 同时作为检查点线程 ID，支持重启后恢复审批。
    @staticmethod
    def config(case_id: str) -> dict:
        return {"configurable": {"thread_id": case_id}, "recursion_limit": 40}

    # 提取人工审批中断的载荷。
    @staticmethod
    def pending_approval(result: dict[str, Any]) -> dict[str, Any] | None:
        interrupts = result.get("__interrupt__", [])
        return interrupts[0].value if interrupts else None

    # 复制当前图状态，供持久化审计与接口查询。
    def current_state(self, case_id: str) -> dict[str, Any]:
        return dict(self.graph.get_state(self.config(case_id)).values)

    # 执行新工单并保存暂停或结束状态。
    def create_case(self, message: str, requester_id: str = "demo-user") -> dict[str, Any]:
        case_id = f"CASE-{uuid.uuid4().hex[:10].upper()}"
        self.repository.create_case(case_id, requester_id, message)
        with self.invoke_lock:
            result = self.graph.invoke({"case_id": case_id, "requester_id": requester_id,
                "raw_request": message, "status": "CREATED", "audit": []}, config=self.config(case_id))
            state = self.current_state(case_id)
            self.repository.save_case_state(case_id, state)
            self.repository.add_event(case_id, "GRAPH_PAUSED" if self.pending_approval(result)
                                      else "GRAPH_FINISHED", {"status": state.get("status")})
        return {"case_id": case_id, "status": state.get("status", "UNKNOWN"),
                "state": state, "pending_approval": self.pending_approval(result)}

    # 仅允许处理待审批工单，保存人工结论后恢复检查点。
    def decide_approval(self, case_id: str, *, approved: bool, actor_id: str, comment: str = "") -> dict[str, Any]:
        with self.invoke_lock:
            case = self.repository.get_case(case_id)
            if case is None: raise KeyError(case_id)
            if case["status"] != "WAITING_APPROVAL":
                raise ValueError(f"case is not waiting for approval: {case['status']}")
            self.repository.add_approval(case_id, actor_id, approved, comment)
            result = resume_after_approval(self.graph, case_id, approved, actor_id)
            state = self.current_state(case_id)
            self.repository.save_case_state(case_id, state)
            self.repository.add_event(case_id, "GRAPH_FINISHED", {"status": state.get("status")})
        return {"case_id": case_id, "status": state.get("status", "UNKNOWN"),
                "state": state, "pending_approval": self.pending_approval(result)}

    # 工单详情读取不会触发诊断或修复。
    def get_case(self, case_id: str) -> dict[str, Any] | None:
        return self.repository.get_case(case_id)

    # 释放模型客户端、检查点和业务仓储连接。
    def close(self) -> None:
        if hasattr(self.reasoner, 'client'): self.reasoner.client.close()
        self.tools.backend.close()
        self.checkpoint_conn.close()
        self.repository.conn.close()
