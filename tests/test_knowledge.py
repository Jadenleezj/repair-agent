import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from repair_agent.knowledge.store import KnowledgeStore
from repair_agent.application.runtime import RepairRuntime
from repair_agent.agent.reasoning import HttpJsonReasoner
from repair_agent.agent.models import DiagnosisResult

class KnowledgeTests(unittest.TestCase):
    # 为当前测试创建独立临时数据库及运行环境，避免测试间数据相互污染。
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = KnowledgeStore(self.path/'knowledge.db')
        self.sample = json.loads((Path(__file__).parents[1]/'examples/historical_cases.json').read_text())
    # 释放测试资源并删除临时目录。
    def tearDown(self): self.temp.cleanup()
    # 将测试案例写入临时 JSON，再调用正式导入流程。
    def ingest(self, entries):
        path = self.path/'cases.json'
        path.write_text(json.dumps(entries,ensure_ascii=False))
        return self.store.import_file(path)
    # 验证中文检索、异常类型过滤、引用哈希及无匹配结果。
    def test_retrieval_filter_and_no_match(self):
        self.ingest(self.sample)
        hits=self.store.search('签收成功 订单派送中','DELIVERY_TASK_ORDER_MISMATCH')
        self.assertEqual(hits[0]['case']['case_id'],'DEMO-HISTORY-001')
        self.assertEqual(len(hits[0]['content_hash']),64)
        self.assertEqual(self.store.search('签收','PICKUP_SCAN_NOT_APPLIED'),[])
        self.assertEqual(self.store.search('xyz987'),[])
    # 验证同 ID 更新，以及非法批次不会部分写入知识库。
    def test_upsert_and_atomic_validation(self):
        self.ingest(self.sample)
        edited=[dict(self.sample[0],title='更新后的案例')]
        self.ingest(edited)
        bad=dict(self.sample[0],case_id='BAD',playbook_id='UNREVIEWED_SQL')
        with self.assertRaises(ValueError): self.ingest([self.sample[0],bad])
        hits=self.store.search('签收')
        self.assertEqual(len(hits),1)
        self.assertEqual(hits[0]['case']['title'],'更新后的案例')
    # 验证 JSONL 导入及重建知识库对象后的持久化读取。
    def test_jsonl_and_restart(self):
        p=self.path/'cases.jsonl';p.write_text(json.dumps(self.sample[0],ensure_ascii=False)+'\n')
        self.store.import_file(p)
        self.assertTrue(KnowledgeStore(self.path/'knowledge.db').search('签收'))
    # 验证即便历史案例匹配，实时证据不足时仍转人工。
    def test_history_does_not_replace_missing_live_evidence(self):
        self.ingest(self.sample)
        runtime=RepairRuntime(self.path/'runtime.db',knowledge_path=self.path/'knowledge.db')
        try:
            result=runtime.create_case('订单 W1002 已签收但是还在派送中')
            self.assertTrue(result['state']['knowledge_hits'])
            self.assertEqual(result['status'],'ESCALATED')
            self.assertIsNone(result['state'].get('execution_result'))
        finally: runtime.close()
    # 验证历史引用进入模型载荷，但不混入当前事实证据。
    def test_reference_is_passed_to_llm_but_not_as_evidence(self):
        reasoner=HttpJsonReasoner('http://localhost','test','test')
        state={'issue_type':'DELIVERY_TASK_ORDER_MISMATCH','evidence':[],
               'knowledge_hits':[{'case':self.sample[0]}]}
        captured=[]
        # 测试替身：捕获模型请求载荷并返回固定诊断，不调用外部服务。
        def complete(model,system,payload):
            captured.append(payload)
            return DiagnosisResult(root_cause=None,evidence_ids=[],playbook_id=None,
                                   historical_match=0,evidence_consistency=0)
        try:
            with patch.object(reasoner,'_complete',side_effect=complete): reasoner.diagnose(state)
            self.assertEqual(captured[0]['historical_references'],state['knowledge_hits'])
            self.assertEqual(captured[0]['evidence'],[])
        finally: reasoner.client.close()
    # 验证知识检索故障时仍能使用实时证据完成诊断。
    def test_retrieval_failure_does_not_break_diagnosis(self):
        runtime=RepairRuntime(self.path/'runtime.db')
        try:
            with patch.object(runtime.knowledge,'search',side_effect=RuntimeError('down')):
                result=runtime.create_case('订单 W1001 已签收但还是派送中')
            self.assertEqual(result['status'],'WAITING_APPROVAL')
            self.assertEqual(result['state']['knowledge_hits'],[])
        finally: runtime.close()
