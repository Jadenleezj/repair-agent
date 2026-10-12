"""独立运行的模拟业务后端；生产可替换为公司已有修复接口。"""
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import ValidationError

from .backend_client import READ_TOOLS, REPAIR_ENDPOINTS, RepairCommand
from .mock_backend import MockRepairBackend
from .repository import Repository

# 默认本地 HTTP 传输使用的路由适配器，和独立服务器共用同一后端服务实现。
def handle_mock_request(backend: MockRepairBackend, request: httpx.Request) -> httpx.Response:
    reverse = {path: pid for pid,path in REPAIR_ENDPOINTS.items()}
    if request.method == 'GET' and request.url.path.startswith('/evidence/'):
        tool = request.url.path.rsplit('/',1)[-1]
        if tool not in READ_TOOLS: return httpx.Response(404,json={'detail':'unknown tool'})
        return httpx.Response(200,json=backend.call_read_tool(tool,request.url.params.get('waybill_id','')))
    if request.method == 'POST' and request.url.path in reverse:
        try:
            command = RepairCommand.model_validate_json(request.content)
        except ValidationError:
            return httpx.Response(422,json={'detail':'invalid parameters'})
        if request.headers.get('Idempotency-Key') != command.idempotency_key:
            return httpx.Response(400,json={'detail':'idempotency key mismatch'})
        return httpx.Response(200,json=backend.execute_playbook(playbook_id=reverse[request.url.path],**command.model_dump()))
    return httpx.Response(404,json={'detail':'not found'})

# 提供同一读写契约的独立 FastAPI 应用，默认只绑定本机并使用演示库。
def create_backend_app(repository: Repository | None = None) -> FastAPI:
    from contextlib import asynccontextmanager
    owns_repo = repository is None
    repo = repository or Repository(Path(os.getenv('REPAIR_BACKEND_DB','data/repair_backend.db')))
    backend = MockRepairBackend(repo)

    # 服务停止时仅关闭由应用创建的仓储，测试注入的仓储由调用者管理。
    @asynccontextmanager
    async def lifespan(app):
        yield
        if owns_repo: repo.conn.close()

    app = FastAPI(title='Mock Business Repair Backend',lifespan=lifespan)

    # 只读业务证据查询，不接受任意 SQL。
    @app.get('/evidence/{tool}')
    def read(tool: str, waybill_id: str = Query(pattern=r'^W[0-9]{4,}$')):
        if tool not in READ_TOOLS: raise HTTPException(404,'unknown tool')
        return backend.call_read_tool(tool,waybill_id)

    # 修复请求超时后只查询记录，不触发重复写入。


    # 固定端点映射修复方案，后端独立执行版本和业务前提校验及幂等事务。
    def execute(command: RepairCommand, endpoint: str, key: str | None):
        if key != command.idempotency_key: raise HTTPException(400,'idempotency key mismatch')
        pid = next(pid for pid,path in REPAIR_ENDPOINTS.items() if path == endpoint)
        return backend.execute_playbook(playbook_id=pid,**command.model_dump())

    # 补齐签收状态的封装业务接口。
    @app.post('/repairs/delivery-completion')
    def delivery(command: RepairCommand, idempotency_key: str | None = Header(default=None)):
        return execute(command,'/repairs/delivery-completion',idempotency_key)

    # 补齐揽收状态的封装业务接口。
    @app.post('/repairs/pickup-scan')
    def pickup(command: RepairCommand, idempotency_key: str | None = Header(default=None)):
        return execute(command,'/repairs/pickup-scan',idempotency_key)

    # 重建读模型的封装业务接口。
    @app.post('/repairs/dispatch-projection')
    def projection(command: RepairCommand, idempotency_key: str | None = Header(default=None)):
        return execute(command,'/repairs/dispatch-projection',idempotency_key)
    return app
