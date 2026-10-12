# Delivery State Repair Agent

长期运行使用持久化队列与后台 Worker，默认提交返回 HTTP 202。详见 [稳定性与长期运行](docs/稳定性与长期运行.md)。

中文入口与代码说明见 [项目使用与代码导读](docs/项目使用与代码导读.md)。

一个面向派送作业的生产修数 Agent 面试项目。

## 业务背景

派送业务同时包含线下作业和多个线上系统。作业人员可能出现漏扫、补扫、乱序扫码、重复提交或弱网下延迟上报，导致：

- 线下已完成签收，运单仍停留在“派送中”。
- 派送员已揽件并开始派送，运单仍是“已分配”。
- 核心运单状态正确，但作业看板的读模型没有同步。
- 运单、派送任务、作业事件和看板之间出现状态不一致，阻塞交接、完结、结算或后续作业。

业务人员提交问题后，Agent 会核对运单、派送任务和作业记录，识别真实的状态不一致，选择经审核的修数 Playbook，并根据风险自动执行或等待开发审批，以减少对现场作业的阻塞。

> 业务描述是 Claim，不是 Fact；模型结论是 Recommendation，不是 Authorization；Tool 成功不等于作业已恢复。

## 派送状态模型

```text
CREATED → ASSIGNED → PICKED_UP / DELIVERING → DELIVERED
```

当前 Demo 将下列数据源分开建模：

```text
核心运单 Waybill          运单权威状态
派送任务 DeliveryTask    作业任务状态与派送员
作业事件 OperationEvent  揽收、签收等扫码记录
派送看板 Projection       供运营和现场查看的读模型
```

## 核心流程

```text
业务问题
   ↓
Triage：提取运单号和问题类型
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
| `APPLY_DELIVERY_COMPLETION_V1` | 签收作业已成功，派送任务已完成，运单仍派送中 | MEDIUM | 人工审批 |
| `APPLY_PICKUP_SCAN_V1` | 揽收扫描成功且任务已派送中，运单仍已分配 | HIGH | 人工审批 |
| `REBUILD_DISPATCH_PROJECTION_V1` | 核心运单正确，派送看板过期 | LOW | 自动执行 |

Agent 只能选择 Playbook，不允许生成任意 SQL。

## 安全与工程特性

- LangGraph 多轮调查循环。
- SQLite Checkpointer，进程重启后恢复待审批 Case。
- Case、审批、时间线和执行审计持久化。
- 五维置信度：意图、证据完整度、一致性、历史匹配度、Playbook 成功率。
- 作业记录缺失时硬门禁拒绝修数。
- `interrupt()` 人工审批与恢复。
- 审批后重新校验运单版本，防止 TOCTOU。
- 修数 Tool 幂等，并使用独立只读查询验收。
- FastAPI 工单、审批、时间线和 Playbook 接口。
- 默认本地 Reasoner，可选支持 JSON Schema 的真实 LLM 端点。
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

签收后运单未完结：

```bash
repair-agent --reset-demo --approve \
  "运单 W1001 线下已签收，但系统还是派送中，阻塞交接作业"
```

作业事件平台不可用，不修数：

```bash
repair-agent --reset-demo \
  "运单 W1002 线下已签收，但系统还是派送中"
```

揽收后运单未进入派送中：

```bash
repair-agent --reset-demo --approve \
  "运单 W2001 作业员已取件，但运单还是已分配"
```

看板读模型过期，低风险自动修复：

```bash
repair-agent --reset-demo \
  "运单 W3001 已送达，但派送看板显示还在派送中"
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
  -d '{"message":"运单 W1001 线下已签收，但系统还是派送中","requester_id":"station-operator"}'
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
POST /demo/waybills/{waybill_id}/bump-version
POST /demo/reset
```

## 可选真实 LLM

```bash
export REPAIR_LLM_BASE_URL="https://your-provider.example/v1"
export REPAIR_LLM_API_KEY="..."
export REPAIR_LLM_MODEL="your-model"
```

LLM 只负责理解业务描述、规划只读查询和提出诊断；风险门禁、审批、写操作和验收仍由确定性代码控制。LLM 引用的 `evidence_id` 和 Playbook ID 会被二次校验。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖签收修数、揽收修数、读模型修复、作业证据缺失、业务误报、审批拒绝、幂等、进程重启恢复和 TOCTOU。

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

可用 `GET /knowledge/search?query=签收&issue_type=DELIVERY_TASK_WAYBILL_MISMATCH` 预览检索结果；此接口不会执行修复。案例应包含诊断依据、适用前提、修复步骤和验收标准；上线前用未入库的独立案例评估检索效果。

## 平台模拟工具

已接入日志、线上数据、配置中心、定时任务和 Hive 五种只读工具，支持 Agent 调查与独立 HTTP 查询。使用示例和可编辑数据位置见 [中文导读](docs/项目使用与代码导读.md#五个平台模拟工具)。

## 封装修复接口

修复执行已改为 `Playbook → 固定 HTTP 接口 → 后端业务服务`。Agent 不直接执行业务写 SQL。默认使用本地 HTTP MockTransport；设置 `REPAIR_BACKEND_BASE_URL` 可调用独立后端。接口映射、请求参数、启动命令与超时处理见 [中文导读](docs/项目使用与代码导读.md#修复执行调用封装后端接口)。
