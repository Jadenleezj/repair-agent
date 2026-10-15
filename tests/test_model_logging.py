"""原始模型响应与独立耗时日志的关联、失败、重试和脱敏契约。"""
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

import httpx
from repair_agent.application.logging_setup import configure_logging, LOG_CONTEXT
from repair_agent.agent.reasoning import HttpJsonReasoner
from repair_agent.execution.reliability import StabilitySettings, DependencyGuard, Metrics


class ModelLoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.patch=patch.dict(os.environ, {'REPAIR_LOG_DIR':self.tmp.name,'REPAIR_LLM_API_KEY':'secret-test-key'})
        self.patch.start()
        configure_logging()
        self.token=LOG_CONTEXT.set({'case_id':'CASE-LOG','request_id':'incoming-request'})
        self.reasoner=HttpJsonReasoner('https://model.test/v1','secret-test-key','test-model')
        self.reasoner.client.close()

    def tearDown(self):
        self.reasoner.client.close()
        LOG_CONTEXT.reset(self.token)
        self.patch.stop()
        self.tmp.cleanup()
        configure_logging()

    def records(self,filename):
        return [json.loads(line) for path in Path(self.tmp.name).glob('*/'+filename)
                for line in path.read_text().splitlines()]

    def test_response_is_saved_before_tool_call_validation_fails(self):
        response={'id':'completion-1','choices':[{'finish_reason':'tool_calls','message':{
            'content':None,'tool_calls':[{'id':'call-1','type':'function','function':{
                'name':'GET_ORDER','arguments':'{"order_id":"WRONG","api_key":"unknown-secret"}'}}]}}],
            'usage':{'total_tokens':123},'metadata':{'api_key':'unknown-secret'},
            'text':'secret-test-key'}
        self.reasoner.client=httpx.Client(transport=httpx.MockTransport(
            lambda req:httpx.Response(200,json=response,headers={'x-request-id':'provider-request'})))
        result=self.reasoner.plan_tool_call({'order_id':'W3001','issue_type':'DISPATCH_PROJECTION_STALE'})
        self.assertIsNone(result['pending_tool_call'])
        responses=self.records('model_responses.log')
        self.assertEqual(len(responses),1)
        logged=responses[0]
        self.assertEqual(logged['response']['id'],'completion-1')
        self.assertEqual(logged['response']['usage']['total_tokens'],123)
        self.assertEqual(logged['response']['metadata']['api_key'],'[REDACTED]')
        self.assertNotIn('unknown-secret',json.dumps(logged))
        self.assertNotIn('secret-test-key',json.dumps(logged))
        self.assertEqual(logged['case_id'],'CASE-LOG')
        self.assertEqual(logged['model_request_id'],'provider-request')
        timings=self.records('request_timing.log')
        self.assertEqual([r['record_type'] for r in timings],['llm_attempt','llm_call'])
        self.assertTrue(all(r['llm_call_id']==logged['llm_call_id'] for r in timings))
        self.assertGreaterEqual(timings[0]['duration_ms'],0)
        self.assertEqual(timings[0]['http_status'],200)
        self.assertEqual(timings[1]['attempts'],1)
        self.assertEqual(response['metadata']['api_key'],'unknown-secret')
        self.assertNotIn('completion-1', ''.join(p.read_text() for p in Path(self.tmp.name).glob('*/agent.log')))

    def test_retries_have_separate_attempts_and_one_total(self):
        attempts=0
        def respond(req):
            nonlocal attempts
            attempts+=1
            if attempts==1:raise httpx.ConnectError('TLS connection failed',request=req)
            return httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]})
        self.reasoner.client=httpx.Client(transport=httpx.MockTransport(respond))
        self.reasoner.guard=DependencyGuard(replace(StabilitySettings(),read_retries=1,retry_base_seconds=.001),Metrics())
        self.reasoner._request_message({'model':'test-model','messages':[]})
        records=self.records('request_timing.log')
        self.assertEqual([r['record_type'] for r in records],['llm_attempt','llm_attempt','llm_call'])
        self.assertEqual(records[0]['error_type'],'ConnectError')
        self.assertEqual(records[0]['outcome'],'failed')
        self.assertEqual(records[1]['attempt'],2)
        self.assertEqual(records[2]['attempts'],2)
        self.assertGreaterEqual(records[2]['duration_ms'],sum(r['duration_ms'] for r in records[:2])-.01)

    def test_http_failure_records_status_and_elapsed_time(self):
        self.reasoner.client=httpx.Client(transport=httpx.MockTransport(
            lambda req:httpx.Response(401,json={'error':{'message':'Unauthorized'}})))
        with self.assertRaises(httpx.HTTPStatusError):
            self.reasoner._request_message({'model':'test-model','messages':[]})
        records=self.records('request_timing.log')
        self.assertEqual(records[0]['http_status'],401)
        self.assertEqual(records[0]['error_type'],'HTTPStatusError')
        self.assertEqual(records[1]['outcome'],'failed')
        self.assertEqual(self.records('model_responses.log'),[])

    def test_http_and_task_elapsed_time_are_distinct_and_correlated(self):
        from fastapi.testclient import TestClient
        from repair_agent.application.runtime import RepairRuntime
        from repair_agent.agent.reasoning import RuleReasoner
        from repair_agent.api.app import create_app
        runtime=RepairRuntime(Path(self.tmp.name)/'test.db', reasoner=RuleReasoner())
        client=TestClient(create_app(runtime))
        try:
            response=client.post('/cases?wait=true',json={'message':'已签收但状态没更新'})
            self.assertEqual(response.status_code,201)
            records=self.records('request_timing.log')
            task=next(r for r in records if r['record_type']=='task_run')
            http=next(r for r in records if r['record_type']=='http_request')
            self.assertEqual(task['case_id'],response.json()['case_id'])
            self.assertEqual(task['request_id'],http['request_id'])
            self.assertEqual(http['request_id'],response.headers['X-Request-ID'])
            self.assertEqual(http['http_status'],201)
            self.assertGreaterEqual(http['duration_ms'],task['duration_ms'])
        finally:
            client.close()
            runtime.close()
