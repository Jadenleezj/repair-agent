from __future__ import annotations

import os
from pathlib import Path
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool
from .jobs import QueueFull

from fastapi import FastAPI, HTTPException, Query, Response

from .models import ApprovalRequest, CaseResponse, CreateCaseRequest, IssueType
from .playbooks import PLAYBOOKS
from .runtime import RepairRuntime
from .platform_tools import PlatformToolName


DEFAULT_DB = Path(os.getenv("REPAIR_AGENT_DB", "data/repair_agent.db"))


# 创建 FastAPI 路由并注入运行时；未传入运行时时使用默认数据库。
def create_app(runtime: RepairRuntime | None = None) -> FastAPI:
    owned = runtime is None
    current = runtime or RepairRuntime(DEFAULT_DB)

    # 生命周期负责启动消费者与有限停机；注入的测试运行时由调用者释放。
    @asynccontextmanager
    async def lifespan(app):
        current.start_workers()
        try:yield
        finally:
            if owned:await run_in_threadpool(current.close)
            elif current.workers:await run_in_threadpool(current.workers.stop)

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

    # 就绪检查只探测必要本地依赖，外部平台故障通过熔断与监控单独呈现。
    @app.get("/ready")
    def readiness():
        try:
            with current.jobs.connect() as conn:conn.execute('SELECT 1')
        except Exception:raise HTTPException(503,'database unavailable')
        if current.closed or not current.workers or not current.workers.ready():
            raise HTTPException(503,'workers not ready')
        return {'status':'ready'}

    # JSON 运维指标：有界计数、队列积压、未决写操作及各依赖熔断状态。
    @app.get("/metrics")
    def metrics():
        return {**current.metrics.snapshot(),**current.jobs.stats(),'dependencies':current.guard.snapshot()}

    # 查询持久化任务进度，不暴露内部 owner 令牌或请求载荷。
    @app.get("/cases/{case_id}/task")
    def task(case_id: str):
        result=current.jobs.get(case_id)
        if result is None:raise HTTPException(404,'task not found')
        return result

    # 取消诊断任务；已提交的修复必须先对账，不能宣称已撤销。
    @app.post("/cases/{case_id}/cancel")
    def cancel(case_id: str):
        try:return current.jobs.cancel(case_id)
        except KeyError:raise HTTPException(404,'task not found')
        except ValueError as exc:raise HTTPException(409,str(exc))

    # 未决修复达到重试上限后，可人工重新提交只读对账。
    @app.post("/cases/{case_id}/reconcile",status_code=202)
    def reconcile(case_id: str):
        try:return current.jobs.request_reconcile(case_id)
        except KeyError:raise HTTPException(404,'task not found')
        except QueueFull as exc:raise HTTPException(429,str(exc))
        except ValueError as exc:raise HTTPException(409,str(exc))

    # 只读取已有的归档证据，哈希参数不允许路径穿越。
    @app.get("/artifacts/{artifact_id}")
    def artifact(artifact_id: str):
        try:return current.artifacts.read(artifact_id)
        except (ValueError,FileNotFoundError):raise HTTPException(404,'artifact not found')

    # 根据查询、可选异常类型和数量限制预览历史引用，不执行修复。
    @app.get("/knowledge/search")
    def search_knowledge(
        query: str = Query(min_length=1, max_length=8000),
        issue_type: IssueType | None = None,
        limit: int = Query(default=3, ge=1, le=10),
    ) -> list[dict]:
        return app.state.runtime.knowledge.search(query, issue_type, limit)

    # 列出五个平台模拟工具；工具均为只读，参数统一为运单号。
    @app.get("/tools/platforms")
    def platform_catalog() -> list[dict]:
        return app.state.runtime.tools.platforms.catalog()

    # 单独调用一个平台工具，便于调试结果或模拟故障，不触发修复。
    @app.get("/tools/platforms/{tool_name}")
    def query_platform(tool_name: PlatformToolName, waybill_id: str = Query(pattern=r"^W[0-9]{4,}$")) -> dict:
        return app.state.runtime.tools.call_read_tool(tool_name, waybill_id)

    # 返回所有允许选择的修复方案及风险元数据。
    @app.get("/playbooks")
    def list_playbooks() -> list[dict]:
        return [item.to_dict() for item in PLAYBOOKS.values()]

    # 接收并校验用户请求，交给运行时创建诊断工单。
    @app.post("/cases", response_model=CaseResponse, status_code=202)
    def create_case(request: CreateCaseRequest, response: Response, wait: bool = False) -> dict:
        try:
            if wait:
                response.status_code=201
                return current.create_case(request.message,request.requester_id)
            return current.submit_case(request.message,request.requester_id)
        except QueueFull as exc:raise HTTPException(429,str(exc))
        except ValueError as exc:raise HTTPException(503,str(exc))

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
    @app.post("/cases/{case_id}/approval", response_model=CaseResponse, status_code=202)
    def approval(case_id: str, request: ApprovalRequest, response: Response, wait: bool = False) -> dict:
        try:
            if wait and current.jobs.get(case_id):raise ValueError("queued tasks must use asynchronous approval")
            if wait:response.status_code=200
            method=current.decide_approval if wait else current.submit_approval
            return method(
                case_id, approved=request.approved,
                actor_id=request.actor_id, comment=request.comment,
            )
        except QueueFull as exc:
            raise HTTPException(429,str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="case not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
