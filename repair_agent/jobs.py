"""SQLite 持久化任务：原子领取、租约隔离、审批入队和过期任务回收。"""
from contextlib import contextmanager
import json
import sqlite3
import time
import uuid

from .repository import utc_now

TERMINAL_JOBS = {'FINISHED','FAILED','CANCELLED','TIMED_OUT','MANUAL_REVIEW'}

class QueueFull(ValueError):
    pass

class JobStore:
    # 业务表由 Repository 创建，任务表沿用同一数据库以保证入队原子性。
    def __init__(self, path, settings):
        self.path = str(path)
        self.settings = settings
        with self.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS repair_jobs (
                case_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, owner TEXT, lease_until REAL, heartbeat REAL,
                attempts INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT 'READ',
                cancel_requested INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT)''')
            conn.execute('CREATE INDEX IF NOT EXISTS jobs_pending ON repair_jobs(status,available_at)')

    # 每个操作独立连接；BEGIN IMMEDIATE 保证多进程领取与审批的原子性。
    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                yield conn
        finally: conn.close()

    # 在同一事务中创建工单和任务，满队列时不会留下孤立工单。
    def submit(self, message, requester_id):
        case_id = 'CASE-'+uuid.uuid4().hex[:10].upper()
        now = time.time()
        state = {'case_id':case_id,'requester_id':requester_id,'raw_request':message,'status':'QUEUED','audit':[]}
        with self.connect() as conn:
            self.check_capacity(conn)
            conn.execute("INSERT INTO repair_cases VALUES (?,?,?,NULL,'QUEUED',?,?,?)",
                         (case_id,requester_id,message,json.dumps(state,ensure_ascii=False),utc_now(),utc_now()))
            conn.execute("INSERT INTO repair_jobs(case_id,kind,payload,status,available_at,created_at,updated_at) VALUES (?,'INVESTIGATE',?,'PENDING',?,?,?)",
                         (case_id,json.dumps(state,ensure_ascii=False),now,now,now))
            self.event(conn,case_id,'TASK_QUEUED',{'kind':'INVESTIGATE'})
        return {'case_id':case_id,'status':'QUEUED','state':state,'pending_approval':None}

    # 队列限制包含待执行和正在执行的任务，不包含暂停等待人工的工单。
    def check_capacity(self, conn):
        count = conn.execute("SELECT count(*) FROM repair_jobs WHERE status IN ('PENDING','RUNNING')").fetchone()[0]
        if count >= self.settings.queue_capacity: raise QueueFull('task queue is full')

    # 事件与任务状态共用事务，防止成功状态已保存而事件丢失。
    def event(self, conn, case_id, name, payload):
        conn.execute('INSERT INTO case_events(case_id,event_type,payload_json,created_at) VALUES (?,?,?,?)',
                     (case_id,name,json.dumps(payload,ensure_ascii=False),utc_now()))

    # 按 FIFO 原子领取一个任务；owner 是每次领取的新令牌，防止旧执行者回写。
    def claim(self):
        now=time.time()
        with self.connect() as conn:
            row=conn.execute("SELECT * FROM repair_jobs WHERE status='PENDING' AND available_at<=? ORDER BY created_at LIMIT 1",(now,)).fetchone()
            if not row: return None
            token=uuid.uuid4().hex
            conn.execute("UPDATE repair_jobs SET status='RUNNING',owner=?,lease_until=?,heartbeat=?,attempts=attempts+1,updated_at=? WHERE case_id=?",
                         (token,now+self.settings.lease_seconds,now,now,row['case_id']))
            result=dict(row);result.update(owner=token,status='RUNNING',attempts=row['attempts']+1)
            result['payload']=json.loads(result['payload'])
            self.event(conn,row['case_id'],'TASK_CLAIMED',{'kind':row['kind'],'attempt':result['attempts']})
            return result

    # 心跳仅延长仍属于当前执行者且未到期的租约，不能复活已过期租约。
    def heartbeat(self, case_id, owner):
        now=time.time()
        with self.connect() as conn:
            cur=conn.execute("UPDATE repair_jobs SET heartbeat=?,lease_until=?,updated_at=? WHERE case_id=? AND owner=? AND status='RUNNING' AND lease_until>?",
                             (now,now+self.settings.lease_seconds,now,case_id,owner,now))
            return cur.rowcount==1

    # 检查所有权和取消标志，用于每个图节点及外部调用边界。
    def flags(self, case_id, owner):
        with self.connect() as conn:
            row=conn.execute('SELECT * FROM repair_jobs WHERE case_id=?',(case_id,)).fetchone()
            if not row: return False,True
            active=row['owner']==owner and row['status']=='RUNNING' and (row['lease_until'] or 0)>time.time()
            return active,bool(row['cancel_requested'])

    # 写接口发出前持久化意图，异常退出后只能对账，不能直接重跑修复。
    def begin_write(self, job, intent):
        with self.connect() as conn:
            cur=conn.execute("UPDATE repair_jobs SET phase='WRITE',payload=?,updated_at=? WHERE case_id=? AND owner=? AND status='RUNNING' AND lease_until>? AND cancel_requested=0",
                             (json.dumps(intent,ensure_ascii=False),time.time(),job['case_id'],job['owner'],time.time()))
            if cur.rowcount!=1:
                from .reliability import RunStopped
                raise RunStopped('LEASE_LOST')
            self.event(conn,job['case_id'],'REPAIR_INTENT_SAVED',intent)

    # 保存图状态与任务结果，只有当前有效 owner 能完成；未知写结果进入对账队列。
    def finish(self, job, state, status='FINISHED', error=None):
        now=time.time()
        with self.connect() as conn:
            row=conn.execute("SELECT * FROM repair_jobs WHERE case_id=? AND owner=? AND status='RUNNING' AND lease_until>?",
                             (job['case_id'],job['owner'],now)).fetchone()
            if not row: return False
            if state.get('status')=='EXECUTION_OUTCOME_UNKNOWN':
                if job['kind']=='RECONCILE' and row['attempts']>=self.settings.max_attempts:
                    status='MANUAL_REVIEW'
                else:
                    status='PENDING'
                    attempts=row['attempts'] if job['kind']=='RECONCILE' else 0
                    conn.execute("UPDATE repair_jobs SET kind='RECONCILE',attempts=?,cancel_requested=0 WHERE case_id=?",(attempts,job['case_id']))
            conn.execute('UPDATE repair_jobs SET status=?,owner=NULL,lease_until=NULL,updated_at=?,available_at=?,error=? WHERE case_id=?',
                         (status,now,now+(1 if status=='PENDING' else 0),error,job['case_id']))
            state_json=json.dumps(state,ensure_ascii=False,default=str)
            conn.execute('UPDATE repair_cases SET state_json=?,status=?,waybill_id=?,updated_at=? WHERE case_id=?',
                         (state_json,state['status'],state.get('waybill_id'),utc_now(),job['case_id']))
            self.event(conn,job['case_id'],'TASK_'+status,{'status':state['status'],'error':error})
        return True

    # 审批和重新入队在一个事务中完成，两个并发审批最多接受一次。
    def approve(self, case_id, approved, actor_id, comment):
        now=time.time()
        with self.connect() as conn:
            row=conn.execute('SELECT * FROM repair_jobs WHERE case_id=?',(case_id,)).fetchone()
            if row is None: raise KeyError(case_id)
            if row['status']!='WAITING_APPROVAL': raise ValueError('case is not waiting for approval')
            self.check_capacity(conn)
            payload={'approved':approved,'actor_id':actor_id}
            conn.execute("UPDATE repair_jobs SET kind='RESUME',payload=?,status='PENDING',attempts=0,phase='READ',cancel_requested=0,available_at=?,updated_at=? WHERE case_id=?",
                         (json.dumps(payload),now,now,case_id))
            conn.execute('INSERT INTO approvals(case_id,actor_id,approved,comment,created_at) VALUES(?,?,?,?,?)',(case_id,actor_id,int(approved),comment,utc_now()))
            state=json.loads(conn.execute('SELECT state_json FROM repair_cases WHERE case_id=?',(case_id,)).fetchone()[0])
            state['status']='APPROVAL_QUEUED'
            conn.execute("UPDATE repair_cases SET status='APPROVAL_QUEUED',state_json=?,updated_at=? WHERE case_id=?",(json.dumps(state,ensure_ascii=False),utc_now(),case_id))
            self.event(conn,case_id,'APPROVAL_QUEUED',payload)
        return {'case_id':case_id,'status':state['status'],'state':state,'pending_approval':None}

    # 待执行或等待审批可立即取消；执行中的任务设置标志，写入期间不能撤销已发出的请求。
    def cancel(self, case_id):
        with self.connect() as conn:
            row=conn.execute('SELECT * FROM repair_jobs WHERE case_id=?',(case_id,)).fetchone()
            if not row: raise KeyError(case_id)
            if row['status'] not in TERMINAL_JOBS:
                if row['kind']=='RECONCILE' or row['phase']=='WRITE':
                    raise ValueError('repair may already be submitted; reconcile its outcome first')
                conn.execute('UPDATE repair_jobs SET cancel_requested=1,updated_at=? WHERE case_id=?',(time.time(),case_id))
                if row['status']!='RUNNING':
                    state=json.loads(conn.execute('SELECT state_json FROM repair_cases WHERE case_id=?',(case_id,)).fetchone()[0]);state['status']='CANCELLED'
                    conn.execute("UPDATE repair_jobs SET status='CANCELLED' WHERE case_id=?",(case_id,))
                    conn.execute("UPDATE repair_cases SET status='CANCELLED',state_json=?,updated_at=? WHERE case_id=?",(json.dumps(state,ensure_ascii=False),utc_now(),case_id))
                self.event(conn,case_id,'CANCEL_REQUESTED',{})
        return self.get(case_id)

    # 租约过期回收：读任务有限重试，可能已写入的任务只进入对账，旧 owner 被清除。
    def recover(self):
        now=time.time();count=0
        with self.connect() as conn:
            rows=conn.execute("SELECT * FROM repair_jobs WHERE status='RUNNING' AND lease_until<=?",(now,)).fetchall()
            for row in rows:
                state=json.loads(conn.execute('SELECT state_json FROM repair_cases WHERE case_id=?',(row['case_id'],)).fetchone()[0])
                if row['phase']=='WRITE':
                    kind='RECONCILE';status='PENDING' if row['attempts']<self.settings.max_attempts or row['kind']!='RECONCILE' else 'MANUAL_REVIEW'
                    state['status']='EXECUTION_OUTCOME_UNKNOWN'
                else:
                    kind=row['kind'];status='PENDING' if row['attempts']<self.settings.max_attempts and not row['cancel_requested'] else 'FAILED'
                    state['status']='RECOVERING' if status=='PENDING' else ('CANCELLED' if row['cancel_requested'] else 'WORKER_LOST')
                    if row['cancel_requested']:status='CANCELLED'
                attempts=0 if kind=='RECONCILE' and row['kind']!='RECONCILE' else row['attempts']
                conn.execute('UPDATE repair_jobs SET kind=?,status=?,owner=NULL,lease_until=NULL,attempts=?,available_at=?,updated_at=? WHERE case_id=?',
                             (kind,status,attempts,now,now,row['case_id']))
                conn.execute('UPDATE repair_cases SET status=?,state_json=?,updated_at=? WHERE case_id=?',(state['status'],json.dumps(state,ensure_ascii=False),utc_now(),row['case_id']))
                self.event(conn,row['case_id'],'LEASE_RECOVERED',{'phase':row['phase'],'next':kind,'status':status});count+=1
        return count

    # 停机超过收尾时间时立即撤销当前执行者租约，由恢复规则决定读重试或写对账。
    def retire(self,job):
        with self.connect() as conn:
            conn.execute("UPDATE repair_jobs SET lease_until=0 WHERE case_id=? AND owner=? AND status='RUNNING'",(job['case_id'],job['owner']))
        self.recover()

    # 人工核实服务恢复后可重新发起只读对账，仍然禁止直接重发修复接口。
    def request_reconcile(self,case_id):
        with self.connect() as conn:
            row=conn.execute('SELECT * FROM repair_jobs WHERE case_id=?',(case_id,)).fetchone()
            if row is None:raise KeyError(case_id)
            if row['status']!='MANUAL_REVIEW' or row['kind']!='RECONCILE':
                raise ValueError('case is not waiting for manual reconciliation')
            self.check_capacity(conn)
            conn.execute("UPDATE repair_jobs SET status='PENDING',attempts=0,available_at=?,updated_at=? WHERE case_id=?",(time.time(),time.time(),case_id))
            self.event(conn,case_id,'RECONCILIATION_REQUESTED',{})
        return self.get(case_id)

    # API 返回任务信息时隐藏内部执行令牌，仅展示进度和租约时间。
    def get(self, case_id):
        with self.connect() as conn:
            row=conn.execute('SELECT * FROM repair_jobs WHERE case_id=?',(case_id,)).fetchone()
        if not row: return None
        value=dict(row);value.pop('owner');value.pop('payload');return value

    # 汇总队列与未决写操作数量，不返回工单级高基数标签。
    def stats(self):
        with self.connect() as conn:
            counts={r[0]:r[1] for r in conn.execute('SELECT status,count(*) FROM repair_jobs GROUP BY status')}
            oldest=conn.execute("SELECT min(created_at) FROM repair_jobs WHERE status='PENDING'").fetchone()[0]
            unknown=conn.execute("SELECT count(*) FROM repair_cases WHERE status='EXECUTION_OUTCOME_UNKNOWN'").fetchone()[0]
        return {'jobs':counts,'oldest_pending_seconds':time.time()-oldest if oldest else 0,'unknown_outcomes':unknown}

    # 查找已终结且过保留期的工单；等待审批和结果未决的数据不清理。
    def expired(self):
        cutoff=time.time()-self.settings.retention_days*86400
        with self.connect() as conn:
            return [r[0] for r in conn.execute("SELECT case_id FROM repair_jobs WHERE status IN ('FINISHED','FAILED','CANCELLED','TIMED_OUT') AND updated_at<? AND case_id NOT IN (SELECT case_id FROM repair_cases WHERE status='EXECUTION_OUTCOME_UNKNOWN')",(cutoff,))]

    # 调用者先删除对应检查点，再清理工单审计；保留后端执行幂等记录用于防止重复修复。
    def delete_expired(self, case_id):
        with self.connect() as conn:
            for table in ['case_events','approvals','repair_cases','repair_jobs']:
                conn.execute(f'DELETE FROM {table} WHERE case_id=?',(case_id,))
