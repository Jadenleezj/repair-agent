"""验证本地配置读取、覆盖优先级及各启动入口的配置生效时机。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repair_agent.application.config import default_env_file, load_local_config
from repair_agent.agent.reasoning import HttpJsonReasoner, reasoner_from_env
from repair_agent.execution.reliability import StabilitySettings
from repair_agent.api.app import create_app
from repair_agent.cli import main


class ConfigTests(unittest.TestCase):
    # 独立配置文件和环境快照，测试绝不读取开发者真实密钥。
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'test.env'
        self.path.write_text('# isolated test configuration\n')
        clean = {key:value for key,value in os.environ.items()
                 if not key.startswith('REPAIR_') and key != 'PYTHON_DOTENV_DISABLED'}
        clean['REPAIR_ENV_FILE'] = str(self.path)
        self.env_patch = patch.dict(os.environ,clean,clear=True)
        self.env_patch.start()

    # 恢复原进程环境并删除测试文件。
    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    # dotenv 支持注释和带特殊字符的引号值，且不覆盖已有环境变量。
    def test_read_quotes_and_preserve_environment(self):
        self.path.write_text('REPAIR_LLM_API_KEY="test-key#with-space value"\nREPAIR_LLM_MODEL=file-model\n',encoding='utf-8')
        os.environ['REPAIR_LLM_MODEL'] = 'environment-model'
        self.assertEqual(load_local_config(),self.path)
        self.assertEqual(os.environ['REPAIR_LLM_API_KEY'],'test-key#with-space value')
        self.assertEqual(os.environ['REPAIR_LLM_MODEL'],'environment-model')

    # 工厂直接读取文件启用模型和运行预算，创建客户端但不发送请求。
    def test_model_factory_and_settings_read_file(self):
        self.path.write_text('REPAIR_LLM_BASE_URL=http://test/v1\nREPAIR_LLM_API_KEY=test-key\nREPAIR_LLM_MODEL=test-model\nREPAIR_WORKERS=3\n')
        reasoner = reasoner_from_env()
        try:
            self.assertIsInstance(reasoner,HttpJsonReasoner)
            self.assertEqual(reasoner.model,'test-model')
            self.assertEqual(StabilitySettings.from_env().workers,3)
        finally: reasoner.client.close()

    # API 默认数据库必须在读取本地配置后确定，不能在模块导入时固化。
    def test_api_database_uses_file_configuration(self):
        db = Path(self.tmp.name)/'api-config.db'
        self.path.write_text(f'REPAIR_AGENT_DB={db}\n')
        app = create_app()
        try:
            self.assertEqual(app.state.runtime.db_path,str(db))
            self.assertTrue(db.exists())
        finally: app.state.runtime.close()

    # CLI 默认数据库读文件配置，显式 --db 参数优先。
    def test_cli_database_and_argument_priority(self):
        db = Path(self.tmp.name)/'cli-config.db'
        self.path.write_text(f'REPAIR_AGENT_DB={db}\n')
        result={'case_id':'CASE-CONFIG','state':{'status':'TEST'},'pending_approval':None}
        with patch('repair_agent.cli.RepairRuntime') as runtime, patch('builtins.print'):
            runtime.return_value.create_case.return_value = result
            with patch('sys.argv',['repair-agent','test']): main()
            self.assertEqual(runtime.call_args.args[0],db)
            override = Path(self.tmp.name)/'explicit.db'
            with patch('sys.argv',['repair-agent','test','--db',str(override)]): main()
            self.assertEqual(runtime.call_args.args[0],override)

    # 显式配置文件路径拼错时报告缺失，而不是悄悄切换运行模式。
    def test_missing_explicit_configuration_fails(self):
        with self.assertRaises(FileNotFoundError):
            load_local_config(Path(self.tmp.name)/'missing.env')

    # 开发模式固定项目根目录，普通 wheel 安装使用启动目录。
    def test_source_and_installed_default_location(self):
        root = Path(self.tmp.name)/'source'
        module = root/'repair_agent/application/config.py'
        module.parent.mkdir(parents=True)
        (root/'pyproject.toml').write_text('')
        with patch('repair_agent.application.config.__file__',str(module)):
            self.assertEqual(default_env_file(),(root/'.env').resolve())
        (root/'pyproject.toml').unlink()
        with patch('repair_agent.application.config.__file__',str(module)), patch('pathlib.Path.cwd',return_value=Path(self.tmp.name)):
            self.assertEqual(default_env_file(),Path(self.tmp.name)/'.env')

    # 默认 .env 不存在时仍可使用既有环境变量和默认配置。
    def test_missing_default_configuration_is_optional(self):
        os.environ.pop('REPAIR_ENV_FILE')
        with patch('repair_agent.application.config.default_env_file',return_value=Path(self.tmp.name)/'.env'):
            self.assertIsNone(load_local_config())
