"""原生工具调用协议、任务参数隔离和工作流回传的契约测试。"""
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from repair_agent.agent.reasoning import HttpJsonReasoner
from repair_agent.application.runtime import RepairRuntime
from repair_agent.execution.reliability import CURRENT_RUN, RunControl, RunStopped, StabilitySettings
from dataclasses import replace


def tool_response(name='GET_ORDER', order_id='W3001', call_id='call_1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps({'order_id': order_id})},
    }]}


class ToolCallingTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.message = tool_response()
        self.reasoner = HttpJsonReasoner('http://model.test', 'test-key', 'test-model')
        self.reasoner.client.close()
        def respond(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': self.message}]})
        self.reasoner.client = httpx.Client(transport=httpx.MockTransport(respond))
        self.state = {'order_id': 'W3001', 'issue_type': 'DISPATCH_PROJECTION_STALE',
                      'evidence': [], 'queried_tools': []}

    def tearDown(self):
        self.reasoner.client.close()

    def test_native_tools_and_validated_call(self):
        result = self.reasoner.plan_tool_call(self.state)
        self.assertEqual(result['next_tool'], 'GET_ORDER')
        self.assertEqual(result['pending_tool_call']['id'], 'call_1')
        body = self.requests[0]
        self.assertNotIn('response_format', body)
        self.assertFalse(body['parallel_tool_calls'])
        self.assertEqual(body['tool_choice'], 'auto')
        self.assertTrue(all(t['function']['strict'] for t in body['tools']))
        self.assertNotIn('APPLY_DELIVERY_COMPLETION', [t['function']['name'] for t in body['tools']])
        self.assertEqual(body['tools'][0]['function']['parameters']['required'], ['order_id'])
        self.assertEqual(self.state.get('tool_call_messages'), None)  # 不原地修改图状态。

    def test_no_call_stops_investigation(self):
        self.message = {'role': 'assistant', 'content': '证据已经足够'}
        self.assertIsNone(self.reasoner.plan_tool_call(self.state)['next_tool'])

    def test_invalid_calls_fall_back_without_retaining_model_call(self):
        invalid = [tool_response(order_id='W9999'), tool_response(name='WRITE_SQL'),
                   tool_response(name='GET_DELIVERY_TASK'), tool_response(call_id='')]
        malformed = tool_response()
        malformed['tool_calls'][0]['function']['arguments'] = '{broken'
        invalid.append(malformed)
        multiple = tool_response()
        multiple['tool_calls'].append(tool_response(call_id='call_2')['tool_calls'][0])
        invalid.append(multiple)
        extra = tool_response()
        extra['tool_calls'][0]['function']['arguments'] = json.dumps({'order_id': 'W3001', 'sql': 'UPDATE orders'})
        invalid.append(extra)
        for message in invalid:
            with self.subTest(message=message):
                self.message = message
                result = self.reasoner.plan_tool_call(self.state)
                self.assertEqual(result['next_tool'], 'GET_ORDER')
                self.assertIsNone(result['pending_tool_call'])
                self.assertEqual(result['tool_call_messages'], [])
        self.message = tool_response()
        result = self.reasoner.plan_tool_call(dict(self.state, queried_tools=['GET_ORDER']))
        self.assertEqual(result['next_tool'], 'GET_DISPATCH_PROJECTION')
        self.assertIsNone(result['pending_tool_call'])

    def test_unsupported_endpoint_falls_back(self):
        self.reasoner.client.close()
        self.reasoner.client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "tools unsupported"})))
        result = self.reasoner.plan_tool_call(self.state)
        self.assertEqual(result["next_tool"], "GET_ORDER")
        self.assertIsNone(result["pending_tool_call"])

    def test_tool_call_counts_against_model_budget(self):
        control = RunControl(StabilitySettings())
        token = CURRENT_RUN.set(control)
        try:
            result = self.reasoner.plan_tool_call(self.state)
            self.assertIsNotNone(result["pending_tool_call"])
            self.assertEqual(control.llm_calls, 1)
            self.assertGreater(control.tokens, 0)
            self.assertLessEqual(self.requests[0]["max_tokens"], 4096)
        finally:
            CURRENT_RUN.reset(token)

    def test_budget_exhaustion_is_not_swallowed(self):
        token = CURRENT_RUN.set(RunControl(replace(StabilitySettings(), token_budget=10)))
        try:
            with self.assertRaises(RunStopped):
                self.reasoner.plan_tool_call(self.state)
            self.assertEqual(self.requests, [])
        finally:
            CURRENT_RUN.reset(token)

    def test_workflow_returns_tool_result_and_persists_messages(self):
        self.reasoner.triage = self.reasoner.fallback.triage
        self.reasoner.diagnose = self.reasoner.fallback.diagnose
        def respond(request):
            body = json.loads(request.content)
            self.requests.append(body)
            round_number = len(self.requests)
            if round_number == 1:
                message = tool_response()
            elif round_number == 2:
                message = tool_response('GET_DISPATCH_PROJECTION', call_id='call_2')
            else:
                message = {'role': 'assistant', 'content': '证据已足够'}
            return httpx.Response(200, json={'choices': [{'message': message}]})
        self.reasoner.client.close()
        self.reasoner.client = httpx.Client(transport=httpx.MockTransport(respond))
        with tempfile.TemporaryDirectory() as folder:
            runtime = RepairRuntime(Path(folder) / 'test.db', reasoner=self.reasoner)
            try:
                result = runtime.create_case('订单 W3001 已送达，但看板显示派送中')
                self.assertEqual(result['status'], 'SUCCEEDED')
                history = result['state']['tool_call_messages']
                self.assertEqual([m['role'] for m in history], ['assistant', 'tool', 'assistant', 'tool'])
                self.assertEqual(history[1]['tool_call_id'], 'call_1')
                self.assertEqual(json.loads(history[1]['content'])['fact_type'], 'ORDER_SNAPSHOT')
                self.assertEqual(self.requests[1]['messages'][2:4], history[:2])
                self.assertEqual(runtime.current_state(result['case_id'])['tool_call_messages'], history)
                self.assertIsNone(result['state']['pending_tool_call'])
            finally:
                runtime.close()
