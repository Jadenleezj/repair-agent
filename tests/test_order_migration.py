"""已有任务的领域命名迁移不能丢失业务状态或恢复位置。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.base import empty_checkpoint
from repair_agent.persistence.repository import Repository


class OrderMigrationTests(unittest.TestCase):
    # 兼容原六列订单表：补齐演示数据且原订单、金额和版本均保持不变。
    def test_six_column_order_table_preserves_existing_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'six-columns.db'
            conn = sqlite3.connect(path)
            conn.execute('CREATE TABLE demo_orders (order_id TEXT PRIMARY KEY,status TEXT NOT NULL,amount INTEGER NOT NULL,version INTEGER NOT NULL,fulfillment_created INTEGER NOT NULL,projection_status TEXT NOT NULL)')
            conn.execute('INSERT INTO demo_orders VALUES (?,?,?,?,?,?)',('W1001','DELIVERING',1350,77,1,'DELIVERING'))
            conn.commit()
            conn.close()
            repo = Repository(path)
            try:
                original = repo.conn.execute("SELECT * FROM demo_orders WHERE order_id='W1001'").fetchone()
                self.assertEqual(original['amount'],1350)
                self.assertEqual(original['version'],77)
                self.assertEqual(original['fulfillment_created'],1)
                self.assertEqual(repo.conn.execute('SELECT count(*) FROM demo_orders').fetchone()[0],5)
                added = repo.conn.execute("SELECT * FROM demo_orders WHERE order_id='W2001'").fetchone()
                self.assertEqual(added['status'],'CREATE')
                self.assertEqual(added['amount'],0)
                self.assertEqual(added['fulfillment_created'],0)
                repo.seed_demo_data()
                self.assertEqual(repo.conn.execute('SELECT count(*) FROM demo_orders').fetchone()[0],5)
            finally:
                repo.conn.close()

    def test_legacy_case_and_checkpoint_survive_rename(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.db'
            conn = sqlite3.connect(path)
            conn.execute('CREATE TABLE repair_cases (case_id TEXT PRIMARY KEY, requester_id TEXT, raw_request TEXT, waybill_id TEXT, status TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)')
            state = {'case_id': 'CASE-OLD', 'waybill_id': 'W1001',
                     'issue_type': 'DELIVERY_TASK_WAYBILL_MISMATCH',
                     'queried_tools': ['GET_WAYBILL'], 'status': 'WAITING_APPROVAL'}
            conn.execute('INSERT INTO repair_cases VALUES (?,?,?,?,?,?,?,?)',
                         ('CASE-OLD', 'user', '运单 W1001', 'W1001', 'WAITING_APPROVAL', json.dumps(state), '', ''))
            conn.commit()
            saver = SqliteSaver(conn)
            checkpoint = empty_checkpoint()
            checkpoint['channel_values'] = state
            checkpoint['channel_versions'] = {'waybill_id': '1'}
            config = saver.put({'configurable': {'thread_id': 'CASE-OLD', 'checkpoint_ns': ''}}, checkpoint, {}, {})
            saver.put_writes(config, [('waybill_id', 'W1001')], 'task-1')
            conn.close()
            repo = Repository(path)
            try:
                self.assertEqual(repo.get_case('CASE-OLD')['state']['order_id'], 'W1001')
                self.assertEqual(repo.get_case('CASE-OLD')['raw_request'], '订单 W1001')
                migrated = SqliteSaver(repo.conn).get_tuple(config)
                self.assertEqual(migrated.checkpoint['channel_values']['queried_tools'], ['GET_ORDER'])
                self.assertIn('order_id', migrated.checkpoint['channel_versions'])
                self.assertEqual(migrated.pending_writes[0][1], 'order_id')
                self.assertEqual(migrated.config, config)
            finally:
                repo.conn.close()
            # 再次打开也能保持数据，迁移不会重复创建或清空工单。
            repo = Repository(path)
            try:
                self.assertEqual(repo.get_case('CASE-OLD')['state']['order_id'], 'W1001')
            finally:
                repo.conn.close()
