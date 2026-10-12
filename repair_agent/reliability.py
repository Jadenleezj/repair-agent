"""运行预算、平台隔离、失败退避和进程内指标；所有等待都响应取消。"""
from __future__ import annotations

import contextvars
import json
import math
import os
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class StabilitySettings:
    workers: int = 2
    queue_capacity: int = 100
    task_seconds: float = 120
    lease_seconds: float = 30
    heartbeat_seconds: float = 5
    poll_seconds: float = .2
    shutdown_seconds: float = 5
    max_attempts: int = 3
    model_calls: int = 30
    token_budget: int = 30000
    repeat_limit: int = 2
    read_retries: int = 2
    retry_base_seconds: float = .2
    tool_timeout_seconds: float = 10
    model_timeout_seconds: float = 20
    tool_concurrency: int = 2
    breaker_failures: int = 3
    breaker_seconds: float = 30
    output_bytes: int = 32000
    retention_days: int = 30
    maintenance_seconds: float = 60

    # 读取统一前缀环境配置，并在启动时拒绝无效预算或心跳配置。
    @classmethod
    def from_env(cls):
        values = {k: type(v)(os.environ['REPAIR_'+k.upper()])
                  for k, v in cls().__dict__.items() if 'REPAIR_'+k.upper() in os.environ}
        settings = cls(**values)
        settings.validate()
        return settings

    # 控制配置必须有限且为正数；允许将只读重试设置为零。
    def validate(self):
        for key, value in self.__dict__.items():
            if not math.isfinite(value) or (value < 0 if key == 'read_retries' else value <= 0):
                raise ValueError(f'{key} has an invalid value')
        if self.lease_seconds < self.heartbeat_seconds * 3:
            raise ValueError('lease_seconds must be >= 3 * heartbeat_seconds')


class RunStopped(RuntimeError):
    # 保留可序列化终止原因，供工单状态和审计使用。
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


class RunControl:
    # 租约检查由调用方注入，超时使用 monotonic 避免系统时钟跳变。
    def __init__(self, settings: StabilitySettings, active: Callable[[], bool] = lambda: True,
                 cancelled: Callable[[], bool] = lambda: False):
        self.settings = settings
        self.deadline = time.monotonic() + settings.task_seconds
        self.active = active
        self.cancelled = cancelled
        self.llm_calls = 0
        self.calls = 0
        self.tokens = 0
        self.repeats = defaultdict(int)
        self.write_started = False
        self.write_completed = False
        self.before_write: Callable[[], None] = lambda: None

    # 节点和底层请求共同检查预算；已失去执行权的 Worker 不能继续写操作。
    def check(self):
        if not self.active(): raise RunStopped('LEASE_LOST')
        if self.cancelled(): raise RunStopped('CANCELLED')
        if time.monotonic() >= self.deadline: raise RunStopped('TIMED_OUT')

    # 记录推理入口次数，并限制反复调用；token 为实际用量或明确标注的估算。
    def reasoning(self):
        self.check()
        if self.calls >= self.settings.model_calls: raise RunStopped('MODEL_BUDGET_EXCEEDED')
        self.calls += 1

    # 真实 HTTP 模型请求（包括重试）单独计数，不让底层重试绕开调用预算。
    def model_request(self):
        self.check()
        if self.llm_calls >= self.settings.model_calls:raise RunStopped('MODEL_BUDGET_EXCEEDED')
        self.llm_calls += 1

    # 在发送请求前预留输入预算，返回后追加输出用量，预算用完终止后续执行。
    def consume(self, count: int):
        if self.tokens + max(0, count) > self.settings.token_budget:
            raise RunStopped('TOKEN_BUDGET_EXCEEDED')
        self.tokens += max(0, count)

    # 重复限制按工具与参数组合计算，只有调查调用使用该计数。
    def tool(self, name: str, parameters: str):
        self.check()
        key = (name, parameters)
        self.repeats[key] += 1
        if self.repeats[key] > self.settings.repeat_limit: raise RunStopped('REPEAT_LIMIT_EXCEEDED')

    # 小步等待，不让重试退避和资源排队忽略取消或任务截止时间。
    def wait(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(.05, max(0, end-time.monotonic())))


CURRENT_RUN = contextvars.ContextVar('repair_current_run', default=None)


