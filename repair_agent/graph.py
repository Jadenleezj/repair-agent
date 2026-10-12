from __future__ import annotations

from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .knowledge import KnowledgeStore
from .models import RepairState
from .playbooks import get_playbook
from .reasoning import Reasoner, RuleReasoner
from .services import ToolService


REQUIRED_FACTS = {
    "DELIVERY_TASK_WAYBILL_MISMATCH": {
        "WAYBILL_SNAPSHOT", "DELIVERY_TASK", "OPERATION_EVENTS"
    },
    "PICKUP_SCAN_NOT_APPLIED": {
        "WAYBILL_SNAPSHOT", "DELIVERY_TASK", "OPERATION_EVENTS"
    },
    "DISPATCH_PROJECTION_STALE": {"WAYBILL_SNAPSHOT", "DISPATCH_PROJECTION"},
}


# 返回追加一条消息后的审计列表，不原地修改已有状态。
def append_audit(state: RepairState, message: str) -> list[str]:
    return [*state.get("audit", []), message]


# 收集状态为 FOUND 的事实类型，用于检查关键证据完整度。
def found_fact_types(state: RepairState) -> set[str]:
    return {x["fact_type"] for x in state.get("evidence", []) if x["status"] == "FOUND"}


# 构建调查、诊断、决策、审批、执行和验收图；可注入推理器、知识库与检查点。
def build_graph(
    tools: ToolService,
    *,
    reasoner: Reasoner | None = None,
    knowledge: KnowledgeStore | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
):
    reasoner = reasoner or RuleReasoner()

    # 理解原始请求并初始化调查状态；提取运单、异常类型和缺失信息。
    def parse_and_triage(state: RepairState) -> dict:
        result = reasoner.triage(state["raw_request"])
        return {
            "waybill_id": result.waybill_id,
            "issue_type": result.issue_type,
            "claims": result.claims,
            "missing_information": result.missing_information,
            "evidence": [],
            "queried_tools": [],
            "investigation_round": 0,
            "status": "TRIAGED",
            "audit": append_audit(
                state, f"triage: waybill={result.waybill_id}, issue={result.issue_type}"
            ),
        }

    # 缺信息走补充信息分支，未知类型结束，其余请求进入历史检索。
    def route_after_triage(
        state: RepairState,
    ) -> Literal["need_info", "retrieve_knowledge", "finalize"]:
        if state.get("missing_information"):
            return "need_info"
        if state.get("issue_type") == "UNKNOWN":
            return "finalize"
        return "retrieve_knowledge"

    # 检索同类历史经验并保存引用；失败时继续实时调查，历史不作为当前证据。
    def retrieve_knowledge(state: RepairState) -> dict:
        try:
            hits = knowledge.search(state["raw_request"], state.get("issue_type")) if knowledge else []
            message = f"retrieve_knowledge: references={[h['case']['case_id'] for h in hits]}"
        except Exception:
            hits = []
            message = "retrieve_knowledge: unavailable; continuing with live evidence only"
        return {"knowledge_hits": hits, "audit": append_audit(state, message)}

    # 记录缺失输入并返回待补充状态，不执行调查或修复。
    def need_info(state: RepairState) -> dict:
        return {
            "decision": "NEED_MORE_INFORMATION",
            "decision_reasons": [f"缺少必要信息: {state.get('missing_information', [])}"],
            "status": "NEED_MORE_INFORMATION",
            "audit": append_audit(state, "need_info: required input missing"),
        }

    # 规划下一项只读查询；最多调查九轮，以避免无界循环。
    def plan_investigation(state: RepairState) -> dict:
        if state.get("investigation_round", 0) >= 9:
            next_tool = None
        else:
            next_tool = reasoner.plan_next_tool(state)
        return {
            "next_tool": next_tool,
            "status": "INVESTIGATING" if next_tool else "INVESTIGATION_COMPLETE",
            "audit": append_audit(state, f"plan_investigation: next_tool={next_tool}"),
        }

    # 存在待查询工具则执行，否则进入诊断。
    def route_plan(state: RepairState) -> Literal["execute_read_tool", "diagnose"]:
        return "execute_read_tool" if state.get("next_tool") else "diagnose"

    # 执行只读工具，将证据、已查询工具和轮次写回图状态。
    def execute_read_tool(state: RepairState) -> dict:
        tool = state["next_tool"] or ""
        evidence = tools.call_read_tool(tool, state["waybill_id"] or "")
        queried = [*state.get("queried_tools", []), tool]
        all_evidence = [*state.get("evidence", []), evidence]
        return {
            "evidence": all_evidence,
            "queried_tools": queried,
            "investigation_round": state.get("investigation_round", 0) + 1,
            "status": "EVIDENCE_COLLECTED",
            "audit": append_audit(
                state, f"tool_call: {tool} -> {evidence['status']}"
            ),
        }

    # 调用推理器诊断，计算证据缺项与置信度分项，并记录诊断时运单版本。
    def diagnose(state: RepairState) -> dict:
        result = reasoner.diagnose(state)
        required = REQUIRED_FACTS.get(state.get("issue_type", "UNKNOWN"), set())
        found = found_fact_types(state)
        missing = sorted(required - found)
        waybill = next(
            (x["value"] for x in state.get("evidence", [])
             if x["fact_type"] == "WAYBILL_SNAPSHOT" and x["status"] == "FOUND"),
            None,
        )
        status = "NO_ANOMALY_FOUND" if result.root_cause == "NO_ANOMALY" else "DIAGNOSED"
        return {
            "root_cause": result.root_cause,
            "diagnosis_evidence_ids": result.evidence_ids,
            "playbook_id": result.playbook_id,
            "missing_evidence": missing,
            "diagnosed_version": waybill.get("version") if waybill else None,
            "status": status,
            "confidence_breakdown": {
                "intent": 0.95 if state.get("issue_type") != "UNKNOWN" else 0.2,
                "evidence_completeness": len(found & required) / len(required) if required else 0,
                "evidence_consistency": result.evidence_consistency,
                "historical_match": result.historical_match,
                "playbook_success_rate": 0,
                "final": 0,
            },
            "audit": append_audit(
                state, f"diagnose: root_cause={result.root_cause}, missing={missing}"
            ),
        }

    # 无异常直接结束；其他诊断进入风险与置信度评估。
    def route_after_diagnosis(
        state: RepairState,
    ) -> Literal["assess_decision", "finalize"]:
        return "finalize" if state.get("status") == "NO_ANOMALY_FOUND" else "assess_decision"

    # 计算五维加权分数；证据缺失或无白名单方案优先拒绝，再决定自动执行或审批。
    def assess_decision(state: RepairState) -> dict:
        playbook = get_playbook(state.get("playbook_id"))
        parts = dict(state.get("confidence_breakdown", {}))
        parts["playbook_success_rate"] = playbook.success_rate if playbook else 0
        final = round(
            0.20 * parts.get("intent", 0)
            + 0.25 * parts.get("evidence_completeness", 0)
            + 0.20 * parts.get("evidence_consistency", 0)
            + 0.15 * parts.get("historical_match", 0)
            + 0.20 * parts.get("playbook_success_rate", 0),
            4,
        )
        parts["final"] = final
        reasons: list[str] = []

        # Hard gates override the soft confidence score.
        if state.get("missing_evidence"):
            decision = "REJECT"
            reasons.append(f"关键证据缺失: {state['missing_evidence']}")
        elif playbook is None:
            decision = "REJECT"
            reasons.append("没有经审核的修复 Playbook")
        elif final < 0.90:
            decision = "REJECT"
            reasons.append(f"置信度 {final:.2f} 低于 0.90")
        elif playbook.auto_execute_allowed and playbook.risk_level == "LOW":
            decision = "AUTO_EXECUTE"
            reasons.extend(["证据完整", "低风险 Playbook 允许自动执行"])
        else:
            decision = "NEED_APPROVAL"
            reasons.extend(["证据完整", f"{playbook.risk_level} 风险写操作需要审批"])

        status = "WAITING_APPROVAL" if decision == "NEED_APPROVAL" else "DECIDED"
        return {
            "confidence": final,
            "confidence_breakdown": parts,
            "risk_level": playbook.risk_level if playbook else "HIGH",
            "decision": decision,
            "decision_reasons": reasons,
            "status": status,
            "audit": append_audit(state, f"decision: {decision}, confidence={final}"),
        }

    # 按决策选择人工审批、执行前复核或结束。
    def route_decision(
        state: RepairState,
    ) -> Literal["approval", "revalidate", "finalize"]:
        if state["decision"] == "NEED_APPROVAL":
            return "approval"
        if state["decision"] == "AUTO_EXECUTE":
            return "revalidate"
        return "finalize"

    # 暂停图并等待人工结论；恢复后记录批准状态和审批人。
    def approval(state: RepairState) -> dict:
        response = interrupt({
            "type": "REPAIR_APPROVAL",
            "case_id": state["case_id"],
            "waybill_id": state["waybill_id"],
            "root_cause": state.get("root_cause"),
            "evidence_ids": state.get("diagnosis_evidence_ids", []),
            "playbook_id": state.get("playbook_id"),
            "confidence": state.get("confidence"),
            "confidence_breakdown": state.get("confidence_breakdown"),
            "risk_level": state.get("risk_level"),
            "reasons": state.get("decision_reasons", []),
        })
        approved = bool(response.get("approved"))
        actor = str(response.get("actor_id", "unknown"))
        return {
            "approved": approved,
            "approval_actor": actor,
            "status": "APPROVED" if approved else "REJECTED_BY_HUMAN",
            "audit": append_audit(state, f"approval: approved={approved}, actor={actor}"),
        }

    # 批准后进入复核，拒绝则结束工单。
    def route_approval(state: RepairState) -> Literal["revalidate", "finalize"]:
        return "revalidate" if state.get("approved") else "finalize"

    # 重新读取运单版本，检查审批等待期间状态是否变化；版本变化阻断写操作。
    def revalidate(state: RepairState) -> dict:
        waybill = tools.get_waybill(state["waybill_id"] or "")
        valid = bool(waybill and waybill["version"] == state.get("diagnosed_version"))
        return {
            "status": "PRECONDITIONS_VALID" if valid else "PRECONDITIONS_CHANGED",
            "audit": append_audit(state, f"revalidate: valid={valid}"),
        }

    # 仅当前提复核通过时执行修复，其余情况结束。
    def route_revalidate(state: RepairState) -> Literal["execute", "finalize"]:
        return "execute" if state["status"] == "PRECONDITIONS_VALID" else "finalize"

    # 以工单和 Playbook 组成幂等键，执行修复并记录执行结果。
    def execute(state: RepairState) -> dict:
        result = tools.execute_playbook(
            case_id=state["case_id"], waybill_id=state["waybill_id"] or "",
            playbook_id=state["playbook_id"] or "",
            expected_version=state["diagnosed_version"] or -1,
            idempotency_key=f"{state['case_id']}:{state['playbook_id']}",
        )
        return {
            "execution_result": result,
            "status": "EXECUTED" if result["success"] else (
                "EXECUTION_OUTCOME_UNKNOWN" if result.get("outcome_unknown") else "EXECUTION_FAILED"
            ),
            "audit": append_audit(state, f"execute: {result}"),
        }

    # 执行成功才进入独立验收，失败则结束并保留错误结果。
    def route_execute(state: RepairState) -> Literal["verify", "finalize"]:
        return "verify" if state.get("execution_result", {}).get("success") else "finalize"

    # 通过独立只读查询检查修复后的业务条件，返回逐项检查及总体成功标记。
    def verify(state: RepairState) -> dict:
        result = tools.verify(state["waybill_id"] or "", state["playbook_id"] or "")
        return {
            "verification_result": result,
            "status": "SUCCEEDED" if result["success"] else "VERIFICATION_FAILED",
            "audit": append_audit(state, f"verify: success={result['success']}"),
        }

    # 统一终态：保留明确终态，将策略拒绝转为人工升级、未知类型转为不支持。
    def finalize(state: RepairState) -> dict:
        terminal = {
            "NO_ANOMALY_FOUND", "NEED_MORE_INFORMATION", "REJECTED_BY_HUMAN",
            "PRECONDITIONS_CHANGED", "EXECUTION_FAILED", "EXECUTION_OUTCOME_UNKNOWN", "VERIFICATION_FAILED",
        }
        status = state.get("status", "ESCALATED")
        if status not in terminal and state.get("decision") == "REJECT":
            status = "ESCALATED"
        elif status == "TRIAGED" and state.get("issue_type") == "UNKNOWN":
            status = "UNSUPPORTED_ISSUE"
        return {"status": status, "audit": append_audit(state, f"finalize: {status}")}

    builder = StateGraph(RepairState)
    for name, node in {
        "parse_and_triage": parse_and_triage, "retrieve_knowledge": retrieve_knowledge, "need_info": need_info,
        "plan_investigation": plan_investigation, "execute_read_tool": execute_read_tool,
        "diagnose": diagnose, "assess_decision": assess_decision,
        "approval": approval, "revalidate": revalidate, "execute": execute,
        "verify": verify, "finalize": finalize,
    }.items():
        builder.add_node(name, node)

    builder.add_edge(START, "parse_and_triage")
    builder.add_conditional_edges("parse_and_triage", route_after_triage)
    builder.add_edge("retrieve_knowledge", "plan_investigation")
    builder.add_edge("need_info", END)
    builder.add_conditional_edges("plan_investigation", route_plan)
    builder.add_edge("execute_read_tool", "plan_investigation")
    builder.add_conditional_edges("diagnose", route_after_diagnosis)
    builder.add_conditional_edges("assess_decision", route_decision)
    builder.add_conditional_edges("approval", route_approval)
    builder.add_conditional_edges("revalidate", route_revalidate)
    builder.add_conditional_edges("execute", route_execute)
    builder.add_edge("verify", END)
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())


# 将人工结论以 Command 注入同一工单检查点，继续暂停的工作流。
def resume_after_approval(
    graph: Any, case_id: str, approved: bool, actor_id: str = "demo-approver"
) -> dict:
    return graph.invoke(
        Command(resume={"approved": approved, "actor_id": actor_id}),
        config={"configurable": {"thread_id": case_id}},
    )
