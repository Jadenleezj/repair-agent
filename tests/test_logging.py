"""验证日期切换、关联信息和凭据脱敏。"""
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
from repair_agent.application.logging_setup import DailyJsonHandler, LOG_CONTEXT, traced


class LoggingTests(unittest.TestCase):
    def test_daily_files_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'REPAIR_LLM_API_KEY':'test-secret-key'}):
            handler=DailyJsonHandler(Path(directory))
            error_handler=DailyJsonHandler(Path(directory), True)
            token=LOG_CONTEXT.set({'case_id':'CASE-TEST'})
            try:
                for day in (12,13):
                    record=logging.LogRecord('test',logging.ERROR,'',0,'api_key=test-secret-key Bearer hidden',(),None)
                    record.created=datetime(2026,10,day,23,59,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
                    handler.handle(record)
                    error_handler.handle(record)
                for day in (12,13):
                    folder=Path(directory)/f'2026-10-{day}'
                    item=json.loads((folder/'agent.log').read_text())
                    self.assertEqual(item['case_id'],'CASE-TEST')
                    self.assertNotIn('test-secret-key',item['message'])
                    self.assertNotIn('hidden',item['message'])
                    self.assertTrue((folder/'error.log').exists())
            finally:LOG_CONTEXT.reset(token)

    def test_trace_context_restored_on_failure(self):
        @traced
        def fail(case_id):
            self.assertEqual(LOG_CONTEXT.get()['case_id'],case_id)
            raise ValueError('controlled failure')
        with self.assertLogs(__name__,level='INFO'):
            with self.assertRaises(ValueError):fail('CASE-X')
        self.assertEqual(LOG_CONTEXT.get(),{})
