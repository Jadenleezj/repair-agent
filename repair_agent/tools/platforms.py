"""五种只读平台的本地模拟适配器，不连接真实生产系统。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from repair_agent.domain.models import Evidence
from repair_agent.persistence.repository import Repository, utc_now

PlatformToolName = Literal[
    'QUERY_LOG_PLATFORM', 'QUERY_ONLINE_DATA', 'QUERY_CONFIG_CENTER',
    'QUERY_SCHEDULER', 'QUERY_HIVE_TABLE',
]
PLATFORM_TOOLS = {
    'QUERY_LOG_PLATFORM': ('日志平台', 'PLATFORM_LOGS'),
    'QUERY_ONLINE_DATA': ('线上数据', 'ONLINE_DATA_SNAPSHOT'),
    'QUERY_CONFIG_CENTER': ('配置中心', 'CONFIG_SNAPSHOT'),
    'QUERY_SCHEDULER': ('定时任务平台', 'SCHEDULER_RUNS'),
    'QUERY_HIVE_TABLE': ('Hive 表', 'HIVE_SNAPSHOT'),
}

class MockPlatformTools:
    # 加载独立的平台快照；线上数据直接读取演示业务库，保证修复后查询可见。
    def __init__(self, repository: Repository, fixture_path: str | Path | None = None):
        self.repo = repository
        path = Path(fixture_path) if fixture_path else Path(__file__).parent / 'fixtures' / 'platforms.json'
        self.fixtures = json.loads(path.read_text(encoding='utf-8'))

    # 展示只读工具名称、用途和统一查询参数，供 API 和模型规划使用。
    def catalog(self) -> list[dict]:
        return [{'name': name, 'description': label, 'read_only': True,
                 'parameters': {'order_id': '订单号，例如 W1001'},
                 'fact_type': fact} for name, (label, fact) in PLATFORM_TOOLS.items()]

    # 按订单查询平台数据；区分无数据和平台不可用，Hive 快照不冒充实时数据。
    def query(self, tool: str, order_id: str) -> Evidence:
        if tool not in PLATFORM_TOOLS:
            raise ValueError(f'Unknown platform tool: {tool}')
        status = 'FOUND'
        if tool == 'QUERY_ONLINE_DATA':
            with self.repo.lock:
                order = self.repo.conn.execute('SELECT * FROM demo_orders WHERE order_id=?', (order_id,)).fetchone()
                task = self.repo.conn.execute('SELECT * FROM demo_delivery_tasks WHERE order_id=?', (order_id,)).fetchone()
            status = 'FOUND' if order else 'NOT_FOUND'
            value = {'environment': 'mock-online', 'order': dict(order) if order else None,
                     'delivery_task': dict(task) if task else None, 'queried_at': utc_now()}
        else:
            item = self.fixtures.get(tool, {}).get(order_id)
            status = item['status'] if item else 'NOT_FOUND'
            value = item.get('value') if item else None
        return {'evidence_id': f'EV-{tool}', 'source': f'mock:{tool.lower()}',
                'fact_type': PLATFORM_TOOLS[tool][1], 'value': value,
                'status': status, 'authoritative': tool == 'QUERY_ONLINE_DATA' and status == 'FOUND'}
