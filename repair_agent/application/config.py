"""本地配置加载；应用启动时读取 .env，不覆盖已有进程环境变量。"""
from __future__ import annotations

import os
from pathlib import Path
import threading

from dotenv import load_dotenv

_CONFIG_LOCK = threading.RLock()


# 开发安装固定读取项目根目录；普通安装包从启动目录读取本地配置。
def default_env_file() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    if (project_root / 'pyproject.toml').is_file():
        return project_root / '.env'
    return Path.cwd() / '.env'


# 显式配置文件缺失时报错，默认 .env 缺失允许使用原有规则与默认配置。
def load_local_config(path: str | Path | None = None) -> Path | None:
    specified = path if path is not None else os.getenv('REPAIR_ENV_FILE')
    env_file = Path(specified).expanduser() if specified else default_env_file()
    if not env_file.is_file():
        if specified:
            raise FileNotFoundError(f'Configuration file not found: {env_file}')
        return None
    with _CONFIG_LOCK:
        # 不记录文件内容或密钥；每次启动读取，修改已有配置后重启进程生效。
        load_dotenv(dotenv_path=env_file, override=False, encoding='utf-8')
    return env_file
