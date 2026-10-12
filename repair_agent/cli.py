from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .runtime import RepairRuntime


# 将对象输出为缩进 JSON，保留中文，便于查看诊断与审计。
def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


# 从运行结果提取诊断、历史引用、决策、执行与验收信息，形成 CLI 摘要。
def summary(result: dict[str, Any]) -> dict[str, Any]:
    state = result["state"]
    return {
        "case_id": result["case_id"], "waybill_id": state.get("waybill_id"),
        "issue_type": state.get("issue_type"), "root_cause": state.get("root_cause"),
        "confidence": state.get("confidence"),
        "confidence_breakdown": state.get("confidence_breakdown"),
        "decision": state.get("decision"), "decision_reasons": state.get("decision_reasons"),
        "status": state.get("status"), "execution_result": state.get("execution_result"),
        "verification_result": state.get("verification_result"), "audit": state.get("audit"),
    }


# 解析 CLI 参数，创建工单，处理审批交互并打印最终结果。
def main() -> None:
    parser = argparse.ArgumentParser(description="Delivery state repair LangGraph interview demo")
    parser.add_argument(
        "message", nargs="?",
        default="运单 W1001 线下已签收，但系统还是派送中，阻塞交接作业",
    )
    parser.add_argument("--db", default="data/repair_agent.db")
    parser.add_argument("--requester", default="demo-user")
    parser.add_argument("--reset-demo", action="store_true")
    approval = parser.add_mutually_exclusive_group()
    approval.add_argument("--approve", action="store_true")
    approval.add_argument("--reject", action="store_true")
    args = parser.parse_args()

    runtime = RepairRuntime(Path(args.db))
    if args.reset_demo:
        runtime.repository.reset_demo_data()
    result = runtime.create_case(args.message, args.requester)
    if result["pending_approval"]:
        print("\n=== 待人工审批 ===")
        print_json(result["pending_approval"])
        if args.approve or args.reject:
            approved = args.approve
        else:
            approved = input("\n是否批准修复？[y/N]: ").strip().lower() in {"y", "yes"}
        result = runtime.decide_approval(
            result["case_id"], approved=approved,
            actor_id="cli-approver", comment="CLI decision",
        )
    print("\n=== 最终状态 ===")
    print_json(summary(result))
    runtime.close()


if __name__ == "__main__":
    main()
