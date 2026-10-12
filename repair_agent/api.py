from __future__ import annotations

import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from .models import ApprovalRequest, CaseResponse, CreateCaseRequest, IssueType
from .playbooks import PLAYBOOKS
from .runtime import RepairRuntime


DEFAULT_DB = Path(os.getenv("REPAIR_AGENT_DB", "data/repair_agent.db"))


# 创建 FastAPI 路由并注入运行时；未传入运行时时使用默认数据库。
def create_app(runtime: RepairRuntime | None = None) -> FastAPI:
    owned = runtime is None
    current = runtime or RepairRuntime(DEFAULT_DB)

    # 生命周期负责启动消费者与有限停机；注入的测试运行时由调用者释放。
    @asynccontextmanager
    async def lifespan(app):
        try: yield
        finally:
            if owned: current.close()

    app = FastAPI(
        lifespan=lifespan,
        title="Delivery State Repair Agent",
        version="0.2.0",
        description="Evidence-driven LangGraph workflow with approval and durable audit.",
    )
    app.state.runtime = current

    # 返回服务存活标记，不执行数据库或模型连通性检查。
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}













    # 根据查询、可选异常类型和数量限制预览历史引用，不执行修复。


    # 列出五个平台模拟工具；工具均为只读，参数统一为运单号。


    # 单独调用一个平台工具，便于调试结果或模拟故障，不触发修复。


    # 返回所有允许选择的修复方案及风险元数据。
    @app.get("/playbooks")
    def list_playbooks() -> list[dict]:
        return [item.to_dict() for item in PLAYBOOKS.values()]

    # 接收并校验用户请求，交给运行时创建诊断工单。
    @app.post("/cases", response_model=CaseResponse, status_code=201)
    def create_case(request: CreateCaseRequest) -> dict:
        return current.create_case(request.message, request.requester_id)

    # 返回最近创建的工单摘要，按 limit 限制数量。
    @app.get("/cases")
    def list_cases(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
        return app.state.runtime.repository.list_cases(limit)

    # 返回工单详情；不存在时返回 404。
    @app.get("/cases/{case_id}")
    def get_case(case_id: str) -> dict:
        case = app.state.runtime.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        return case

    # 按事件顺序读取工单业务时间线；节点细节另见 state.audit。
    @app.get("/cases/{case_id}/timeline")
    def timeline(case_id: str) -> list[dict]:
        if app.state.runtime.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return app.state.runtime.repository.timeline(case_id)

    # 提交审批并恢复工单；不存在返回 404，非待审批状态返回 409。
    @app.post("/cases/{case_id}/approval", response_model=CaseResponse)
    def approval(case_id: str, request: ApprovalRequest) -> dict:
        try:
            return current.decide_approval(case_id, approved=request.approved,
                actor_id=request.actor_id, comment=request.comment)
        except KeyError: raise HTTPException(404, "case not found") from None
        except ValueError as exc: raise HTTPException(409, str(exc)) from exc

    # 演示故障注入：增加运单版本，模拟审批期间数据变化；不存在时返回 404。
    @app.post("/demo/waybills/{waybill_id}/bump-version")
    def bump_version(waybill_id: str) -> dict:
        if not app.state.runtime.repository.bump_waybill_version(waybill_id):
            raise HTTPException(status_code=404, detail="waybill not found")
        return {"waybill_id": waybill_id, "mutated": True}

    # 重置演示业务数据，不清空历史知识库或工单。
    @app.post("/demo/reset")
    def reset_demo() -> dict:
        app.state.runtime.repository.reset_demo_data()
        return {"reset": True}

    return app


# 以工厂模式启动本地 HTTP 服务，绑定 127.0.0.1:8000。
def run() -> None:
    import uvicorn
    uvicorn.run(
        "repair_agent.api:create_app", factory=True,
        host="127.0.0.1", port=8000, reload=False,
    )
