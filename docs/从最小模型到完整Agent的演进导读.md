# 从最小模型到完整 Agent：按问题逐步理解 Repair Agent

这份导读采用教学顺序：先理解一个模型能完成的最小任务，再通过具体问题解释后续机制。教学层次不等于 Git 提交时间线；第一批提交已经包含审批、检查点、幂等与验收，并不是一个只有模型调用的原型。

## 贯穿整个项目的例子

用户说：“订单 W1001 线下已签收，但系统还是派送中。”

我们需要依次回答：

1. 用户在说哪张订单、什么问题？
2. 当前订单、派送任务和作业事件分别是什么状态？
3. 有没有足够证据确认异常？
4. 哪个封装业务接口可以处理，是否需要人工批准？
5. 请求是否实际执行，修复后业务条件是否成立？

当前演示数据中，W1001 订单为 DELIVERING、派送任务为 COMPLETE，并有成功的签收作业记录。签收修复为中风险，需要审批。W1003 本身已经完成签收，可以用来理解“用户描述不等于真实异常”。

## 先认清项目中的“模型”

入口在 `repair_agent/agent/reasoning.py` 的 `reasoner_from_env()`。

- 默认使用 `RuleReasoner`，按关键词和规则理解、规划、诊断，不调用真实大模型。
- `REPAIR_LLM_BASE_URL`、`REPAIR_LLM_API_KEY`、`REPAIR_LLM_MODEL` 全部配置后，使用 `HttpJsonReasoner` 调用模型服务。
- 真实适配器的普通请求或结构校验失败会回退规则；预算和取消异常会继续向上抛出，终止任务。

二者都实现三个职责：`triage()` 理解请求、`plan_next_tool()` 选择调查工具、`diagnose()` 提出诊断与修复方案。

学习时先用规则版本观察输入输出，再理解真实模型如何实现同一接口。默认演示跑通，不代表已经验证真实模型的诊断质量。

## 第一步：让模型理解用户的问题

最小流程是：

```text
用户描述 → triage() → 订单号、问题类型、原始主张、缺失信息
```

例如输入：

```text
订单 W1001 线下已签收，但系统还是派送中
```

规则推理器输出的结构为：
```json
{
  "order_id": "W1001",
  "issue_type": "DELIVERY_TASK_ORDER_MISMATCH",
  "claims": ["订单 W1001 线下已签收，但系统还是派送中"],
  "missing_information": []
}
```

`issue_type` 代表待调查的方向，此时尚未确认异常。没有订单号时应要求补充信息，不能自行猜一个。

只运行这一层的练习：

```bash
source .venv/bin/activate
python - <<'PY'
from repair_agent.agent.reasoning import RuleReasoner

reasoner = RuleReasoner()
for message in [
    "订单 W1001 线下已签收，但系统还是派送中",
    "这个件线下已签收，系统还是派送中",
    "订单 W3001 已送达，但看板显示派送中",
]:
    print(reasoner.triage(message).model_dump_json(indent=2))
PY
```

代码阅读：`agent/models.py:TriageResult` → `domain/identifiers.py:extract_order_id` → `agent/reasoning.py:RuleReasoner.triage` → `agent/graph.py:parse_and_triage`。

这一层的局限：模型拿到的是用户描述，不能据此确定当前数据库状态。因此下一步需要查询事实。

## 第二步开始：每次只为一个具体问题增加机制

