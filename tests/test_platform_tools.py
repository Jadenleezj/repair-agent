import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from repair_agent.api.app import create_app
from repair_agent.application.runtime import RepairRuntime
from repair_agent.tools.platforms import PLATFORM_TOOLS

class PlatformTests(unittest.TestCase):
    # 各测试使用独立业务库，不修改已有演示数据。
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = RepairRuntime(Path(self.tmp.name)/'demo.db')
    # 关闭运行时并删除临时文件。
    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()
    # 校验五种工具都可调用，无数据和平台故障保持不同状态。
    def test_five_tools_and_missing(self):
        for tool in PLATFORM_TOOLS:
            self.assertEqual(self.runtime.tools.call_read_tool(tool,'W1001')['status'],'FOUND')
            self.assertEqual(self.runtime.tools.call_read_tool(tool,'W9999')['status'],'NOT_FOUND')
        self.assertEqual(self.runtime.tools.call_read_tool('QUERY_LOG_PLATFORM','W1002')['status'],'UNAVAILABLE')
        self.assertFalse(self.runtime.tools.call_read_tool('QUERY_HIVE_TABLE','W1001')['authoritative'])
    # 校验图执行能完整调查五个平台，且缺少当前关键事件时仍不修数。
    def test_graph_integration_and_hard_gate(self):
        result=self.runtime.create_case('订单 W1002 已签收但仍在派送中')
        self.assertTrue(set(PLATFORM_TOOLS).issubset(result['state']['queried_tools']))
        self.assertEqual(result['status'],'ESCALATED')
    # 线上工具读实际演示库，修复后可看到变化；Hive 历史快照不会同步变化。
    def test_live_online_and_offline_snapshot(self):
        self.runtime.create_case('订单 W3001 已送达但看板显示派送中')
        online=self.runtime.tools.call_read_tool('QUERY_ONLINE_DATA','W3001')
        hive=self.runtime.tools.call_read_tool('QUERY_HIVE_TABLE','W3001')
        self.assertEqual(online['value']['order']['projection_status'],'DELIVERED')
        self.assertEqual(hive['value']['rows'][0]['status'],'DELIVERING')
    # API 列出工具并校验工具名和订单参数，不允许任意工具调用。
    def test_api(self):
        client=TestClient(create_app(self.runtime))
        self.assertEqual(len(client.get('/tools/platforms').json()),5)
        self.assertEqual(client.get('/tools/platforms/QUERY_SCHEDULER',params={'order_id':'W3001'}).json()['value']['last_run']['status'],'FAILED')
        self.assertEqual(client.get('/tools/platforms/DELETE_DATA',params={'order_id':'W1001'}).status_code,422)
