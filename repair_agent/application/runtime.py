from __future__ import annotations

from repair_agent.application.logging_setup import traced, configure_logging

import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any
from repair_agent.application.config import load_local_config

from langgraph.types import Command
from repair_agent.persistence.jobs import JobStore
from repair_agent.persistence.artifacts import ArtifactStore
from repair_agent.execution.reliability import StabilitySettings, Metrics, DependencyGuard, RunControl, CURRENT_RUN
from repair_agent.execution.worker import WorkerPool

from langgraph.checkpoint.sqlite import SqliteSaver

from repair_agent.tools.backend_client import RepairBackendClient
from repair_agent.knowledge.store import KnowledgeStore
from repair_agent.agent.graph import build_graph, resume_after_approval
from repair_agent.agent.reasoning import Reasoner, reasoner_from_env
from repair_agent.persistence.repository import Repository
from repair_agent.tools.service import ToolService


class RepairRuntime:
    """Coordinates the graph, durable checkpoint, and business audit store."""

    # 初始化业务工具、历史知识库和 SQLite 检查点，并编译图；知识库路径可配置。
    def __init__(self, db_path: str | Path, reasoner: Reasoner | None = None,
                 knowledge_path: str | Path | None = None, *, settings: StabilitySettings | None = None,
                 metrics: Metrics | None = None, guard: DependencyGuard | None = None,
                 worker_reasoner_factory=None):
        load_local_config()
        configure_logging()
        self.db_path = str(db_path)
        self.settings = settings or StabilitySettings.from_env()
        self.settings.validate()
        self.metrics = metrics or Metrics()
        self.guard = guard or DependencyGuard(self.settings,self.metrics)
        self.worker_reasoner_factory = worker_reasoner_factory
        self.closed = False
        self.workers = None
        self.repository = Repository(self.db_path)
        backend_url = os.getenv("REPAIR_BACKEND_BASE_URL")
        backend = RepairBackendClient(backend_url, api_key=os.getenv("REPAIR_BACKEND_API_KEY")) if backend_url else RepairBackendClient.local(self.repository)
        self.tools = ToolService(self.repository, backend=backend)
        self.tools.guard = self.guard
        self.tools.backend.guard = self.guard
        self.tools.backend.client.timeout = self.settings.tool_timeout_seconds
        self.artifacts = ArtifactStore(self.db_path+'.artifacts',self.settings.output_bytes)
        self.tools.artifacts = self.artifacts
        self.jobs = JobStore(self.db_path,self.settings)
        self.reasoner = reasoner or reasoner_from_env()
        if hasattr(self.reasoner,'guard') or hasattr(self.reasoner,'client'):
            self.reasoner.guard = self.guard
        if hasattr(self.reasoner,'client'):
            self.reasoner.client.timeout = self.settings.model_timeout_seconds
        self.knowledge = KnowledgeStore(knowledge_path or os.getenv("REPAIR_KNOWLEDGE_DB") or (self.db_path + ".knowledge.db"))
        self.checkpoint_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self.checkpoint_conn)
        self.rebuild_graph()
        self.invoke_lock = threading.RLock()

    # 重新绑定当前推理器，Worker 可注入隔离的测试推理器或真实模型适配器。
    def rebuild_graph(self):
        self.graph = build_graph(self.tools,reasoner=self.reasoner,knowledge=self.knowledge,checkpointer=self.checkpointer)

    # 启动持久化任务消费者；重复调用不创建多个线程池。
    def start_workers(self):
        if self.closed: raise ValueError('runtime is closed')
        if self.workers is None:
            self.workers=WorkerPool(self)
            self.workers.start()

    # 异步提交只负责入库，HTTP 不等待模型或工具执行完成。
    @traced
    def submit_case(self,message,requester_id='demo-user'):
        if self.closed or (self.workers and self.workers.stop_event.is_set()): raise ValueError('runtime is stopping')
        return self.jobs.submit(message,requester_id)

    # 人工审批后任务重新入队，等待人工期间不占 Worker 槽位。
    @traced
    def submit_approval(self,case_id,*,approved,actor_id,comment=''):
        return self.jobs.approve(case_id,approved,actor_id,comment)

    # 根据检查点恢复只读调查或已审批任务；重新执行写操作由持久化 phase 禁止。
    @traced
    def invoke_job(self,job):
        snapshot=self.graph.get_state(self.config(job['case_id']))
        if job['kind']=='RESUME' and 'approval' in snapshot.next:
            return self.graph.invoke(Command(resume=job['payload']),self.config(job['case_id']))
        if snapshot.values and snapshot.next:
            return self.graph.invoke(None,self.config(job['case_id']))
        if snapshot.values and not snapshot.next:
            return dict(snapshot.values)
        return self.graph.invoke(job['payload'],self.config(job['case_id']))

    # 对未知结果只查询执行记录和验收，不再次发修复请求。
    @traced
    def reconcile(self,case_id,intent):
        case=self.repository.get_case(case_id)
        state=dict(case['state'] if case else {},case_id=case_id)
        control=CURRENT_RUN.get()
        if control: control.check()
        playbook_id=intent.get('playbook_id') or state.get('playbook_id')
        order_id=intent.get('order_id') or state.get('order_id')
        state.update(playbook_id=playbook_id,order_id=order_id)
        outcome=self.tools.backend.execution_status(f'{case_id}:{playbook_id}')
        record=outcome.get('record',{})
        state['reconciliation_result']=outcome
        if outcome.get('status')=='FOUND' and record.get('case_id')==case_id and record.get('playbook_id')==playbook_id:
            from repair_agent.tools.backend_client import BackendResult
            result=BackendResult.model_validate(record['result']).model_dump(exclude_none=True)
            binding=record['result'].get('request_binding')
            if binding and binding.get('order_id')!=order_id:
                state['status']='EXECUTION_OUTCOME_UNKNOWN'
                return state
            if result.get('success'):
                verification=self.tools.verify(order_id,playbook_id)
                state.update(execution_result=result,verification_result=verification,
                             status='SUCCEEDED' if verification['success'] else 'VERIFICATION_FAILED')
            else:
                state.update(execution_result=result,status='EXECUTION_FAILED')
        else:
            state['status']='EXECUTION_OUTCOME_UNKNOWN'
        state['audit']=[*state.get('audit',[]),f"reconcile: {state['status']}"]
        return state

    # 清理已终结工单及检查点，大结果只在无剩余工单引用时删除。
    def maintenance(self):
        with self.invoke_lock:
            for case_id in self.jobs.expired():
                self.checkpointer.delete_thread(case_id)
                self.jobs.delete_expired(case_id)
            import re
            refs=set()
            with self.repository.lock:
                for row in self.repository.conn.execute('SELECT state_json FROM repair_cases'):
                    refs.update(re.findall(r'"artifact_id"\s*:\s*"([0-9a-f]{64})"',row[0]))
            self.artifacts.cleanup(self.settings.retention_days,refs)

    # 生成以 case_id 为 thread_id 的检查点配置，让同一工单可暂停和恢复。
    @staticmethod
    def config(case_id: str) -> dict:
        return {"configurable": {"thread_id": case_id}, "recursion_limit": 40}

    # 提取图返回的首个 interrupt 载荷；没有暂停时返回 None。
    @staticmethod
    def pending_approval(result: dict[str, Any]) -> dict[str, Any] | None:
        interrupts = result.get("__interrupt__", [])
        return interrupts[0].value if interrupts else None

    # 读取指定工单的最新检查点状态，并复制为普通字典。
    def current_state(self, case_id: str) -> dict[str, Any]:
        return dict(self.graph.get_state(self.config(case_id)).values)

    # 创建并执行工单，持久化最新图状态和结束或暂停事件，返回审批载荷。
    @traced
    def create_case(self, message: str, requester_id: str = "demo-user") -> dict[str, Any]:
        case_id = f"CASE-{uuid.uuid4().hex[:10].upper()}"
        self.repository.create_case(case_id, requester_id, message)
        with self.invoke_lock:
            token=CURRENT_RUN.set(RunControl(self.settings))
            try:
                result = self.graph.invoke(
                {"case_id": case_id, "requester_id": requester_id,
                 "raw_request": message, "status": "CREATED", "audit": []},
                    config=self.config(case_id),
                )
            except Exception as exc:
                from repair_agent.execution.reliability import RunStopped
                state=self.current_state(case_id) or {'case_id':case_id}
                control=CURRENT_RUN.get()
                state['status']='EXECUTION_OUTCOME_UNKNOWN' if control.write_started else (exc.status if isinstance(exc,RunStopped) else 'FAILED')
                self.repository.save_case_state(case_id,state)
                raise
            finally:CURRENT_RUN.reset(token)
        state = self.current_state(case_id)
        self.repository.save_case_state(case_id, state)
        self.repository.add_event(case_id, "GRAPH_PAUSED" if self.pending_approval(result)
                                  else "GRAPH_FINISHED", {"status": state.get("status")})
        return {"case_id": case_id, "status": state.get("status", "UNKNOWN"),
                "state": state, "pending_approval": self.pending_approval(result)}

    # 确认工单处于待审批状态，记录结论，恢复图执行并保存最终状态。
    @traced
    def decide_approval(
        self, case_id: str, *, approved: bool, actor_id: str, comment: str = ""
    ) -> dict[str, Any]:
        case = self.repository.get_case(case_id)
        if case is None:
            raise KeyError(case_id)
        if case["status"] != "WAITING_APPROVAL":
            raise ValueError(f"case is not waiting for approval: {case['status']}")
        with self.invoke_lock:
            # 同步演示入口也必须执行预算检查，审批不能绕过稳定性控制。
            case = self.repository.get_case(case_id)
            if case["status"] != "WAITING_APPROVAL":
                raise ValueError(f"case is not waiting for approval: {case['status']}")
            self.repository.add_approval(case_id, actor_id, approved, comment)
            control = RunControl(self.settings)
            token = CURRENT_RUN.set(control)
            try:
                result = resume_after_approval(self.graph, case_id, approved, actor_id)
            except Exception as exc:
                from repair_agent.execution.reliability import RunStopped
                state = self.current_state(case_id) or {"case_id": case_id}
                state["status"] = "EXECUTION_OUTCOME_UNKNOWN" if control.write_started else (
                    exc.status if isinstance(exc, RunStopped) else "FAILED"
                )
                self.repository.save_case_state(case_id, state)
                self.repository.add_event(case_id, "GRAPH_STOPPED", {"status": state["status"]})
                raise
            finally:
                CURRENT_RUN.reset(token)
            state = self.current_state(case_id)
            self.repository.save_case_state(case_id, state)
            self.repository.add_event(case_id, "GRAPH_FINISHED", {"status": state.get("status")})
        return {"case_id": case_id, "status": state.get("status", "UNKNOWN"),
                "state": state, "pending_approval": self.pending_approval(result)}

    # 从业务仓储读取已持久化工单，不触发图执行。
    def get_case(self, case_id: str) -> dict[str, Any] | None:
        return self.repository.get_case(case_id)

    # 关闭检查点和业务数据库连接；知识库使用按次自动关闭的连接。
    @traced
    def close(self) -> None:
        if self.closed:return
        self.closed=True
        if self.workers:self.workers.stop()
        with self.invoke_lock:
            if hasattr(self.reasoner,'client'):self.reasoner.client.close()
            self.tools.backend.close()
            self.checkpoint_conn.close()
            self.repository.conn.close()
