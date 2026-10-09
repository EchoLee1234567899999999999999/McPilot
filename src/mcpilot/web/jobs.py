"""后台任务与进度事件（Phase 4 新增）。

设计目标
--------
一次真实推荐需要 ~10 秒并会真实调用多次 MCP。为了让**查询状态页**如实反映
处理进度，推荐在一个**后台线程**中执行，进度事件通过线程安全的任务对象发布：

- :class:`Job` —— 单个推荐任务：保存已发生的事件、最终结果或错误。
- :class:`JobManager` —— 线程安全的任务注册表，支持多订阅者。

关键约束
--------
1. 事件必须来自**真实处理动作**（真实 MCP 调用 / 真实候选循环），不允许伪造。
2. 订阅者（SSE 连接）先**回放**已发生事件，再**实时**接收后续事件——回放与订阅
   在锁内原子完成，因此既不丢事件也不重复。
3. 每个任务的订阅队列有上限，慢消费者不会无限占用内存。
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


# 单个订阅者的队列上限：足够容纳全部进度事件；超出即视为异常慢消费者。
_QUEUE_MAX = 512


@dataclass
class Job:
    """单个推荐任务的状态容器（线程安全）。"""

    job_id: str
    status: str = "pending"  # pending | running | done | error
    events: list[dict[str, Any]] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    error_kind: str = ""  # config / network / timeout / param / server / internal
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _subscribers: list[queue.Queue] = field(default_factory=list, repr=False)

    # -- 发布 --------------------------------------------------------------
    def publish(self, event: dict[str, Any]) -> None:
        """发布一条进度事件（来自真实处理动作）。"""
        with self._lock:
            self.events.append(event)
            for q in list(self._subscribers):
                try:
                    q.put_nowait(dict(event))
                except queue.Full:  # pragma: no cover - 极端慢消费者
                    pass

    def finish(self, result: dict[str, Any]) -> None:
        """标记成功完成，并广播终止事件。"""
        with self._lock:
            self.status = "done"
            self.result = result
            self.finished_at = time.time()
            term = {"type": "result", "status": "done"}
            for q in list(self._subscribers):
                try:
                    q.put_nowait(term)
                except queue.Full:  # pragma: no cover
                    pass

    def fail(self, message: str, kind: str = "internal") -> None:
        """标记失败，并广播终止事件（含可读错误信息，绝不含凭据）。"""
        with self._lock:
            self.status = "error"
            self.error = message
            self.error_kind = kind
            self.finished_at = time.time()
            term = {"type": "error", "status": "error", "message": message, "kind": kind}
            for q in list(self._subscribers):
                try:
                    q.put_nowait(term)
                except queue.Full:  # pragma: no cover
                    pass

    # -- 订阅（SSE 用）-----------------------------------------------------
    def subscribe(self) -> Iterator[dict[str, Any]]:
        """返回该任务的事件迭代器：先回放历史，再实时接收，直到任务终止。

        回放与注册订阅在**同一把锁**内完成，保证不丢/不重。
        """
        q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            replay = list(self.events)
            already_done = self.status
            self._subscribers.append(q)
        try:
            for ev in replay:
                yield ev
            # 回放期间任务若已结束，补发终止事件后返回
            if already_done == "done":
                yield {"type": "result", "status": "done"}
                return
            if already_done == "error":
                yield {
                    "type": "error",
                    "status": "error",
                    "message": self.error,
                    "kind": self.error_kind,
                }
                return
            while True:
                try:
                    ev = q.get(timeout=15.0)
                except queue.Empty:
                    # 心跳：用于保活 SSE 连接（前端忽略），不代表任何业务进度
                    yield {"type": "heartbeat"}
                    continue
                yield ev
                if ev.get("type") in ("result", "error"):
                    return
        finally:
            with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    # -- 快照（轮询用）-----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "events": list(self.events),
                "result": self.result,
                "error": self.error,
                "error_kind": self.error_kind,
            }


class JobManager:
    """线程安全的任务注册表。

    ``max_jobs`` 限制保留的任务数；超出时按创建时间清理最旧且**已结束**的任务，
    避免长时间运行后内存无界增长。
    """

    def __init__(self, *, max_jobs: int = 64) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max_jobs = max(4, int(max_jobs))

    def create(self) -> Job:
        job = Job(job_id=uuid.uuid4().hex[:12])
        with self._lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._evict_locked()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def _evict_locked(self) -> None:
        while len(self._order) > self._max_jobs:
            for i, jid in enumerate(self._order):
                job = self._jobs.get(jid)
                if job is not None and job.status in ("done", "error"):
                    self._order.pop(i)
                    self._jobs.pop(jid, None)
                    break
            else:
                return  # 全部在运行中 → 暂不清理


__all__ = ["Job", "JobManager"]
