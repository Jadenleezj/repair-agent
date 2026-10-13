"""本地按日 JSON 日志：关联工单，保留异常堆栈，并屏蔽配置中的密钥。"""
from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_CONTEXT = contextvars.ContextVar('repair_log_context', default={})
_SETUP_LOCK = threading.RLock()


def redact(text: str) -> str:
    """过滤环境变量中的凭据，以及常见 Authorization 与密钥字段。"""
    for key, value in os.environ.items():
        if value and len(value) >= 4 and any(word in key.upper() for word in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')):
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[^\s"\']+', r'\1[REDACTED]', text)
    return re.sub(r'(?i)((?:api[_-]?key|password|secret|access_token)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', text)


class DailyJsonHandler(logging.Handler):
    """按上海日期写入 logs/YYYY-MM-DD，跨午夜自动切换文件。"""

    def __init__(self, directory: Path, errors_only: bool = False):
        super().__init__(logging.ERROR if errors_only else logging.NOTSET)
        self.directory = directory
        self.filename = 'error.log' if errors_only else 'agent.log'

    def emit(self, record):
        """只记录受控元数据，不记录请求体、证据内容及模型提示词。"""
        try:
            now = datetime.fromtimestamp(record.created, ZoneInfo('Asia/Shanghai'))
            fields = {**LOG_CONTEXT.get(), **getattr(record, 'event_fields', {})}
            item = {'time': now.isoformat(timespec='milliseconds'), 'level': record.levelname,
                    'logger': record.name, 'thread': record.threadName, **fields,
                    'message': record.getMessage()}
            if record.exc_info:
                item['exception'] = logging.Formatter().formatException(record.exc_info)
            # 先脱敏字段再序列化，避免正则吞掉 JSON 的引号或括号。
            item = {key: redact(value) if isinstance(value, str) else value for key, value in item.items()}
            line = json.dumps(item, ensure_ascii=False, default=str)
            folder = self.directory / now.strftime('%Y-%m-%d')
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            # 每条日志独立关闭文件，轮转无需依赖后台线程，停机也不会遗留缓冲。
            with (folder / self.filename).open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except Exception:
            self.handleError(record)


def configure_logging() -> Path:
    """重复初始化不叠加 Handler；目录和级别可通过本地 .env 配置。"""
    directory = Path(os.getenv('REPAIR_LOG_DIR', 'logs')).expanduser().absolute()
    level = getattr(logging, os.getenv('REPAIR_LOG_LEVEL', 'INFO').upper(), logging.INFO)
    with _SETUP_LOCK:
        root = logging.getLogger()
        existing = [h for h in root.handlers if isinstance(h, DailyJsonHandler)]
        if not existing or any(h.directory != directory for h in existing):
            for handler in existing:
                root.removeHandler(handler)
                handler.close()
            root.addHandler(DailyJsonHandler(directory))
            root.addHandler(DailyJsonHandler(directory, True))
        root.setLevel(level)
        logging.getLogger('httpx').setLevel(logging.WARNING)
    return directory


def traced(fn):
    """记录函数耗时及有限结果摘要，并在调用结束后恢复上下文。"""
    signature = inspect.signature(fn)
    logger = logging.getLogger(fn.__module__)

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        fields = dict(LOG_CONTEXT.get())
        for key in ('case_id', 'order_id', 'tool', 'playbook_id', 'approved'):
            if key in bound:
                fields[key] = bound[key]
        for key in ('job', 'state'):
            value = bound.get(key)
            if isinstance(value, dict):
                fields.update({k: value[k] for k in ('case_id', 'order_id') if value.get(k)})
        token = LOG_CONTEXT.set(fields)
        started = time.monotonic()
        logger.info('%s started', fn.__qualname__)
        try:
            result = fn(*args, **kwargs)
            summary = {k: result[k] for k in ('case_id', 'status', 'success', 'decision', 'confidence') if isinstance(result, dict) and k in result}
            if isinstance(result, list):
                summary['result_count'] = len(result)
            log = logger.warning if summary.get('success') is False or summary.get('status') == 'UNAVAILABLE' else logger.info
            log('%s completed duration_ms=%.1f', fn.__qualname__, (time.monotonic()-started)*1000,
                        extra={'event_fields': summary})
            return result
        except BaseException as exc:
            if type(exc).__name__ in ('GraphInterrupt', 'GraphBubbleUp'):
                logger.info('%s paused for approval', fn.__qualname__)
            elif isinstance(exc, Exception):
                logger.exception('%s failed duration_ms=%.1f', fn.__qualname__, (time.monotonic()-started)*1000)
            raise
        finally:
            LOG_CONTEXT.reset(token)
    return wrapped
