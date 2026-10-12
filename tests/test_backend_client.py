import tempfile
import unittest
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
from repair_agent.backend_client import RepairBackendClient
from repair_agent.backend_api import create_backend_app
from repair_agent.repository import Repository

class BackendTests(unittest.TestCase):
    # 各测试用独立仓储，防止接口写操作污染已有业务数据。
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.repo=Repository(Path(self.tmp.name)/'backend.db')
        self.args=dict(case_id='CASE-HTTP',waybill_id='W3001',playbook_id='REBUILD_DISPATCH_PROJECTION_V1',expected_version=9,idempotency_key='HTTP-key')
    # 关闭数据库和临时目录。
    def tearDown(self):
        self.repo.conn.close(); self.tmp.cleanup()
    # 验证 HTTP 请求携带固定端点、幂等头和结构化参数，未携带 SQL。
    def test_contract_and_no_sql(self):
        requests=[]
        def respond(request):
            # 捕获修复请求，模拟封装接口返回成功。
            requests.append(request)
            return httpx.Response(200,json={'success':True,'new_version':10})
        client=RepairBackendClient('http://test',transport=httpx.MockTransport(respond))
        try:
            self.assertTrue(client.execute(**self.args)['success'])
            self.assertEqual(requests[0].url.path,'/repairs/dispatch-projection')
            self.assertEqual(requests[0].headers['Idempotency-Key'],'HTTP-key')
            self.assertNotIn(b'SQL',requests[0].content)
        finally: client.close()
    # 验证模拟 HTTP 后端执行后状态变化，且重复调用不会再次修改。
    def test_mock_transport_idempotency(self):
        client=RepairBackendClient.local(self.repo)
        try:
            self.assertTrue(client.execute(**self.args)['success'])
            self.assertTrue(client.execute(**self.args)['idempotent_replay'])
            self.assertEqual(client.read('GET_WAYBILL','W3001')['value']['version'],10)
            conflict=dict(self.args,case_id='OTHER')
            self.assertEqual(client.execute(**conflict)['reason'],'IDEMPOTENCY_KEY_CONFLICT')
            self.assertEqual(client.execute(**dict(self.args,waybill_id='W1001'))['reason'],'IDEMPOTENCY_KEY_CONFLICT')
        finally: client.close()
    # 验证写超时结果不确定，绝不回退为本地修复或自动重试。
    def test_timeout_no_write_fallback(self):
        def timeout(request):
            # 模拟网络超时；不调用任何业务写操作。
            raise httpx.ReadTimeout('timeout',request=request)
        client=RepairBackendClient('http://test',transport=httpx.MockTransport(timeout))
        try:
            self.assertTrue(client.execute(**self.args)['outcome_unknown'])
            self.assertEqual(self.repo.conn.execute("SELECT version FROM demo_waybills WHERE waybill_id='W3001'").fetchone()[0],9)
            self.assertEqual(client.read('GET_WAYBILL','W3001')['status'],'UNAVAILABLE')
        finally: client.close()
    # 验证独立 FastAPI 后端的参数、幂等头及读接口与客户端契约一致。
    def test_backend_api(self):
        client=TestClient(create_backend_app(self.repo))
        payload={k:v for k,v in self.args.items() if k!='playbook_id'}
        self.assertEqual(client.post('/repairs/dispatch-projection',json=payload).status_code,400)
        result=client.post('/repairs/dispatch-projection',json=payload,headers={'Idempotency-Key':'HTTP-key'})
        self.assertTrue(result.json()['success'])
        self.assertEqual(client.get('/evidence/GET_WAYBILL',params={'waybill_id':'W3001'}).json()['value']['status'],'DELIVERED')
        self.assertEqual(client.post('/repairs/dispatch-projection',json=dict(payload,sql='UPDATE x'),headers={'Idempotency-Key':'HTTP-key'}).status_code,422)
    # 验证后端格式异常与 HTTP 错误均记录结果未知，未知方案不发请求。
    def test_invalid_response_and_allowlist(self):
        for response in [httpx.Response(200,json={'success':'yes'}),httpx.Response(503)]:
            client=RepairBackendClient('http://test',transport=httpx.MockTransport(lambda request:response))
            try:
                self.assertTrue(client.execute(**self.args)['outcome_unknown'])
                self.assertEqual(client.execute(**dict(self.args,playbook_id='ARBITRARY_SQL'))['reason'],'PLAYBOOK_NOT_ALLOW_LISTED')
            finally: client.close()
