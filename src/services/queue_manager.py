"""
核心职责：生成任务排队与并发调度管理器（GenerationQueueManager）。
主要功能：
1. 根据当前打码方式与浏览器槽位容量计算动态并发上限，为超并发请求提供 FIFO 排队机制；
2. 解决多请求同时发起时在底层打码槽位竞争导致 30 秒排队超时的根源问题；
3. 提供实时队列任务状态快照（排队中/执行中/已完成/已取消），并支持后台手动取消尚未开始执行的排队任务。
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from ..core.config import config


@dataclass
class QueueTaskItem:
    """队列任务项"""

    task_id: str
    model: str
    generation_type: str  # "image" | "video"
    prompt: str
    stream: bool
    status: str = "queued"  # "queued" | "running" | "completed" | "failed" | "cancelled"
    status_text: str = "queued"
    progress: int = 0
    token_id: Optional[int] = None
    token_email: Optional[str] = None
    log_id: Optional[int] = None
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def to_dict(self, queue_position: Optional[int] = None) -> Dict[str, Any]:
        now = time.time()
        if self.status == "queued":
            wait_seconds = max(0.0, now - self.created_at)
            run_seconds = 0.0
        elif self.status == "running":
            wait_seconds = max(0.0, (self.started_at or now) - self.created_at)
            run_seconds = max(0.0, now - (self.started_at or now))
        else:
            end_ts = self.finished_at or now
            if self.started_at:
                wait_seconds = max(0.0, self.started_at - self.created_at)
                run_seconds = max(0.0, end_ts - self.started_at)
            else:
                wait_seconds = max(0.0, end_ts - self.created_at)
                run_seconds = 0.0

        prompt_preview = (self.prompt or "").strip()
        if len(prompt_preview) > 120:
            prompt_preview = f"{prompt_preview[:120]}..."

        return {
            "task_id": self.task_id,
            "model": self.model,
            "generation_type": self.generation_type,
            "prompt": prompt_preview,
            "full_prompt": (self.prompt or "")[:1000],
            "stream": self.stream,
            "status": self.status,
            "status_text": self.status_text,
            "progress": self.progress,
            "queue_position": queue_position,
            "can_cancel": self.status == "queued",
            "token_id": self.token_id,
            "token_email": self.token_email,
            "log_id": self.log_id,
            "error_message": self.error_message,
            "wait_seconds": round(wait_seconds, 1),
            "run_seconds": round(run_seconds, 1),
            "total_seconds": round(wait_seconds + run_seconds, 1),
            "created_at": datetime.fromtimestamp(self.created_at).isoformat(),
            "started_at": (
                datetime.fromtimestamp(self.started_at).isoformat()
                if self.started_at
                else None
            ),
            "finished_at": (
                datetime.fromtimestamp(self.finished_at).isoformat()
                if self.finished_at
                else None
            ),
        }


class GenerationQueueManager:
    """生成任务 FIFO 队列与并发控制器"""

    MAX_HISTORY_ITEMS = 50
    DEFAULT_QUEUE_TIMEOUT_SECONDS = 1800.0

    def __init__(self):
        self._lock = asyncio.Lock()
        self._tasks: Dict[str, QueueTaskItem] = {}
        self._waiting_ids: Deque[str] = deque()
        self._running_ids: set[str] = set()
        self._history_ids: Deque[str] = deque()
        self._completed_total: int = 0
        self._failed_total: int = 0
        self._cancelled_total: int = 0

    def get_effective_concurrency(self) -> int:
        """根据当前打码配置动态计算并发执行槽位上限"""
        method = str(getattr(config, "captcha_method", "yescaptcha") or "yescaptcha").strip().lower()
        browser_count = max(1, int(getattr(config, "browser_count", 1) or 1))
        if method in {"browser", "cloakbrowser"}:
            return browser_count
        if method == "personal":
            max_tabs = max(1, int(getattr(config, "personal_max_resident_tabs", 5) or 5))
            return min(50, browser_count * max_tabs)
        return max(4, browser_count * 4)

    def get_queue_wait_timeout(self, generation_type: str = "image") -> float:
        """读取系统配置中的队列排队最大等待时间（秒）"""
        _ = generation_type
        try:
            configured = float(getattr(config, "queue_timeout", 1800) or 1800)
            return max(30.0, min(86400.0, configured))
        except Exception:
            return self.DEFAULT_QUEUE_TIMEOUT_SECONDS

    def _schedule_locked(self) -> None:
        """在锁内按 FIFO 唤醒等待队列中的任务，直到达到并发上限"""
        limit = self.get_effective_concurrency()
        while len(self._running_ids) < limit and self._waiting_ids:
            next_task_id = self._waiting_ids.popleft()
            item = self._tasks.get(next_task_id)
            if not item or item.status != "queued":
                continue
            item.status = "running"
            item.status_text = "started"
            item.started_at = time.time()
            self._running_ids.add(next_task_id)
            item.event.set()

    def _archive_task_locked(self, task_id: str) -> None:
        """将已结束的任务移入近期历史队列并控制容量"""
        if task_id not in self._history_ids:
            self._history_ids.appendleft(task_id)
        while len(self._history_ids) > self.MAX_HISTORY_ITEMS:
            expired_id = self._history_ids.pop()
            if (
                expired_id not in self._running_ids
                and expired_id not in self._waiting_ids
            ):
                self._tasks.pop(expired_id, None)

    async def enqueue(
        self,
        *,
        task_id: str,
        model: str,
        generation_type: str,
        prompt: str,
        stream: bool = False,
        log_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """将生成请求加入队列；若有空闲槽位且无前序等待任务则立即进入 running"""
        async with self._lock:
            item = QueueTaskItem(
                task_id=task_id,
                model=model,
                generation_type=generation_type,
                prompt=prompt or "",
                stream=bool(stream),
                log_id=log_id,
            )
            self._tasks[task_id] = item
            limit = self.get_effective_concurrency()
            if len(self._running_ids) < limit and not self._waiting_ids:
                item.status = "running"
                item.status_text = "started"
                item.started_at = time.time()
                self._running_ids.add(task_id)
                item.event.set()
                position = 0
            else:
                item.status = "queued"
                item.status_text = "queued"
                self._waiting_ids.append(task_id)
                position = len(self._waiting_ids)

            return {
                "task_id": task_id,
                "status": item.status,
                "queue_position": position,
                "max_concurrency": limit,
                "running_count": len(self._running_ids),
                "queued_count": len(self._waiting_ids),
            }

    async def wait_for_turn(
        self,
        task_id: str,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """等待轮到该任务执行。

        Returns:
            dict: {"status": "running" | "cancelled" | "timeout", "wait_ms": int, "reason": Optional[str]}
        """
        async with self._lock:
            item = self._tasks.get(task_id)
            if not item:
                return {"status": "cancelled", "wait_ms": 0, "reason": "任务不存在"}
            if item.status == "running":
                return {"status": "running", "wait_ms": 0, "reason": None}
            if item.status == "cancelled":
                return {
                    "status": "cancelled",
                    "wait_ms": int((time.time() - item.created_at) * 1000),
                    "reason": item.error_message or "队列任务已被手动取消",
                }
            wait_event = item.event
            gen_type = item.generation_type

        effective_timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None and timeout_seconds > 0
            else self.get_queue_wait_timeout(gen_type)
        )

        try:
            await asyncio.wait_for(wait_event.wait(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                item = self._tasks.get(task_id)
                if item and item.status == "queued":
                    try:
                        self._waiting_ids.remove(task_id)
                    except ValueError:
                        pass
                    item.status = "failed"
                    item.status_text = "queue_timeout"
                    item.finished_at = time.time()
                    item.error_message = f"队列等待超时 ({int(effective_timeout)}秒)"
                    self._failed_total += 1
                    self._archive_task_locked(task_id)
                    self._schedule_locked()
                wait_ms = (
                    int(((item.finished_at or time.time()) - item.created_at) * 1000)
                    if item
                    else int(effective_timeout * 1000)
                )
            return {
                "status": "timeout",
                "wait_ms": wait_ms,
                "reason": f"队列等待超时（已等待 {int(effective_timeout)} 秒）",
            }

        async with self._lock:
            item = self._tasks.get(task_id)
            if not item:
                return {"status": "cancelled", "wait_ms": 0, "reason": "任务已被移除"}
            wait_end = item.started_at or item.finished_at or time.time()
            wait_ms = max(0, int((wait_end - item.created_at) * 1000))
            if item.status == "cancelled":
                return {
                    "status": "cancelled",
                    "wait_ms": wait_ms,
                    "reason": item.error_message or "队列任务已被手动取消",
                }
            return {"status": "running", "wait_ms": wait_ms, "reason": None}

    async def update_task(
        self,
        task_id: str,
        *,
        status_text: Optional[str] = None,
        progress: Optional[int] = None,
        token_id: Optional[int] = None,
        token_email: Optional[str] = None,
        log_id: Optional[int] = None,
    ) -> None:
        """更新任务的执行进度与绑定的账号/日志信息"""
        async with self._lock:
            item = self._tasks.get(task_id)
            if not item:
                return
            if status_text is not None:
                item.status_text = status_text
            if progress is not None:
                item.progress = max(0, min(100, int(progress)))
            if token_id is not None:
                item.token_id = token_id
            if token_email is not None:
                item.token_email = token_email
            if log_id is not None:
                item.log_id = log_id

    async def finish_task(
        self,
        task_id: str,
        *,
        status: str = "completed",
        status_text: Optional[str] = None,
        progress: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """结束任务并释放并发槽位，自动唤醒下一个排队任务"""
        async with self._lock:
            item = self._tasks.get(task_id)
            was_waiting = False
            if task_id in self._waiting_ids:
                try:
                    self._waiting_ids.remove(task_id)
                    was_waiting = True
                except ValueError:
                    pass
            self._running_ids.discard(task_id)

            if item:
                # 如果此前已经被标记为 cancelled，不覆盖为其它状态
                if item.status != "cancelled":
                    item.status = status
                    if status_text:
                        item.status_text = status_text
                    elif status == "completed":
                        item.status_text = "completed"
                    elif status == "cancelled":
                        item.status_text = "cancelled"
                    else:
                        item.status_text = "failed"

                    if progress is not None:
                        item.progress = max(0, min(100, int(progress)))
                    elif status == "completed":
                        item.progress = 100

                    if error_message:
                        item.error_message = error_message

                    item.finished_at = time.time()
                    if status == "completed":
                        self._completed_total += 1
                    elif status == "cancelled":
                        self._cancelled_total += 1
                    else:
                        self._failed_total += 1

                item.event.set()
                self._archive_task_locked(task_id)

            if not was_waiting:
                self._schedule_locked()
            else:
                self._schedule_locked()

    async def cancel_task(self, task_id: str, reason: str = "手动取消排队任务") -> Dict[str, Any]:
        """手动取消尚未开始执行的排队任务（status == 'queued'）"""
        normalized_id = str(task_id or "").strip()
        if not normalized_id:
            return {"success": False, "message": "任务 ID 不能为空"}

        async with self._lock:
            item = self._tasks.get(normalized_id)
            if not item:
                return {"success": False, "message": f"未找到任务: {normalized_id}"}
            if item.status == "running":
                return {
                    "success": False,
                    "message": "该任务已开始执行，仅支持取消排队中（尚未执行）的任务",
                }
            if item.status != "queued":
                return {
                    "success": False,
                    "message": f"任务当前状态为 {item.status}，无需取消",
                }

            try:
                self._waiting_ids.remove(normalized_id)
            except ValueError:
                pass

            item.status = "cancelled"
            item.status_text = "cancelled"
            item.error_message = reason
            item.finished_at = time.time()
            self._cancelled_total += 1
            item.event.set()
            self._archive_task_locked(normalized_id)
            self._schedule_locked()

            return {
                "success": True,
                "message": "排队任务已取消",
                "task": item.to_dict(queue_position=None),
            }

    async def cancel_all_queued(self, reason: str = "批量手动取消排队任务") -> Dict[str, Any]:
        """一键取消全部处于排队等待中（尚未执行）的任务"""
        async with self._lock:
            cancelled_ids: List[str] = []
            while self._waiting_ids:
                tid = self._waiting_ids.popleft()
                item = self._tasks.get(tid)
                if not item or item.status != "queued":
                    continue
                item.status = "cancelled"
                item.status_text = "cancelled"
                item.error_message = reason
                item.finished_at = time.time()
                self._cancelled_total += 1
                item.event.set()
                self._archive_task_locked(tid)
                cancelled_ids.append(tid)

            return {
                "success": True,
                "cancelled_count": len(cancelled_ids),
                "cancelled_task_ids": cancelled_ids,
                "message": (
                    f"已取消 {len(cancelled_ids)} 个排队中的任务"
                    if cancelled_ids
                    else "当前没有排队中的待执行任务"
                ),
            }

    async def clear_history(self) -> Dict[str, Any]:
        """清理已完成/已取消/已失败的历史队列记录（不影响排队中与执行中任务）"""
        async with self._lock:
            removed = 0
            while self._history_ids:
                tid = self._history_ids.pop()
                if tid not in self._running_ids and tid not in self._waiting_ids:
                    if self._tasks.pop(tid, None) is not None:
                        removed += 1
            return {
                "success": True,
                "removed_count": removed,
                "message": f"已清理 {removed} 条历史队列记录",
            }

    async def get_snapshot(self, include_history: bool = True) -> Dict[str, Any]:
        """获取当前队列状态与任务列表快照"""
        async with self._lock:
            self._schedule_locked()
            max_concurrency = self.get_effective_concurrency()
            queued_list: List[Dict[str, Any]] = []
            running_list: List[Dict[str, Any]] = []
            history_list: List[Dict[str, Any]] = []

            for idx, tid in enumerate(self._waiting_ids, start=1):
                item = self._tasks.get(tid)
                if item and item.status == "queued":
                    queued_list.append(item.to_dict(queue_position=idx))

            running_items = [
                self._tasks[tid]
                for tid in self._running_ids
                if tid in self._tasks and self._tasks[tid].status == "running"
            ]
            running_items.sort(key=lambda x: x.started_at or x.created_at)
            for item in running_items:
                running_list.append(item.to_dict(queue_position=0))

            if include_history:
                for tid in list(self._history_ids)[:25]:
                    if tid in self._running_ids or tid in self._waiting_ids:
                        continue
                    item = self._tasks.get(tid)
                    if item:
                        history_list.append(item.to_dict(queue_position=None))

            return {
                "success": True,
                "max_concurrency": max_concurrency,
                "captcha_method": str(getattr(config, "captcha_method", "yescaptcha") or "yescaptcha"),
                "queued_count": len(queued_list),
                "running_count": len(running_list),
                "completed_total": self._completed_total,
                "failed_total": self._failed_total,
                "cancelled_total": self._cancelled_total,
                "queued_tasks": queued_list,
                "running_tasks": running_list,
                "history_tasks": history_list,
                "tasks": [*queued_list, *running_list, *history_list],
            }


queue_manager = GenerationQueueManager()

