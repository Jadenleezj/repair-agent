# Delivery State Repair Agent

长期运行使用持久化队列与后台 Worker，默认提交返回 HTTP 202。详见 [稳定性与长期运行](docs/稳定性与长期运行.md)。

中文入口与代码说明见 [项目使用与代码导读](docs/项目使用与代码导读.md)。

目录职责、依赖方向与新导入路径见 [子包结构与依赖关系](docs/子包结构与依赖关系.md)。

按“遇到问题 → 增加机制”的顺序学习，见 [从最小模型到完整 Agent 的演进导读](docs/从最小模型到完整Agent的演进导读.md)。

一个面向派送作业的生产修数 Agent 面试项目。

## 业务背景

派送业务同时包含线下作业和多个线上系统。作业人员可能出现漏扫、补扫、乱序扫码、重复提交或弱网下延迟上报，导致：

- 线下已完成签收，订单仍停留在“派送中”。
- 骑手已接单，订单仍停留在“刚创建”。
- 核心订单状态正确，但作业看板的读模型没有同步。
- 订单、派送任务、作业事件和看板之间出现状态不一致，阻塞交接、完结、结算或后续作业。

业务人员提交问题后，Agent 会核对订单、派送任务和作业记录，识别真实的状态不一致，选择经审核的修数 Playbook，并根据风险自动执行或等待开发审批，以减少对现场作业的阻塞。

> 业务描述是 Claim，不是 Fact；模型结论是 Recommendation，不是 Authorization；Tool 成功不等于作业已恢复。

## 派送状态模型

状态枚举、含义和代码修改位置见 [业务状态定义](docs/业务状态定义.md)，也可通过 `GET /statuses` 查询。

```text
订单：CREATE → DELIVERING → DELIVERED
                    ↕ ON_HOLD
           异常无法派送 → RETURNED

派送任务：CREATE → PENDING_ASSIGN → ASSIGNED → COMPLETE
```

当前 Demo 将下列数据源分开建模：

```text
核心订单 Order          订单权威状态
派送任务 DeliveryTask    作业任务状态与派送员
作业事件 OperationEvent  骑手接单、签收等作业记录
派送看板 Projection       供运营和现场查看的读模型
```

## 核心流程

```text
业务问题
   ↓
Triage：提取订单号和问题类型
   ↓
Investigation Planner ⇄ 只读 Tool
   ↓
证据驱动的状态不一致诊断
   ↓
硬门禁＋可解释置信度
   ├─ 证据不足 → 转开发人工排查
   ├─ 低风险   → 自动执行
   └─ 中高风险 → 暂停等待审批
                         ↓
                再校验 → 幂等修数 → 独立验收
```

## 内置 Playbook

| Playbook | 适用问题 | 风险 | 执行方式 |
|---|---|---:|---|
| `APPLY_DELIVERY_COMPLETION_V1` | 签收作业已成功，派送任务已完成，订单仍派送中 | MEDIUM | 人工审批 |
| `APPLY_PICKUP_SCAN_V1` | 骑手已接任务且接单记录有效，订单仍为 CREATE | HIGH | 人工审批 |
| `REBUILD_DISPATCH_PROJECTION_V1` | 核心订单正确，派送看板过期 | LOW | 自动执行 |

Agent 只能选择 Playbook，不允许生成任意 SQL。

## 安全与工程特性

- LangGraph 多轮调查循环。
- SQLite Checkpointer，进程重启后恢复待审批 Case。
- Case、审批、时间线和执行审计持久化。
- 五维置信度：意图、证据完整度、一致性、历史匹配度、Playbook 成功率。
- 作业记录缺失时硬门禁拒绝修数。
- `interrupt()` 人工审批与恢复。
- 审批后重新校验订单版本，防止 TOCTOU。
- 修数 Tool 幂等，并使用独立只读查询验收。
- FastAPI 工单、审批、时间线和 Playbook 接口。
- 默认本地 Reasoner，可选支持 DeepSeek JSON Output 或 OpenAI JSON Schema，以及原生 Tool Calling 的真实 LLM 端点。
- 持久化有界任务队列、后台 Worker、租约与心跳、审批后重新入队。
- 时长与模型预算、协作式取消、只读重试、依赖并发隔离和熔断。
- 修复结果未知时只读对账；大证据归档与终态检查点保留期清理。
- 存活、就绪和运行指标接口。

