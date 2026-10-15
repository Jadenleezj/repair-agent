"""DeepSeek Chat Completions 请求格式及完整调查闭环，不调用外部 API。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from repair_agent.agent.reasoning import HttpJsonReasoner, reasoner_from_env
from repair_agent.agent.adapters import create_reasoner
from repair_agent.agent.adapters.deepseek import DeepSeekReasoner
from repair_agent.application.runtime import RepairRuntime


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.message = {'content': json.dumps({
            'order_id': 'W3001', 'issue_type': 'DISPATCH_PROJECTION_STALE',
            'claims': ['看板未同步'], 'missing_information': []})}
        self.reasoner = DeepSeekReasoner('https://api.deepseek.com', 'test-deepseek-key', 'deepseek-flash')
        self.reasoner.client.close()
        def respond(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': self.message}]})
        self.reasoner.client = httpx.Client(transport=httpx.MockTransport(respond))

    def tearDown(self):
        self.reasoner.client.close()

    def test_json_output_with_schema_prompt_and_local_validation(self):
        result = self.reasoner.triage('订单 W3001 看板未同步')
        self.assertEqual(result.order_id, 'W3001')
        body = self.requests[0]
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertIn('JSON Schema', body['messages'][0]['content'])
        self.assertIn('EXAMPLE JSON OUTPUT', body['messages'][0]['content'])
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual(body['max_tokens'], 4096)
        # JSON 合法但类型错误仍会被拒绝，回退规则而非信任远程内容。
        self.message = {'content': '{"order_id":"W9999","issue_type":"WRITE_SQL","claims":[]}'}
        self.assertEqual(self.reasoner.triage('订单 W3001 看板未同步').order_id, 'W3001')

    def test_standard_tool_calls_without_beta_or_openai_only_parameters(self):
        self.message = {'content': None, 'tool_calls': [{'id': 'call_ds', 'type': 'function',
            'function': {'name': 'GET_ORDER', 'arguments': '{"order_id":"W3001"}'}}]}
        result = self.reasoner.plan_tool_call({'order_id': 'W3001', 'issue_type': 'DISPATCH_PROJECTION_STALE'})
        self.assertEqual(result['next_tool'], 'GET_ORDER')
        body = self.requests[0]
        self.assertNotIn('parallel_tool_calls', body)
        self.assertNotIn('response_format', body)
        self.assertTrue(all('strict' not in t['function'] for t in body['tools']))
        self.assertEqual(body['thinking'], {'type': 'disabled'})

    def test_openai_adapter_keeps_original_request_format(self):
        client = HttpJsonReasoner('https://api.openai.com/v1', 'test-openai-key', 'test-model')
        client.client.close()
        client.client = httpx.Client(transport=httpx.MockTransport(
            lambda request: (self.requests.append(json.loads(request.content)) or
                             httpx.Response(200, json={'choices': [{'message': self.message}]}))))
        try:
            client.triage('订单 W3001 看板未同步')
            body = self.requests[-1]
            self.assertEqual(body['response_format']['type'], 'json_schema')
            self.assertTrue(body['response_format']['json_schema']['strict'])
            self.assertNotIn('thinking', body)
            self.message = {'content': 'No more tools.'}
            client.plan_tool_call({'order_id': 'W3001', 'issue_type': 'DISPATCH_PROJECTION_STALE'})
            body = self.requests[-1]
            self.assertFalse(body['parallel_tool_calls'])
            self.assertTrue(all(t['function']['strict'] for t in body['tools']))
            self.assertNotIn('thinking', body)
        finally:
            client.client.close()

    def test_deepseek_adapter_does_not_mutate_shared_request(self):
        body = {'model': 'deepseek-flash', 'messages': [], 'parallel_tool_calls': False,
                'tools': [{'type': 'function', 'function': {'name': 'GET_ORDER', 'strict': True}}]}
        before = json.loads(json.dumps(body))
        self.reasoner._request_message(body)
        self.assertEqual(body, before)
        self.assertNotIn('strict', self.requests[0]['tools'][0]['function'])

    def test_provider_detection_and_gateway_override(self):
        for url, provider, expected in [('https://api.deepseek.com/v1', None, DeepSeekReasoner),
                                         ('https://gateway.test/v1', 'deepseek', DeepSeekReasoner),
                                         ('https://api.deepseek.com.evil.test', None, HttpJsonReasoner)]:
            with self.subTest(url=url):
                client = create_reasoner(url, 'test', 'model', provider=provider)
                try:self.assertIs(type(client), expected)
                finally:client.client.close()
        with self.assertRaises(ValueError):
            create_reasoner('https://gateway.test', 'test', 'model', provider='invalid')
        with patch('repair_agent.agent.reasoning.load_local_config'), patch.dict(os.environ, {
            'REPAIR_LLM_BASE_URL':'https://gateway.test/v1','REPAIR_LLM_API_KEY':'test',
            'REPAIR_LLM_MODEL':'deepseek-flash','REPAIR_LLM_PROVIDER':'deepseek'}):
            client=reasoner_from_env()
            try:self.assertIs(type(client),DeepSeekReasoner)
            finally:client.client.close()

    def test_complete_workflow_with_json_and_native_tool_messages(self):
        planning_round = 0
        def respond(request):
            nonlocal planning_round
            body = json.loads(request.content)
            self.requests.append(body)
            if 'tools' in body:
                planning_round += 1
                if planning_round <= 2:
                    name = 'GET_ORDER' if planning_round == 1 else 'GET_DISPATCH_PROJECTION'
                    message = {'content': None, 'tool_calls': [{
                        'id': f'call_ds_{planning_round}', 'type': 'function',
                        'function': {'name': name, 'arguments': '{"order_id":"W3001"}'}}]}
                else:
                    self.assertEqual(body['messages'][-1]['role'], 'tool')
                    message = {'content': 'Evidence is complete.'}
            elif 'diagnosisresult' in body['messages'][0]['content'].lower():
                message = {'content': json.dumps({
                    'root_cause': 'DISPATCH_PROJECTION_OUT_OF_SYNC',
                    'evidence_ids': ['EV-GET_ORDER','EV-GET_DISPATCH_PROJECTION'],
                    'playbook_id':'REBUILD_DISPATCH_PROJECTION_V1',
                    'historical_match':0.98,'evidence_consistency':1.0})}
            else:
                message = self.message
            return httpx.Response(200, json={'choices':[{'message':message,'finish_reason':'stop'}],
                                            'usage':{'total_tokens':200}})
        self.reasoner.client.close()
        self.reasoner.client = httpx.Client(transport=httpx.MockTransport(respond))
        with tempfile.TemporaryDirectory() as folder:
            runtime=RepairRuntime(Path(folder)/'test.db', reasoner=self.reasoner)
            try:
                result=runtime.create_case('订单 W3001 看板未同步')
                self.assertEqual(result['status'],'SUCCEEDED')
                self.assertEqual(result['state']['root_cause'],'DISPATCH_PROJECTION_OUT_OF_SYNC')
                self.assertEqual(len(result['state']['tool_call_messages']),4)
                self.assertEqual(len(self.requests),5)
            finally:runtime.close()
