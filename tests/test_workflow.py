import tempfile
import unittest
from pathlib import Path

from repair_agent.runtime import RepairRuntime


class WorkflowTests(unittest.TestCase):
    # 为当前测试创建独立临时数据库及运行环境，避免测试间数据相互污染。
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.runtime = RepairRuntime(self.db_path)

    # 释放测试资源并删除临时目录。
    def tearDown(self):
        self.runtime.close()
        self.temp_dir.cleanup()

    # 验证签收修复先暂停审批，批准后执行并通过验收。
    def test_delivery_completion_pauses_and_resumes(self):
        initial = self.runtime.create_case("运单 W1001 线下已签收，但系统还是派送中")
        self.assertEqual(initial["status"], "WAITING_APPROVAL")
        self.assertIsNotNone(initial["pending_approval"])

        final = self.runtime.decide_approval(
            initial["case_id"], approved=True, actor_id="developer-1"
        )
        self.assertEqual(final["status"], "SUCCEEDED")
        self.assertTrue(final["state"]["verification_result"]["success"])

    # 验证作业事件缺失时硬门禁阻止写操作。
    def test_missing_operation_events_hard_gate_prevents_write(self):
        final = self.runtime.create_case("运单 W1002 线下已签收，但系统还是派送中")
        self.assertEqual(final["status"], "ESCALATED")
        self.assertEqual(final["state"]["decision"], "REJECT")
        self.assertIn("OPERATION_EVENTS", final["state"]["missing_evidence"])

    # 验证用户误报不覆盖权威数据，不会触发不必要修复。
    def test_business_claim_is_checked_against_authoritative_data(self):
        final = self.runtime.create_case("运单 W1003 说是已签收但系统还是派送中")
        self.assertEqual(final["status"], "NO_ANOMALY_FOUND")
        self.assertIsNone(final["state"].get("execution_result"))

    # 验证揽收状态修复被识别为高风险并等待审批。
    def test_pickup_scan_repair_is_high_risk_and_requires_approval(self):
        initial = self.runtime.create_case("运单 W2001 作业员已取件，但运单还是已分配")
        self.assertEqual(initial["status"], "WAITING_APPROVAL")
        self.assertEqual(initial["state"]["risk_level"], "HIGH")

    # 验证低风险看板重建可自动执行并验收。
    def test_low_risk_dispatch_projection_repair_auto_executes(self):
        final = self.runtime.create_case("运单 W3001 已送达，但派送看板显示还在派送中")
        self.assertEqual(final["state"]["decision"], "AUTO_EXECUTE")
        self.assertEqual(final["status"], "SUCCEEDED")

    # 验证缺少运单号时要求补充信息。
    def test_missing_waybill_id_requests_information(self):
        final = self.runtime.create_case("这个件线下已签收，系统还在派送中")
        self.assertEqual(final["status"], "NEED_MORE_INFORMATION")

    # 验证审批期间运单版本改变后拒绝执行旧计划。
    def test_revalidation_detects_change_after_approval(self):
        initial = self.runtime.create_case("运单 W1001 线下已签收，但系统还是派送中")
        self.runtime.repository.bump_waybill_version("W1001")
        final = self.runtime.decide_approval(
            initial["case_id"], approved=True, actor_id="developer-1"
        )
        self.assertEqual(final["status"], "PRECONDITIONS_CHANGED")
        self.assertIsNone(final["state"].get("execution_result"))

    # 验证重建运行时后能从持久化检查点继续审批流程。
    def test_checkpoint_can_resume_after_runtime_restart(self):
        initial = self.runtime.create_case("运单 W1001 线下已签收，但系统还是派送中")
        case_id = initial["case_id"]
        self.runtime.close()
        self.runtime = RepairRuntime(self.db_path)
        final = self.runtime.decide_approval(
            case_id, approved=True, actor_id="developer-after-restart"
        )
        self.assertEqual(final["status"], "SUCCEEDED")

    # 验证相同幂等键返回原结果，不重复改变运单版本。
    def test_repair_tool_is_idempotent(self):
        first = self.runtime.tools.execute_playbook(
            case_id="CASE-IDEMPOTENT", waybill_id="W3001",
            playbook_id="REBUILD_DISPATCH_PROJECTION_V1", expected_version=9,
            idempotency_key="same-key",
        )
        second = self.runtime.tools.execute_playbook(
            case_id="CASE-IDEMPOTENT", waybill_id="W3001",
            playbook_id="REBUILD_DISPATCH_PROJECTION_V1", expected_version=9,
            idempotency_key="same-key",
        )
        self.assertTrue(first["success"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self.runtime.tools.get_waybill("W3001")["version"], 10)


if __name__ == "__main__":
    unittest.main()
