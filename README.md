# Delivery State Repair Agent

面向异常运单状态的辅助诊断与修复项目。Agent 查询当前证据、选择白名单 Playbook，通过风险门禁、人工审批、版本复核与幂等执行完成修复，并独立验收。

## 启动与验证

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
repair-agent --reset-demo --approve "运单 W1001 已签收但仍在派送中"
repair-agent-api
python -m unittest discover -s tests -v
```

HTTP 服务默认地址为 `http://127.0.0.1:8000`，接口文档位于 `/docs`。`POST /cases` 同步创建工单，待审批时使用 `POST /cases/{case_id}/approval`。模型只负责理解、规划与诊断，门禁与执行由程序控制。

## 历史经验 RAG

使用中文双字切词与 BM25 检索，历史经验与当前业务证据分开保存。仅在当前事实通过门禁时允许修复；检索得分不等于正确率。示例均为合成数据，不自动导入。

```bash
python -m repair_agent.knowledge examples/historical_cases.json
```

支持 JSON/JSONL 导入，`GET /knowledge/search?query=签收` 可预览引用。真实模型诊断收到参考经验，规则推理仍依据当前证据。

## 五种模拟平台工具

日志、线上数据、配置中心、定时任务和 Hive 工具均为只读。平台快照位于 `repair_agent/platform_fixtures.json`；线上数据读取当前演示库，Hive 是历史快照。`GET /tools/platforms` 查看目录，`GET /tools/platforms/{tool_name}?waybill_id=W1001` 调用工具。

## 封装修复接口

Agent 通过固定 HTTP 端点调用后端修复接口，不直接执行写 SQL。设置 `REPAIR_BACKEND_BASE_URL` 连接实际后端，默认使用本地 HTTP 模拟传输。写请求携带幂等键及诊断版本，超时保留结果未知，不自动重发。

```bash
uvicorn repair_agent.backend_api:create_backend_app --factory --host 127.0.0.1 --port 8001
```

`REPAIR_BACKEND_API_KEY` 可配置认证头；后端必须实现实际的幂等、版本与业务前提校验。
