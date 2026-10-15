"""聊天页面和静态资源由现有 API 服务提供，不开放项目文件。"""
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from repair_agent.application.runtime import RepairRuntime
from repair_agent.agent.reasoning import RuleReasoner
from repair_agent.api.app import create_app


class FrontendTests(unittest.TestCase):
    def test_chat_page_assets_and_api_share_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime=RepairRuntime(Path(folder)/'test.db',reasoner=RuleReasoner())
            client=TestClient(create_app(runtime))
            try:
                page=client.get('/')
                self.assertEqual(page.status_code,200)
                self.assertIn('text/html',page.headers['content-type'])
                self.assertIn('/assets/app.js',page.text)
                for path in ('app.js','styles.css'):
                    self.assertEqual(client.get('/assets/'+path).status_code,200)
                self.assertEqual(client.get('/assets/../.env').status_code,404)
                result=client.post('/cases?wait=true',json={'message':'订单 W3001 看板显示不一致'})
                self.assertEqual(result.status_code,201)
                case_id=result.json()['case_id']
                self.assertEqual(client.get('/cases/'+case_id).json()['state']['status'],'SUCCEEDED')
                self.assertEqual(client.get('/cases').json()[0]['case_id'],case_id)
            finally:
                client.close()
                runtime.close()