## 快速启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### CLI 演示

签收后订单未完结：

```bash
repair-agent --reset-demo --approve \
  "订单 W1001 线下已签收，但系统还是派送中，阻塞交接作业"
```

作业事件平台不可用，不修数：

```bash
repair-agent --reset-demo \
  "订单 W1002 线下已签收，但系统还是派送中"
```

接单后订单未进入派送中：

```bash
repair-agent --reset-demo --approve \
  "订单 W2001 骑手已接单，但订单还是刚创建"
```

看板读模型过期，低风险自动修复：

```bash
repair-agent --reset-demo \
  "订单 W3001 已送达，但派送看板显示还在派送中"
```

### HTTP API

```bash
repair-agent-api
```

打开 `http://127.0.0.1:8000/docs`，或创建 Case：

默认提交与审批返回 HTTP 202，后台继续执行。创建后查询工单状态，到 `WAITING_APPROVAL` 时提交审批，再查询最终状态。同步 CLI 和 `?wait=true` 仅供演示。

```bash
curl -s http://127.0.0.1:8000/cases \
  -H 'Content-Type: application/json' \
  -d '{"message":"订单 W1001 线下已签收，但系统还是派送中","requester_id":"station-operator"}'
```

```text
GET  /health
GET  /ready
GET  /metrics
GET  /playbooks
GET  /cases
GET  /cases/{case_id}
GET  /cases/{case_id}/timeline
GET  /cases/{case_id}/task
POST /cases/{case_id}/approval
POST /cases/{case_id}/cancel
POST /cases/{case_id}/reconcile
GET  /artifacts/{artifact_id}
POST /demo/orders/{order_id}/bump-version
POST /demo/reset
```

## 可选真实 LLM

在项目根目录的 `.env` 中填写以下配置，启动时会自动读取：

```dotenv
REPAIR_LLM_BASE_URL=https://api.deepseek.com
REPAIR_LLM_API_KEY=你的DeepSeek_API密钥
REPAIR_LLM_MODEL=deepseek-flash
REPAIR_LLM_PROVIDER=auto
```

本地已创建密钥留空的 `.env`，新环境可从 `.env.example` 复制。已有环境变量优先，修改文件后重启服务。完整说明见 [本地配置文件](docs/本地配置文件.md)。

LLM 只负责理解业务描述、规划只读查询和提出诊断；风险门禁、审批、写操作和验收仍由确定性代码控制。LLM 引用的 `evidence_id` 和 Playbook ID 会被二次校验。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖签收修数、接单状态修数、读模型修复、作业证据缺失、业务误报、审批拒绝、幂等、进程重启恢复和 TOCTOU。

更详细的架构边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。


## 历史经验 RAG

诊断前新增 `retrieve_knowledge` 节点，按异常类型过滤，再使用中文双字切词与 BM25 检索历史案例（最多 3 条）。引用包含完整案例、检索分数与内容哈希，随 Case 状态持久化，CLI 输出也会展示。真实 LLM 诊断收到历史参考；默认本地 RuleReasoner 仍按当前证据进行规则诊断，引用只用于展示和审计。

这是本地关键词 RAG，不包含 embedding、向量库或 reranker。检索分数不是正确率，不改变现有历史匹配置信度或审批门禁。知识库为空或检索失败时继续原有证据诊断；历史内容不能替代当前作业证据，也不能授权写操作。现有 API 为本地 Demo，知识库应只导入可供当前使用者读取的脱敏案例；尚未实现按租户隔离检索。

