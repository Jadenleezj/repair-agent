"""有界后台执行：每个 Worker 使用独立运行时，心跳线程维护所有租约。"""
from repair_agent.application.logging_setup import traced
import logging
import threading
import time

from repair_agent.execution.reliability import CURRENT_RUN, RunControl, RunStopped

logger=logging.getLogger(__name__)

class WorkerPool:
    # 工厂为每个工作线程创建独立数据库连接、检查点连接和 HTTP 客户端。
    def __init__(self, runtime):
        self.runtime=runtime;self.settings=runtime.settings;self.jobs=runtime.jobs
        self.stop_event=threading.Event();self.cancel_event=threading.Event()
        self.lock=threading.Lock();self.active={};self.threads=[];self.monitor=None

    # 首次启动先回收过期任务，再启动有限数量的消费者和心跳线程。
    def start(self):
        if self.threads: return
        self.jobs.recover()
        self.stop_event.clear();self.cancel_event.clear()
        for i in range(self.settings.workers):
            thread=threading.Thread(target=self.loop,name=f'repair-worker-{i}',daemon=True)
            self.threads.append(thread);thread.start()
        self.monitor=threading.Thread(target=self.watch,name='repair-heartbeat',daemon=True);self.monitor.start()

    # 工作线程只领取自己的任务；失败记录状态，不终止整个消费循环。
    def loop(self):
        from repair_agent.application.runtime import RepairRuntime
        runtime=None
        try:
            runtime=RepairRuntime(self.runtime.db_path,settings=self.settings,
                knowledge_path=self.runtime.knowledge.path,metrics=self.runtime.metrics,guard=self.runtime.guard,
                worker_reasoner_factory=self.runtime.worker_reasoner_factory)
            if self.runtime.worker_reasoner_factory:
                if hasattr(runtime.reasoner,'client'):runtime.reasoner.client.close()
                runtime.reasoner=self.runtime.worker_reasoner_factory()
                if hasattr(runtime.reasoner,'client'):
                    runtime.reasoner.guard=self.runtime.guard
                    runtime.reasoner.client.timeout=self.settings.model_timeout_seconds
                runtime.rebuild_graph()
            while not self.stop_event.is_set():
                try:
                    job=self.jobs.claim()
                    if not job:
                        self.stop_event.wait(self.settings.poll_seconds);continue
                    with self.lock: self.active[job['case_id']]=job
                    try: self.run_job(runtime,job)
                    finally:
                        with self.lock: self.active.pop(job['case_id'],None)
                except Exception:
                    logger.exception('worker iteration failed')
                    self.runtime.metrics.increment('worker.errors')
                    self.stop_event.wait(self.settings.poll_seconds)
        except Exception:
            logger.exception('worker startup failed');self.runtime.metrics.increment('worker.startup_errors')
        finally:
            if runtime: runtime.close()

    # 为每次领取建立预算和租约检查；写前意图在调用后端前持久化。
    @traced
    def run_job(self,runtime,job):
        # 节点执行权以数据库中的 owner 与有效租约为准。
        def active():
            return self.jobs.flags(job['case_id'],job['owner'])[0]
        # 用户取消和超出停机宽限期统一作用于当前任务。
        def cancelled():
            return self.cancel_event.is_set() or self.jobs.flags(job['case_id'],job['owner'])[1]
        control=RunControl(self.settings,active,cancelled)
        # 先提交写入意图，再允许底层客户端发送请求。
        def before_write():
            state=runtime.current_state(job['case_id'])
            intent={k:state.get(k) for k in ['case_id','order_id','playbook_id','diagnosed_version']}
            self.jobs.begin_write(job,intent)
        control.before_write=before_write
        token=CURRENT_RUN.set(control)
        started=time.monotonic()
        try:
            if job['kind']=='RECONCILE':
                state=runtime.reconcile(job['case_id'],job['payload'])
                status='FINISHED'
            else:
                result=runtime.invoke_job(job)
                state=runtime.current_state(job['case_id'])
                pending=runtime.pending_approval(result)
                state["pending_approval"]=pending
                status='WAITING_APPROVAL' if pending else 'FINISHED'
            control.check()
            self.jobs.finish(job,state,status)
        except RunStopped as exc:
            state=runtime.current_state(job['case_id']) or job['payload']
            state=dict(state,case_id=job['case_id'])
            state['status']='EXECUTION_OUTCOME_UNKNOWN' if control.write_started or job['kind']=='RECONCILE' else exc.status
            state['audit']=[*state.get('audit',[]),f'run_stopped: {exc.status}']
            self.jobs.finish(job,state,exc.status if exc.status in {'CANCELLED','TIMED_OUT'} else 'FAILED',exc.status)
            self.runtime.metrics.increment('tasks.'+exc.status.lower())
        except Exception as exc:
            logger.exception('job failed case=%s',job['case_id'])
            state=runtime.current_state(job['case_id']) or {'case_id':job['case_id']}
            state=dict(state,status='EXECUTION_OUTCOME_UNKNOWN' if control.write_started or job['kind']=='RECONCILE' else 'FAILED')
            self.jobs.finish(job,state,'FAILED',type(exc).__name__)
        finally:
            CURRENT_RUN.reset(token)
            self.runtime.metrics.observe('tasks',time.monotonic()-started)
            self.runtime.metrics.increment('tasks.reasoning_entries',control.calls)
            self.runtime.metrics.increment('tasks.model_requests',control.llm_calls)
            self.runtime.metrics.increment('tasks.token_budget_usage',control.tokens)

    # 定期续租与过期回收；维护清理在单独运行时中执行，避免共用检查点连接。
    def watch(self):
        next_maintenance=time.monotonic()+self.settings.maintenance_seconds
        while not self.cancel_event.is_set():
            with self.lock: jobs=list(self.active.values())
            if self.stop_event.is_set() and not jobs: return
            for job in jobs:
                try: self.jobs.heartbeat(job['case_id'],job['owner'])
                except Exception: logger.exception('heartbeat failed')
            try:
                recovered=self.jobs.recover()
                if recovered:self.runtime.metrics.increment('tasks.recovered',recovered)
                if time.monotonic()>=next_maintenance and not self.stop_event.is_set():
                    self.runtime.maintenance()
                    next_maintenance=time.monotonic()+self.settings.maintenance_seconds
            except Exception: logger.exception('maintenance failed')
            self.cancel_event.wait(self.settings.heartbeat_seconds)

    # 先停止领取，有限等待，再请求取消；正在执行的网络调用依靠底层超时退出。
    def stop(self):
        self.stop_event.set()
        deadline=time.monotonic()+self.settings.shutdown_seconds
        for thread in self.threads: thread.join(max(0,deadline-time.monotonic()))
        self.cancel_event.set()
        with self.lock:remaining=list(self.active.values())
        for job in remaining:
            try:self.jobs.retire(job)
            except Exception:logger.exception('failed to retire task during shutdown')
        # 不关闭工作线程仍在使用的连接，连接由各线程 finally 释放。
        if self.monitor:self.monitor.join(timeout=min(1,self.settings.heartbeat_seconds))

    # 就绪检查要求所有工作线程及心跳线程存活，且服务未进入停机状态。
    def ready(self):
        return bool(self.threads and not self.stop_event.is_set() and self.monitor and self.monitor.is_alive()
                    and all(t.is_alive() for t in self.threads))