class Metrics:
    # 指标标签仅使用固定工具名和阶段名，避免高基数工单标签无限增长。
    def __init__(self):
        self.lock = threading.Lock()
        self.counters = defaultdict(int)
        self.durations = defaultdict(lambda: {'count': 0, 'sum_seconds': 0., 'max_seconds': 0.})

    # 计数更新在多 Worker 下保持一致。
    def increment(self, name: str, amount: int = 1):
        with self.lock: self.counters[name] += amount

    # 累积耗时统计，不保留无限历史样本。
    def observe(self, name: str, seconds: float):
        with self.lock:
            item = self.durations[name]
            item['count'] += 1
            item['sum_seconds'] += seconds
            item['max_seconds'] = max(item['max_seconds'], seconds)

    # 返回独立快照，避免 API 序列化时与指标更新冲突。
    def snapshot(self):
        with self.lock:
            return {'counters': dict(self.counters), 'durations': json.loads(json.dumps(self.durations))}


class DependencyUnavailable(RuntimeError):
    pass


class DependencyGuard:
    # 每个平台拥有独立信号量和熔断状态，不共享一个无界线程池。
    def __init__(self, settings: StabilitySettings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.lock = threading.Lock()
        self.slots = {}
        self.breakers = {}

    # 等待平台容量并执行单次或有限重试；写接口必须传 retries=0。
    def call(self, dependency: str, operation: Callable, retries: int = 0):
        control = CURRENT_RUN.get()
        with self.lock:
            slot = self.slots.setdefault(dependency, threading.BoundedSemaphore(self.settings.tool_concurrency))
        wait_until = time.monotonic() + self.settings.tool_timeout_seconds
        while not slot.acquire(timeout=.05):
            if control: control.check()
            if time.monotonic() >= wait_until:
                self.metrics.increment(dependency+'.capacity_timeout')
                raise DependencyUnavailable('DEPENDENCY_CAPACITY_TIMEOUT')
        try:
            with self.lock:
                failures, opened = self.breakers.get(dependency, (0, 0))
                if opened and time.monotonic()-opened < self.settings.breaker_seconds:
                    self.metrics.increment(dependency+'.circuit_open')
                    raise DependencyUnavailable('CIRCUIT_OPEN')
                # 只允许一个半开探测；其余请求保持熔断。
                if opened: self.breakers[dependency] = (failures, time.monotonic())
            for attempt in range(retries+1):
                started = time.monotonic()
                try:
                    if control: control.check()
                    value = operation()
                except RunStopped:
                    raise
                except Exception as exc:
                    import httpx
                    retryable = isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) or (
                        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {429,500,502,503,504})
                    self.metrics.increment(dependency+'.errors')
                    if retryable:
                        with self.lock:
                            count, _ = self.breakers.get(dependency, (0,0))
                            count += 1
                            self.breakers[dependency] = (count, time.monotonic() if count >= self.settings.breaker_failures else 0)
                    if not retryable or attempt >= retries: raise
                    self.metrics.increment(dependency+'.retries')
                    # 自定义等待用于同时检查任务截止时间与租约，不进行无界重试。
                    delay = self.settings.retry_base_seconds*(2**attempt)*random.uniform(.8,1.2)
                    if control: control.wait(delay)
                    else: time.sleep(delay)
                else:
                    if isinstance(value,dict) and value.get('status')=='UNAVAILABLE':
                        self.metrics.increment(dependency+'.unavailable')
                        with self.lock:
                            count,_=self.breakers.get(dependency,(0,0))
                            count+=1
                            self.breakers[dependency]=(count,time.monotonic() if count>=self.settings.breaker_failures else 0)
                    else:
                        with self.lock:self.breakers[dependency]=(0,0)
                        self.metrics.increment(dependency+'.success')
                    if control: control.check()
                    return value
                finally:
                    self.metrics.observe(dependency, time.monotonic()-started)
        finally:
            slot.release()

    # 提供各平台熔断状态，供运维查看故障隔离是否生效。
    def snapshot(self):
        with self.lock:
            return {k: {'failures':v[0], 'open':bool(v[1] and time.monotonic()-v[1] < self.settings.breaker_seconds)}
                    for k,v in self.breakers.items()}