### 导入案例

将真实经验整理为 JSON 数组或 JSONL，每条字段参考 `examples/historical_cases.json`。该文件是明确标注的合成示例，不会自动导入。`case_id` 唯一，重复导入更新同 ID；整批校验成功才写入。不允许未知 Playbook，但可将 `playbook_id` 设为 null，记录目前无法自动处理的经验。`issue_type` 目前限定为现有三类异常或 UNKNOWN，新异常类型仍需扩展诊断与 Playbook。

```bash
# 默认知识库路径与默认 CLI/API 数据库对应
python -m repair_agent.knowledge examples/historical_cases.json
# 导入自己的真实数据
python -m repair_agent.knowledge /absolute/path/history.jsonl
```

自定义业务数据库时，默认知识库路径为 `<业务数据库路径>.knowledge.db`。可以设置 `REPAIR_KNOWLEDGE_DB` 指定知识库文件，并在导入时用 `--db` 指向同一文件。导入后下一次请求立即可检索，无需重启。

可用 `GET /knowledge/search?query=签收&issue_type=DELIVERY_TASK_ORDER_MISMATCH` 预览检索结果；此接口不会执行修复。案例应包含诊断依据、适用前提、修复步骤和验收标准；上线前用未入库的独立案例评估检索效果。

## 平台模拟工具