| 层次 | 当前流程遇到的问题 | 增加的机制 | 当前代码位置 | 如何验证理解 |
|---|---|---|---|---|
| 2：当前事实 | 用户可能误报；模型不知道线上状态 | 只读查询订单、派送任务、作业事件，形成结构化证据 | `tools/service.py`、`domain/models.py:Evidence` | W1003 已签收，最终应为 NO_ANOMALY_FOUND |
| 3：调查规划 | 不同异常需要查询不同数据，查询可能失败 | 规划 → 查询 → 更新状态的循环；保存已查工具和调查轮次 | `agent/reasoning.py:plan_next_tool`、`agent/graph.py:plan_investigation/execute_read_tool` | 比较签收异常与看板异常的 queried_tools |
| 4：受控决策 | 模型可能提出错误方案，查询证据可能不完整 | 结构校验、Playbook 白名单、缺证据门禁与风险策略 | `agent/models.py:DiagnosisResult`、`domain/playbooks.py`、`agent/graph.py:assess_decision` | W1002 作业事件不可用，应升级人工排查 |
| 5：人工审批 | 修改业务数据需要人工确认，等待可能很长 | interrupt 暂停，检查点保存状态，批准后重新读取版本 | `agent/graph.py:approval/revalidate`、`application/runtime.py` | 审批期间修改版本，应为 PRECONDITIONS_CHANGED |
| 6：修复闭环 | Agent 不应拥有自由修改业务库的权限；请求可能重复；接口成功可能没有恢复业务 | 调用固定后端接口，传递幂等键和预期版本，独立读取验收 | `tools/backend_client.py`、`demo/backend.py`、`tools/service.py:verify` | 同一幂等键重复执行，版本只变化一次 |
| 7：历史经验 | 模型缺少团队积累的诊断经验 | RAG 检索脱敏历史案例，作为参考上下文 | `knowledge/store.py`、`agent/graph.py:retrieve_knowledge`、`agent/reasoning.py:diagnose` | 历史案例命中但当前证据不足，仍不能修复 |
| 8：平台信息 | 核心业务表难以解释配置、任务、日志或离线链路问题 | 五类只读平台工具，明确实时数据和历史快照的区别 | `tools/platforms.py`、`tools/fixtures/platforms.json` | 线上修复后数据变化，Hive 历史快照保持原样 |
| 9：运行边界 | 模型可能重复查询、网络缓慢或依赖持续故障 | 总时长与模型/token/重复预算、取消、底层 HTTP 超时、有限重试和熔断 | `execution/reliability.py`、`execution/http_io.py`、`agent/graph.py` | 超预算终止；只读暂态故障可重试，写请求只发一次 |
| 10：长期服务 | 请求占用 HTTP 线程；等待审批占槽位；进程可能退出 | 持久化有界队列、后台 Worker、心跳租约、审批重新入队 | `persistence/jobs.py`、`execution/worker.py`、`api/app.py` | 重启后恢复读任务；过期执行者不能回写结果 |
| 11：未知结果 | 后端提交成功后响应丢失，直接重试可能再次修改 | 写前保存意图，按原幂等键只读对账，再独立验收 | `persistence/jobs.py:begin_write/recover`、`application/runtime.py:reconcile` | 响应丢失后只调用一次修复接口，后续只查询 |
| 12：持续运行维护 | 大结果扩大上下文和检查点，历史记录持续增长，运行状态不透明 | 输出归档、终态保留期清理、健康/就绪/指标接口、有限停机收尾 | `persistence/artifacts.py`、`application/runtime.py:maintenance`、`api/app.py`、`execution/worker.py:stop` | 大结果不作为完整证据；清理保留未决工单；停机撤销执行权 |

建议每次只学习一层，分别写清楚它的输入、输出、一个正常例子和一个失败例子，再进入下一层。

## 为什么先理解事实和执行，再看 RAG

当前 RAG 使用中文双字切词与 BM25，没有 embedding、向量库或 reranker。历史案例包含前提、根因、修复步骤和验收方法，用来帮助推理。

“过去这样修复成功”不能证明“当前这个订单可以这样修复”。即便历史案例完全匹配，也仍需要当前证据、风险判断、版本复核与后端业务校验。

因此先理解实时事实和执行闭环，会更容易看懂 RAG 应该放在哪个位置，以及为什么它没有直接执行权。

## 看源码时要保留的问题意识

以下是当前代码的具体边界，可以作为后续改进的练习，不应描述为已经实现的能力：

- `RuleReasoner` 使用关键词分类与预设工具顺序。比较“已签收”“尚未签收”“签收记录撤销”等表达，可以暴露语言理解的局限；需要真实案例评测后再决定如何改进模型或规则。
- 默认规则规划会在业务查询之后继续查询五个平台。需要通过调用数量和诊断收益判断是否有必要，再优化按需规划。
- 置信度中的部分输入和 Playbook 成功率是演示常量，不能当作真实线上准确率。需要有标签案例、实际执行统计和阈值校准。
- 历史检索主要是规则演示中的展示和审计；真实模型适配器会收到历史引用。比较有无历史引用的诊断结果，才能评估 RAG 带来的收益。
- SQLite 队列与线程 Worker 面向单机有限并发；协作式预算无法强制结束任意无超时的自定义阻塞函数。
- 当前 API 没有企业身份认证和 RBAC，记录审批人字符串不等于校验其权限。

## 和六批提交的对应关系

```text
da479af：已包含教学层次 1～6 的大部分基础闭环
90882d7：增加历史经验 RAG
4e7b7c1：增加五个平台工具
f38af23：将修复和核心事实读取迁移到封装 HTTP 后端
c6b6a35：增加运行预算、队列、恢复、对账和维护
a25b928：补齐中文与架构文档
```

可以在当前目录只读查看历史代码和增量，不必切换工作区：

```bash
git show da479af:repair_agent/reasoning.py
git diff da479af 90882d7 -- repair_agent/graph.py repair_agent/knowledge.py
git show --stat c6b6a35
```

历史拆分版本存在已知边界：前三批的 `Playbook.to_dict()` 保留了对第 4 批才加入的 `backend_client` 的引用，调用其 `/playbooks` 路由会出现缺模块错误。先前各批通过的测试没有覆盖该路由。当前最终版本已有该模块。学习时以当前源码分层阅读，并使用历史 diff 理解增加了什么，不把历史测试通过理解为所有接口都已被验证。

下一次逐行讲解应从 `RuleReasoner.triage()` 开始，然后进入 `parse_and_triage()` 与第一项只读证据查询。
