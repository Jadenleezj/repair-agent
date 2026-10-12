"""故障注入验证：队列、租约、取消、预算、接口响应丢失和输出归档。"""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from repair_agent.api import create_app
from repair_agent.runtime import RepairRuntime
from repair_agent.reliability import StabilitySettings, RunControl, RunStopped, Metrics, DependencyGuard, CURRENT_RUN
from repair_agent.reasoning import RuleReasoner
from repair_agent.jobs import QueueFull

class StabilityTests(unittest.TestCase):
    # 短心跳用于测试租约，不使用真实平台或模型凭据。
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.settings=replace(StabilitySettings(),workers=2,poll_seconds=.01,heartbeat_seconds=.1,lease_seconds=1,shutdown_seconds=1)
        self.runtime=RepairRuntime(Path(self.tmp.name)/'test.db',settings=self.settings)
    # 停止消费者后关闭连接，避免背景线程访问已释放仓储。
    def tearDown(self):
        self.runtime.close();self.tmp.cleanup()
    # 有限等待异步工单状态，不允许测试无限挂起。
    def await_case(self,case_id,status,seconds=4):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            case=self.runtime.get_case(case_id)
            if case['status'] in status:return case
            time.sleep(.01)
        self.fail(f"case did not reach {status}: {self.runtime.get_case(case_id)}")
    # API 入队与审批均立即返回，等待人工期间没有活跃执行任务。
    def test_async_api_and_approval(self):
        with TestClient(create_app(self.runtime)) as client:
            self.assertEqual(client.get('/ready').status_code,200)
            response=client.post('/cases',json={'message':'运单 W1001 已签收但派送中'})
            self.assertEqual(response.status_code,202)
            case_id=response.json()['case_id']
            self.await_case(case_id,{'WAITING_APPROVAL'})
            self.assertEqual(self.runtime.jobs.get(case_id)['status'],'WAITING_APPROVAL')
            approval=client.post(f'/cases/{case_id}/approval',json={'approved':True,'actor_id':'tester'})
            self.assertEqual(approval.status_code,202)
            self.await_case(case_id,{'SUCCEEDED'})
            self.assertEqual(client.get('/metrics').json()['jobs']['FINISHED'],1)
            cancelled=client.post(f'/cases/{case_id}/cancel').json()
            self.assertNotIn('owner',cancelled)
            self.assertNotIn('payload',cancelled)
    # 满队列拒绝提交，未启动 Worker 时任务持久化，重建运行时后可消费。
    def test_backpressure_restart_and_cancel(self):
        self.runtime.settings=replace(self.settings,queue_capacity=1)
        self.runtime.jobs.settings=self.runtime.settings
        first=self.runtime.submit_case('运单 W3001 看板显示派送中')
        with self.assertRaises(QueueFull):self.runtime.submit_case('运单 W1001 已签收')
        self.runtime.close()
        self.runtime=RepairRuntime(Path(self.tmp.name)/'test.db',settings=self.settings)
        self.runtime.start_workers()
        self.await_case(first['case_id'],{'SUCCEEDED'})
        self.runtime.workers.stop()
        self.runtime.workers=None
        second=self.runtime.jobs.submit('运单 W1001 已签收','demo')
        self.runtime.jobs.cancel(second['case_id'])
        self.assertEqual(self.runtime.get_case(second['case_id'])['status'],'CANCELLED')
    # 过期租约被新 owner 领取，旧 owner 无法保存结果；写阶段只转对账。
    def test_lease_fencing_and_write_recovery(self):
        task=self.runtime.submit_case('运单 W1001 已签收')
        old=self.runtime.jobs.claim()
        with self.runtime.jobs.connect() as conn:
            conn.execute('UPDATE repair_jobs SET lease_until=0 WHERE case_id=?',(task['case_id'],))
        self.runtime.jobs.recover();new=self.runtime.jobs.claim()
        self.assertNotEqual(old['owner'],new['owner'])
        self.assertFalse(self.runtime.jobs.finish(old,{'status':'SUCCEEDED'}))
        intent={'case_id':new['case_id'],'waybill_id':'W1001','playbook_id':'APPLY_DELIVERY_COMPLETION_V1','diagnosed_version':7}
        self.runtime.jobs.begin_write(new,intent)
        with self.runtime.jobs.connect() as conn:conn.execute('UPDATE repair_jobs SET lease_until=0')
        self.runtime.jobs.recover()
        self.assertEqual(self.runtime.jobs.get(new['case_id'])['kind'],'RECONCILE')
        self.assertEqual(self.runtime.get_case(new['case_id'])['status'],'EXECUTION_OUTCOME_UNKNOWN')
    # 推理入口超限前终止，慢节点返回后也不能继续修复。
    def test_budget_and_timeout(self):
        self.runtime.close()
        self.runtime=RepairRuntime(Path(self.tmp.name)/'test.db',settings=replace(self.settings,model_calls=1))
        self.runtime.start_workers()
        result=self.runtime.submit_case('运单 W1001 已签收')
        case=self.await_case(result['case_id'],{'MODEL_BUDGET_EXCEEDED'})
        self.assertIsNone(case['state'].get('execution_result'))
        self.runtime.close()
        class SlowReasoner(RuleReasoner):
            # 模拟慢查询入口，节点结束后会检测总任务超时。
            def triage(self,text):
                time.sleep(.06);return super().triage(text)
        self.runtime=RepairRuntime(Path(self.tmp.name)/'slow.db',settings=replace(self.settings,task_seconds=.02),worker_reasoner_factory=SlowReasoner)
        self.runtime.start_workers();result=self.runtime.submit_case('运单 W3001 看板显示派送中')
        self.await_case(result['case_id'],{'TIMED_OUT'})
    # 后端已提交但响应丢失，对账通过原幂等键确认，版本只增加一次。
    def test_unknown_outcome_reconciliation(self):
        initial=self.runtime.create_case('运单 W1001 已签收但派送中')
        state=initial['state']
        result=self.runtime.tools.execute_playbook(case_id=initial['case_id'],waybill_id='W1001',playbook_id=state['playbook_id'],expected_version=7,idempotency_key=f"{initial['case_id']}:{state['playbook_id']}")
        self.assertTrue(result['success'])
        reconciled=self.runtime.reconcile(initial['case_id'],state)
        self.assertEqual(reconciled['status'],'SUCCEEDED')
        self.assertEqual(self.runtime.tools.get_waybill('W1001')['version'],8)
    # 大输出保存原文并降为不可用，不能把截断证据用于自动修复门禁。
    def test_output_archive_and_retention(self):
        evidence={'evidence_id':'EV-X','source':'test','fact_type':'WAYBILL_SNAPSHOT','status':'FOUND','authoritative':True,'value':{'data':'x'*40000}}
        bounded=self.runtime.artifacts.bound(evidence)
        self.assertEqual(bounded['status'],'UNAVAILABLE')
        self.assertTrue(bounded['value']['truncated'])
        key=bounded['value']['artifact_id']
        self.assertEqual(self.runtime.artifacts.read(key),evidence)
        with self.assertRaises(ValueError):self.runtime.artifacts.read('../bad')
    # 暂态只读请求有限重试，熔断阻止持续打击故障平台，预算与取消穿透回退。
    def test_retry_breaker_cancel_and_token_budget(self):
        settings=replace(self.settings,read_retries=1,retry_base_seconds=.001,breaker_failures=2)
        metrics=Metrics();guard=DependencyGuard(settings,metrics)
        calls=[]
        def fail():
            calls.append(1);raise httpx.ReadTimeout('test')
        with self.assertRaises(httpx.ReadTimeout):guard.call('hive',fail,1)
        from repair_agent.reliability import DependencyUnavailable
        with self.assertRaises(DependencyUnavailable):guard.call('hive',fail,1)
        self.assertEqual(len(calls),2)
        control=RunControl(settings,cancelled=lambda:True)
        with self.assertRaises(RunStopped):control.check()
        control=RunControl(replace(settings,token_budget=10))
        with self.assertRaises(RunStopped):control.consume(11)

    # 真正经过队列与 Worker 的响应丢失注入：自动对账确认，不重复调用修复接口。
    def test_worker_reconciles_lost_response_without_repair_retry(self):
        from repair_agent.backend_client import RepairBackendClient
        original=RepairBackendClient.execute
        writes=[]
        def lost(client,**kwargs):
            # 后端完成事务后模拟调用方未收到成功响应。
            result=original(client,**kwargs)
            writes.append(kwargs['idempotency_key'])
            return {'success':False,'outcome_unknown':True,'reason':'BACKEND_OUTCOME_UNKNOWN'}
        with patch.object(RepairBackendClient,'execute',lost):
            self.runtime.start_workers()
            result=self.runtime.submit_case('运单 W3001 已送达但看板显示派送中')
            self.await_case(result['case_id'],{'SUCCEEDED'})
        self.assertEqual(len(writes),1)
        self.assertEqual(self.runtime.tools.get_waybill('W3001')['version'],10)
        self.assertEqual(self.runtime.jobs.get(result['case_id'])['kind'],'RECONCILE')

    # 运行中取消在慢节点返回后生效，后续调查与修复不会继续。
    def test_running_cancellation(self):
        class Slow(RuleReasoner):
            # 可控延迟用于给取消接口留出测试窗口。
            def triage(self,text):time.sleep(.15);return super().triage(text)
        self.runtime.close()
        self.runtime=RepairRuntime(Path(self.tmp.name)/'cancel.db',settings=self.settings,worker_reasoner_factory=Slow)
        self.runtime.start_workers()
        result=self.runtime.submit_case('运单 W3001 已送达但看板显示派送中')
        time.sleep(.04)
        self.runtime.jobs.cancel(result['case_id'])
        self.await_case(result['case_id'],{'CANCELLED'})
        self.assertEqual(self.runtime.tools.get_waybill('W3001')['version'],9)

    # 多个工单持续消费，审批等待不占槽位，统计标签数量保持有界。
    def test_short_continuous_run_and_concurrency(self):
        self.runtime.start_workers()
        ids=[self.runtime.submit_case('运单 W1003 已签收但仍在派送中')['case_id'] for _ in range(20)]
        for case_id in ids:self.await_case(case_id,{'NO_ANOMALY_FOUND'},seconds=8)
        self.assertEqual(self.runtime.jobs.stats()['jobs'].get('RUNNING',0),0)
        self.assertLess(len(self.runtime.metrics.snapshot()['counters']),100)
        self.assertTrue(self.runtime.workers.ready())

    # 停机不能等待慢节点无限返回；租约被撤销，旧任务无法继续执行写操作。
    def test_shutdown_retires_inflight(self):
        class Slow(RuleReasoner):
            # 超过停机宽限但最终会返回，验证执行权撤销。
            def triage(self,text):time.sleep(.2);return super().triage(text)
        self.runtime.close()
        self.runtime=RepairRuntime(Path(self.tmp.name)/'stop.db',settings=replace(self.settings,shutdown_seconds=.02),worker_reasoner_factory=Slow)
        self.runtime.start_workers()
        result=self.runtime.submit_case('运单 W3001 看板显示派送中')
        time.sleep(.04)
        self.runtime.workers.stop()
        self.assertFalse(self.runtime.workers.ready())
        time.sleep(.25)
        self.assertEqual(self.runtime.tools.get_waybill('W3001')['version'],9)
        self.assertEqual(self.runtime.jobs.get(result['case_id'])['status'],'PENDING')

    # 保留期清理删除终结工单与检查点，等待审批工单继续保留。
    def test_terminal_retention_keeps_pending_approval(self):
        self.runtime.start_workers()
        terminal=self.runtime.submit_case('运单 W1003 已签收')['case_id']
        waiting=self.runtime.submit_case('运单 W1001 已签收')['case_id']
        self.await_case(terminal,{'NO_ANOMALY_FOUND'});self.await_case(waiting,{'WAITING_APPROVAL'})
        with self.runtime.jobs.connect() as conn:
            conn.execute('UPDATE repair_jobs SET updated_at=0')
        self.runtime.maintenance()
        self.assertIsNone(self.runtime.get_case(terminal))
        self.assertIsNotNone(self.runtime.get_case(waiting))
        self.assertEqual(self.runtime.current_state(terminal),{})

    # 模型预算耗尽必须终止，不能被通用异常处理转成规则回退继续修复。
    def test_llm_budget_propagates_and_caps_output(self):
        from repair_agent.reasoning import HttpJsonReasoner
        requests=[]
        # 捕获输出上限，返回可校验的结构化 triage 内容。
        def respond(request):
            requests.append(json.loads(request.content))
            content={'waybill_id':'W1001','issue_type':'DELIVERY_TASK_WAYBILL_MISMATCH',
                     'claims':[],'missing_information':[]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(content)}}],
                                            'usage':{'total_tokens':200}})
        reasoner=HttpJsonReasoner('http://test','test','model')
        reasoner.client.close()
        reasoner.client=httpx.Client(transport=httpx.MockTransport(respond))
        try:
            token=CURRENT_RUN.set(RunControl(replace(self.settings,token_budget=10)))
            try:
                with self.assertRaises(RunStopped):reasoner.triage('运单 W1001 已签收')
                self.assertEqual(requests,[])
            finally:CURRENT_RUN.reset(token)
            control=RunControl(self.settings)
            token=CURRENT_RUN.set(control)
            try:
                self.assertEqual(reasoner.triage('运单 W1001 已签收').waybill_id,'W1001')
                self.assertLessEqual(requests[0]['max_tokens'],4096)
                self.assertEqual(control.llm_calls,1)
                self.assertLessEqual(control.tokens,self.settings.token_budget)
            finally:CURRENT_RUN.reset(token)
        finally:reasoner.client.close()

    # 故障返回 503 时只读请求有限重试，写请求从始至终只发一次。
    def test_http_read_retries_but_write_does_not(self):
        from repair_agent.backend_client import RepairBackendClient
        calls=[]
        # 对同一故障平台分别模拟读和写 HTTP 请求。
        def fail(request):
            calls.append(request.method)
            return httpx.Response(503)
        client=RepairBackendClient('http://test',transport=httpx.MockTransport(fail))
        client.guard=DependencyGuard(replace(self.settings,retry_base_seconds=.001),Metrics())
        try:
            self.assertEqual(client.read('GET_WAYBILL','W3001')['status'],'UNAVAILABLE')
            result=client.execute(case_id='CASE-RETRY',waybill_id='W3001',
                playbook_id='REBUILD_DISPATCH_PROJECTION_V1',expected_version=9,idempotency_key='CASE-RETRY:repair')
            self.assertTrue(result['outcome_unknown'])
            self.assertEqual(calls.count('GET'),3)
            self.assertEqual(calls.count('POST'),1)
        finally:client.close()

    # 超大网络响应在达到上限后停止读取并关闭流，不把整个结果加载入内存。
    def test_http_response_limit_closes_stream(self):
        from repair_agent.backend_client import RepairBackendClient
        streams=[]
        class LargeStream(httpx.SyncByteStream):
            # 记录接收字节与关闭状态，模拟持续发送的大响应。
            def __init__(self):self.received=0;self.closed=False
            # 持续发送固定大小块，理论总量远高于接收预算。
            def __iter__(self):
                for _ in range(100):
                    self.received+=16384
                    yield b'x'*16384
            # 请求退出时必须释放响应流。
            def close(self):self.closed=True
        # 每次请求生成独立流，分别验证读证据和写结果的接收上限。
        def respond(request):
            stream=LargeStream();streams.append(stream)
            return httpx.Response(200,stream=stream)
        client=RepairBackendClient('http://test',transport=httpx.MockTransport(respond))
        client.guard=self.runtime.guard
        try:
            self.assertEqual(client.read('GET_WAYBILL','W3001')['status'],'UNAVAILABLE')
            self.assertTrue(client.execute(case_id='CASE-LARGE',waybill_id='W3001',
                playbook_id='REBUILD_DISPATCH_PROJECTION_V1',expected_version=9,idempotency_key='CASE-LARGE:repair')['outcome_unknown'])
            self.assertEqual(len(streams),2)
            self.assertTrue(all(s.closed and s.received<=self.settings.output_bytes*8+16384 for s in streams))
        finally:client.close()

    # 同步演示审批遵守任务时限，慢复核结束后不允许继续写操作。
    def test_synchronous_approval_enforces_budget(self):
        case=self.runtime.create_case('运单 W1001 已签收但仍在派送中')
        original=self.runtime.tools.get_waybill
        # 阻塞复核至超过预算，随后沿原路径查询当前运单。
        def slow(waybill_id):time.sleep(.03);return original(waybill_id)
        self.runtime.settings=replace(self.settings,task_seconds=.01)
        with patch.object(self.runtime.tools,'get_waybill',slow):
            with self.assertRaises(RunStopped):
                self.runtime.decide_approval(case['case_id'],approved=True,actor_id='tester')
        self.assertEqual(self.runtime.get_case(case['case_id'])['status'],'TIMED_OUT')
        self.assertEqual(self.runtime.tools.get_waybill('W1001')['version'],7)