已接入日志、线上数据、配置中心、定时任务和 Hive 五种只读工具，支持 Agent 调查与独立 HTTP 查询。使用示例和可编辑数据位置见 [中文导读](docs/项目使用与代码导读.md#五个平台模拟工具)。

## 封装修复接口

修复执行已改为 `Playbook → 固定 HTTP 接口 → 后端业务服务`。Agent 不直接执行业务写 SQL。默认使用本地 HTTP MockTransport；设置 `REPAIR_BACKEND_BASE_URL` 可调用独立后端。接口映射、请求参数、启动命令与超时处理见 [中文导读](docs/项目使用与代码导读.md#修复执行调用封装后端接口)。

### Order ID 命名与旧数据迁移

业务标识统一使用 Order ID（订单号），代码和 JSON 字段为 `order_id`，只读工具为 `GET_ORDER`，状态枚举为 `OrderStatus`，证据类型为 `ORDER_SNAPSHOT`。示例订单号仍使用 `W1001` 等值；目前规则解析与 API 校验仍使用 `W` 加至少四位数字的演示格式。

启动时自动将旧数据库的字段、演示订单表、工单状态、排队载荷、历史知识和 LangGraph 检查点转换为订单命名，并保留原有任务 ID、检查点 ID 与执行结果。迁移不会重新执行修数操作。更新服务前应停止旧版本进程并备份数据库；不要让新旧版本同时访问迁移中的数据库。

HTTP 参数与路径、工具名及异常类型已同步更名，外部调用方需要使用 `order_id`、`GET_ORDER`、`DELIVERY_TASK_ORDER_MISMATCH` 和 `/demo/orders/{order_id}/bump-version` 等新接口名称。

### 原生 Tool Calling 调查流程

配置三个 `REPAIR_LLM_*` 参数后，问题提取和诊断按服务商使用 JSON Schema 或 JSON Output，并用 Pydantic 校验；只读调查使用 Chat Completions 的原生 `tools` / `tool_calls` 协议。

1. `agent/reasoning.py:plan_tool_call()` 将尚未查询的工具名称、用途和 `order_id` 参数 Schema 放入请求的 `tools` 字段，每轮最多选择一个工具。
2. 模型返回 `tool_calls`，包含调用 ID、工具名称和 JSON 参数；普通消息且无工具调用表示调查结束。
3. 程序校验工具白名单、调用数量、参数结构及订单号。模型不能跨订单查询、请求写操作或重复查询已查工具。非法响应、网络失败或服务不支持该协议时回退本地规则，审计日志会记录实际执行的工具，模型回退会写入服务日志及指标。
4. `agent/graph.py:execute_read_tool()` 执行只读工具，将证据以 `role: tool` 和对应的 `tool_call_id` 写回消息历史。下一轮模型请求包含这些调用和结果。
5. 消息历史保存在 `RepairState.tool_call_messages`，未完成调用保存在 `pending_tool_call`，二者随检查点持久化。新任务初始化为空，不与其他任务共享。

模型负责提出调用请求，程序负责执行。修复写接口仍由 Playbook、风险门禁和审批控制，不开放为模型可自行选择的工具。无模型配置时继续使用本地规则；平台工具仍为演示数据。

相关契约与端到端流程测试见 `tests/test_tool_calling.py`；测试使用模拟 HTTP 响应，不调用付费模型 API。

本地 Postman 调试、日志路径和查询方式见 [本地调试与日志](docs/本地调试与日志.md)。

### DeepSeek 接入

默认配置示例使用 DeepSeek 官方 Chat Completions：`https://api.deepseek.com`，模型 `deepseek-flash`。必须使用 DeepSeek API 密钥，OpenAI 密钥不能用于该服务。项目不会自动充值或申请密钥。

DeepSeek 适配器独立位于 `repair_agent/agent/adapters/deepseek.py`，类名为 `DeepSeekReasoner`。它继承通用 HTTP 推理器，复用业务推理、工具校验、超时和预算处理，仅覆盖 DeepSeek 请求格式。`agent/adapters/__init__.py:create_reasoner()` 根据配置选择 `DeepSeekReasoner` 或原有 `HttpJsonReasoner`；原有推理器内没有 DeepSeek 分支。

`REPAIR_LLM_PROVIDER=auto` 会按 `api.deepseek.com` 官方域名选择 DeepSeek 适配；如果通过公司网关访问 DeepSeek，应显式设置 `REPAIR_LLM_PROVIDER=deepseek`。其他地址默认保持 OpenAI 请求格式，也可以显式设为 `openai`。

DeepSeek 的问题提取、诊断使用 `response_format={"type":"json_object"}`，系统提示中包含 JSON Schema 和格式示例；响应必须通过 Pydantic 类型校验，错误响应会回退规则。只读调查继续使用原生 `tools/tool_calls`，不发送稳定接口不支持的严格 Beta 模式及 OpenAI 专有并行参数，参数白名单与订单号校验仍在程序侧执行。默认明确设置 `thinking={"type":"disabled"}`，该 Demo 不启用思考模式。

修改配置后重启服务；调用成功的日志应包含 `model response finish_reason=...` 和 `HttpJsonReasoner._request_message completed`，出现 `LLM ... fallback` 表示模型阶段未成功完成。

官方说明：[首次调用](https://api-docs.deepseek.com/)、[JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)、[Chat Completions](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)。模拟协议和工作流测试见 `tests/test_deepseek.py`。

### 聊天工作台

重启 `repair-agent-api` 后打开 `http://127.0.0.1:8000/`。在聊天框描述订单问题并附上 Order ID，按 Enter 或点击发送即可创建任务，页面自动查询后台进度。Shift + Enter 换行。

左侧保留最近 100 个任务，点击可查看结果和处理过程。每个聊天对应一个独立修数任务；新问题点击“新建聊天”。审批任务可在结果卡片中批准或拒绝；批准会按原后端规则执行修复。模型和业务后端继续使用项目 `.env` 配置，浏览器不读取 API 密钥。

缺少信息时，页面提供“补充完整信息，重新提交”，它会预填原问题并创建新任务，当前后端尚不支持恢复同一个缺信息任务。前端使用已有 `/cases`、任务进度和审批接口，不增加修数执行权限。

页面源文件位于 `repair_agent/api/static/`，由 FastAPI 提供 HTML、CSS 和原生 JavaScript，无需额外前端构建或 Node 服务。CSS 支持窄屏布局。静态资源已纳入 Python 包数据。
