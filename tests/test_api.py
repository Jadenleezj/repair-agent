import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from repair_agent.api import create_app
from repair_agent.runtime import RepairRuntime


class ApiTests(unittest.TestCase):
    # 为当前测试创建独立临时数据库及运行环境，避免测试间数据相互污染。
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = RepairRuntime(Path(self.temp_dir.name) / "api.db")
        self.client = TestClient(create_app(self.runtime))

    # 释放测试资源并删除临时目录。
    def tearDown(self):
        self.client.close()
        self.runtime.close()
        self.temp_dir.cleanup()

    # 验证 API 创建工单、审批修复和读取审计时间线的完整链路。
    def test_create_approve_and_read_timeline(self):
        created = self.client.post("/cases", json={
            "message": "运单 W1001 线下已签收，但系统还是派送中",
            "requester_id": "business-user",
        })
        self.assertEqual(created.status_code, 201)
        case_id = created.json()["case_id"]
        self.assertEqual(created.json()["status"], "WAITING_APPROVAL")

        approved = self.client.post(f"/cases/{case_id}/approval", json={
            "actor_id": "developer-1", "approved": True, "comment": "evidence checked"
        })
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["status"], "SUCCEEDED")

        timeline = self.client.get(f"/cases/{case_id}/timeline")
        self.assertEqual(timeline.status_code, 200)
        event_types = [item["event_type"] for item in timeline.json()]
        self.assertIn("APPROVAL_DECIDED", event_types)

    # 验证已处理工单不能再次审批。
    def test_duplicate_approval_is_rejected(self):
        created = self.client.post("/cases", json={"message": "运单 W1001 线下已签收，但系统还是派送中"})
        case_id = created.json()["case_id"]
        payload = {"actor_id": "developer-1", "approved": False}
        self.assertEqual(self.client.post(f"/cases/{case_id}/approval", json=payload).status_code, 200)
        self.assertEqual(self.client.post(f"/cases/{case_id}/approval", json=payload).status_code, 409)


if __name__ == "__main__":
    unittest.main()
