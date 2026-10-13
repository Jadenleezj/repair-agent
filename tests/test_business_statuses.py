"""验证两类业务状态、任务聚合、接单语义与旧演示状态迁移。"""
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi.testclient import TestClient

from repair_agent.api.app import create_app
from repair_agent.tools.backend_client import RepairBackendClient
from repair_agent.domain.statuses import OrderStatus, DeliveryTaskStatus
from repair_agent.application.runtime import RepairRuntime
from repair_agent.persistence.repository import Repository


class BusinessStatusTests(unittest.TestCase):
    # 独立模拟数据库用于业务状态检查，不修改当前项目数据。
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'statuses.db'
        self.runtime = RepairRuntime(self.path)

    # 关闭检查点与客户端，再删除模拟数据库。
    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    # 两类状态集合完全独立，API 展示名称与业务含义。
    def test_status_catalog(self):
        self.assertEqual({s.value for s in OrderStatus}, {'CREATE','DELIVERING','ON_HOLD','DELIVERED','RETURNED'})
        self.assertEqual({s.value for s in DeliveryTaskStatus}, {'CREATE','PENDING_ASSIGN','ASSIGNED','COMPLETE'})
        with TestClient(create_app(self.runtime)) as client:
            catalog = client.get('/statuses').json()
            self.assertEqual(len(catalog['order']),5)
            self.assertEqual(len(catalog['delivery_task']),4)
            self.assertEqual(catalog['order'][2]['name'],'On Hold')

    # 接单记录确认任务 Assigned，修复仍为 Create 的订单。
    def test_task_acceptance_repairs_create_to_delivering(self):
        result = self.runtime.create_case('订单 W2001 骑手已经接单，但订单还是刚创建')
        self.assertEqual(result['status'],'WAITING_APPROVAL')
        final = self.runtime.decide_approval(result['case_id'],approved=True,actor_id='tester')
        self.assertEqual(final['status'],'SUCCEEDED')
        self.assertEqual(self.runtime.tools.get_order('W2001')['status'],'DELIVERING')
        task = self.runtime.tools.call_read_tool('GET_DELIVERY_TASK','W2001')['value']
        self.assertEqual(task['task_status'],'ASSIGNED')

    # 一个任务含多张订单：部分完成时保留 Assigned，全部送达才 Complete。
    def test_task_completes_only_after_last_order(self):
        repo = self.runtime.repository
        with repo.lock, repo.conn:
            repo.conn.execute("UPDATE demo_delivery_tasks SET task_id='DT-GROUP',task_status='ASSIGNED' WHERE order_id IN ('W1001','W1003')")
            repo.conn.execute("INSERT INTO demo_orders VALUES ('W1004','DELIVERING',1,'DELIVERING')")
            repo.conn.execute("INSERT INTO demo_delivery_tasks VALUES ('W1004','ASSIGNED','COURIER-18','DT-GROUP')")
            repo.conn.execute("INSERT INTO demo_operation_events VALUES ('W1004','AVAILABLE',?)",
                              (json.dumps([{'event':'PROOF_OF_DELIVERY_SCAN','result':'SUCCESS'}]),))
        first = self.runtime.create_case('订单 W1001 已签收但仍在派送中')
        self.assertEqual(self.runtime.decide_approval(first['case_id'],approved=True,actor_id='tester')['status'],'SUCCEEDED')
        task = self.runtime.tools.call_read_tool('GET_DELIVERY_TASK','W1001')['value']
        self.assertEqual(task['task_status'],'ASSIGNED')
        self.assertFalse(task['all_orders_delivered'])
        self.assertEqual(self.runtime.create_case('订单 W1001 已签收但仍在派送中')['status'],'NO_ANOMALY_FOUND')
        last = self.runtime.create_case('订单 W1004 已签收但仍在派送中')
        self.assertEqual(self.runtime.decide_approval(last['case_id'],approved=True,actor_id='tester')['status'],'SUCCEEDED')
        task = self.runtime.tools.call_read_tool('GET_DELIVERY_TASK','W1001')['value']
        self.assertEqual(task['task_status'],'COMPLETE')
        self.assertTrue(task['all_orders_delivered'])
        self.assertEqual(set(task['member_order_ids']),{'W1001','W1003','W1004'})

    # On Hold 与 Returned 不会被签收或接单方案直接覆盖。
    def test_hold_and_returned_block_status_repairs(self):
        repo = self.runtime.repository
        for status in ['ON_HOLD','RETURNED']:
            with repo.lock, repo.conn:
                repo.conn.execute('UPDATE demo_orders SET status=?,projection_status=? WHERE order_id=?',(status,status,'W1001'))
            case = self.runtime.create_case('订单 W1001 已签收但仍在派送中')
            self.assertEqual(case['status'],'ESCALATED')
            result = self.runtime.tools.execute_playbook(case_id='CASE-'+status,order_id='W1001',
                playbook_id='APPLY_DELIVERY_COMPLETION_V1',expected_version=7,idempotency_key=status)
            self.assertFalse(result['success'])
            self.assertEqual(self.runtime.tools.get_order('W1001')['status'],status)

    # 等待接任务时不能把订单认作骑手已接单。
    def test_pending_assign_does_not_repair_acceptance(self):
        repo = self.runtime.repository
        with repo.lock, repo.conn:
            repo.conn.execute("UPDATE demo_delivery_tasks SET task_status='PENDING_ASSIGN' WHERE order_id='W2001'")
        result = self.runtime.create_case('订单 W2001 骑手已接单但状态仍刚创建')
        self.assertEqual(result['status'],'ESCALATED')
        self.assertEqual(self.runtime.tools.get_order('W2001')['status'],'CREATE')

    # 外部证据中的未知业务状态不能进入修复诊断。
    def test_invalid_upstream_status_is_unavailable(self):
        evidence = {'evidence_id':'EV-T','source':'test','fact_type':'DELIVERY_TASK',
                    'value':{'task_status':'DELIVERED'},'status':'FOUND','authoritative':True}
        client = RepairBackendClient('http://test',transport=httpx.MockTransport(
            lambda request: httpx.Response(200,json=evidence)))
        try:
            self.assertEqual(client.read('GET_DELIVERY_TASK','W1001')['status'],'UNAVAILABLE')
        finally: client.close()

    # 旧状态迁移保留已有工单，同时补齐任务标识和接单事件。
    def test_old_demo_state_migration_preserves_cases(self):
        case_id = self.runtime.create_case('订单 W1003 已签收')['case_id']
        repo = self.runtime.repository
        with repo.lock, repo.conn:
            repo.conn.execute("UPDATE demo_orders SET status='ASSIGNED',projection_status='ASSIGNED' WHERE order_id='W2001'")
            repo.conn.execute("UPDATE demo_delivery_tasks SET task_status='DELIVERING' WHERE order_id='W2001'")
            repo.conn.execute("UPDATE demo_delivery_tasks SET task_status='DELIVERED' WHERE order_id='W1003'")
            repo.conn.execute('UPDATE demo_operation_events SET records_json=? WHERE order_id=?',
                              (json.dumps([{'event':'PICKUP_SCAN','result':'SUCCESS'}]),'W2001'))
            repo.conn.execute('ALTER TABLE demo_delivery_tasks DROP COLUMN task_id')
        self.runtime.close()
        self.runtime = RepairRuntime(self.path)
        self.assertIsNotNone(self.runtime.get_case(case_id))
        self.assertEqual(self.runtime.tools.get_order('W2001')['status'],'CREATE')
        task = self.runtime.tools.call_read_tool('GET_DELIVERY_TASK','W2001')['value']
        self.assertEqual(task['task_status'],'ASSIGNED')
        self.assertEqual(task['task_id'],'DT-W2001')
        events = self.runtime.tools.call_read_tool('QUERY_OPERATION_EVENTS','W2001')['value']
        self.assertEqual(events[0]['event'],'TASK_ACCEPTED')

    # 两个独立 Worker 同时修复相同旧版本，只有一个可以修改订单与任务聚合。
    def test_concurrent_repairs_revalidate_inside_transaction(self):
        repositories = [Repository(self.path),Repository(self.path)]
        clients = [RepairBackendClient.local(repo) for repo in repositories]
        # 每个并发请求使用不同幂等键，但携带同一个预期版本。
        def repair(index):
            return clients[index].execute(case_id=f'CASE-CONCURRENT-{index}',order_id='W1001',
                playbook_id='APPLY_DELIVERY_COMPLETION_V1',expected_version=7,idempotency_key=f'concurrent-{index}')
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(repair,range(2)))
            self.assertEqual(sum(result['success'] for result in results),1)
            self.assertEqual([r['reason'] for r in results if not r['success']],['ORDER_VERSION_CHANGED'])
            self.assertEqual(self.runtime.tools.get_order('W1001')['version'],8)
        finally:
            for client in clients: client.close()
            for repo in repositories: repo.conn.close()
