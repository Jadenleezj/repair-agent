"""将旧运单领域持久化数据迁移为订单领域，保留任务与检查点 ID。"""
import json
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from repair_agent.domain import statuses


def rename_domain(value):
    if isinstance(value, str):
        return value.replace('WAYBILL', 'ORDER').replace('Waybill', 'Order').replace('waybill', 'order').replace('运单', '订单')
    if isinstance(value, dict):
        return {rename_domain(k): rename_domain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [rename_domain(v) for v in value]
    if isinstance(value, tuple):
        return tuple(rename_domain(v) for v in value)
    return value


def migrate_order_domain(conn):
    """在建表之前执行；一次事务迁移结构、JSON 和序列化检查点。"""
    with conn:
        conn.execute('CREATE TABLE IF NOT EXISTS domain_migrations (name TEXT PRIMARY KEY)')
        if conn.execute("SELECT 1 FROM domain_migrations WHERE name='order_domain_v1'").fetchone():
            return
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'demo_waybills' in tables:
            conn.execute('ALTER TABLE demo_waybills RENAME TO demo_orders')
            tables.remove('demo_waybills')
            tables.add('demo_orders')
        for table in ('repair_cases', 'demo_orders', 'demo_delivery_tasks', 'demo_operation_events'):
            if table not in tables:
                continue
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            if 'waybill_id' in columns and 'order_id' not in columns:
                conn.execute(f'ALTER TABLE {table} RENAME COLUMN waybill_id TO order_id')
        for table, column in (('repair_cases', 'state_json'), ('repair_cases', 'raw_request'),
                              ('case_events', 'payload_json'), ('repair_jobs', 'payload'),
                              ('repair_executions', 'result_json'), ('experiences', 'payload')):
            if table in tables:
                for rowid, value in conn.execute(f'SELECT rowid,{column} FROM {table}').fetchall():
                    updated = rename_domain(value)
                    if updated != value:
                        conn.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (updated, rowid))
        serde = JsonPlusSerializer()
        # 老检查点可能包含原枚举类名，仅在反序列化迁移期间提供该名称。
        setattr(statuses, 'WaybillStatus', statuses.OrderStatus)
        try:
            for table, column in (('checkpoints', 'checkpoint'), ('writes', 'value')):
                if table not in tables:
                    continue
                for rowid, kind, blob in conn.execute(f'SELECT rowid,type,{column} FROM {table}').fetchall():
                    decoded = serde.loads_typed((kind, blob))
                    updated = rename_domain(decoded)
                    new_kind, new_blob = serde.dumps_typed(updated)
                    conn.execute(f'UPDATE {table} SET type=?,{column}=? WHERE rowid=?', (new_kind, new_blob, rowid))
            if 'writes' in tables:
                for rowid, channel in conn.execute('SELECT rowid,channel FROM writes').fetchall():
                    conn.execute('UPDATE writes SET channel=? WHERE rowid=?', (rename_domain(channel), rowid))
            if 'checkpoints' in tables:
                for rowid, metadata in conn.execute('SELECT rowid,metadata FROM checkpoints').fetchall():
                    if metadata is not None:
                        conn.execute('UPDATE checkpoints SET metadata=? WHERE rowid=?',
                                     (json.dumps(rename_domain(json.loads(metadata)), ensure_ascii=False), rowid))
        finally:
            delattr(statuses, 'WaybillStatus')
        conn.execute("INSERT INTO domain_migrations VALUES ('order_domain_v1')")
