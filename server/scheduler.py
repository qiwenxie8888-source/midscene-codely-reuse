"""多设备调度器：决定「哪个任务在哪台设备上跑」。

与 runner.py 的区别：
  - runner.py 是单执行槽（一次一个任务），这里按设备并行
  - 这里只管调度，不关心任务怎么执行（由 Executor 负责）

三个关键约束：
  1. 同一游戏全局串行——不是按设备锁，而是跨设备的游戏锁，
     否则两台设备跑同一个游戏会互相覆盖存档
  2. 先看游戏锁，再看设备：跑不了的任务不占设备
  3. 取消/掉线都必须释放设备，否则槽位永久泄漏

这是独立模块，暂时不接入 api.py / runner.py，先用测试验证逻辑。
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .devices import Device
from .executor import Executor

# 终态：这些状态的任务不再占用设备
TERMINAL_STATUSES = {"passed", "failed", "cancelled", "interrupted"}

# 跨游戏任务的锁键：独占所有设备，避免和单游戏任务交错
ALL_GAMES = "__all__"


class BusyError(RuntimeError):
    """队列已满。"""


@dataclass
class RunJob:
    run_id: str
    project: str
    files: list[str] = field(default_factory=list)
    game_key: str | None = None
    clean: bool = True
    status: str = "queued"  # queued | assigned | running | passed | failed | cancelled | interrupted
    device: str | None = None
    wait_reason: str | None = None  # no_device | game_locked | device_offline
    blocking_game: str | None = None
    blocking_device: str | None = None
    queue_position: int | None = None
    created_at: float = field(default_factory=time.time)
    assigned_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    fake_duration: float | None = None  # 测试用：假执行器跑多久
    process: Any = None

    def lock_key(self) -> str:
        return self.game_key or ALL_GAMES

    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return round(end - self.started_at, 1)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "files": self.files,
            "game_key": self.game_key,
            "status": self.status,
            "device": self.device,
            "wait_reason": self.wait_reason,
            "blocking_game": self.blocking_game,
            "blocking_device": self.blocking_device,
            "queue_position": self.queue_position,
            "exit_code": self.exit_code,
            "error": self.error,
            "elapsed": self.elapsed(),
            "created_at": self.created_at,
            "assigned_at": self.assigned_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class Scheduler:
    """调度线程 + 执行线程池。

    调度线程只做配对（job ↔ device），执行在线程池里进行，
    两者通过 Condition 通信：入队、释放、设备变化都会唤醒调度线程。
    """

    def __init__(
        self,
        devices: list[Device],
        executor: Executor,
        max_parallel: int = 2,
        max_queue: int = 3,
    ) -> None:
        self._devices: dict[str, Device] = {device.serial: device for device in devices}
        self._jobs: dict[str, RunJob] = {}
        self._queue: list[RunJob] = []
        self._busy_games: set[str] = set()
        self._lock = threading.RLock()
        self._change = threading.Condition(self._lock)
        self._executor = executor
        self._max_parallel = max(1, min(max_parallel, len(self._devices) or 1))
        self._max_queue = max_queue
        self._pool = ThreadPoolExecutor(
            max_workers=self._max_parallel, thread_name_prefix="run-slot"
        )
        self._dispatch = threading.Thread(
            target=self._dispatch_loop, name="run-dispatch", daemon=True
        )
        self._dispatch.start()

    # ------------------------------------------------------------------ 对外

    def start(
        self,
        project: str,
        files: list[str] | None = None,
        game_key: str | None = None,
        clean: bool = True,
        fake_duration: float | None = None,
    ) -> RunJob:
        with self._change:
            if len(self._queue) >= self._max_queue:
                raise BusyError(f"队列已满（最多排队 {self._max_queue} 个）")
            job = RunJob(
                run_id=time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4],
                project=project,
                files=list(files or []),
                game_key=game_key,
                clean=clean,
                fake_duration=fake_duration,
            )
            self._jobs[job.run_id] = job
            self._queue.append(job)
            self._refresh_queue_state()
            self._change.notify_all()
            return job

    def cancel(self, run_id: str) -> RunJob:
        with self._change:
            job = self._jobs.get(run_id)
            if job is None:
                raise KeyError(run_id)
            if job.status == "queued":
                # 还在排队：直接从队列摘掉，不占设备
                self._queue.remove(job)
                job.status = "cancelled"
                job.finished_at = time.time()
                self._refresh_queue_state()
            elif job.status in ("assigned", "running"):
                # 已分配：交给执行器中断，_run_job 的 finally 负责释放
                job.cancel_event.set()
            self._change.notify_all()
            return job

    def set_device_status(self, serial: str, status: str) -> None:
        """设备上线/离线。离线时把在跑的任务标记为中断并释放槽位。"""
        with self._change:
            device = self._devices.get(serial)
            if device is None:
                return
            device.status = status
            if status in ("offline", "unauthorized") and device.current_run:
                job = self._jobs.get(device.current_run)
                if job is not None and job.status not in TERMINAL_STATUSES:
                    job.cancel_event.set()
                    job.status = "interrupted"
                    job.error = f"设备 {device.label()} 掉线"
            self._change.notify_all()

    def get(self, run_id: str) -> RunJob | None:
        with self._lock:
            return self._jobs.get(run_id)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "devices": [device.to_dict() for device in self._devices.values()],
                "queue_depth": len(self._queue),
                "max_queue": self._max_queue,
                "max_parallel": self._max_parallel,
                "jobs": {run_id: job.to_dict() for run_id, job in self._jobs.items()},
            }

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """等所有任务落终态（测试用）。超时返回 False。"""
        deadline = time.time() + timeout
        with self._change:
            while True:
                pending = [
                    job
                    for job in self._jobs.values()
                    if job.status not in TERMINAL_STATUSES
                ]
                if not pending:
                    return True
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._change.wait(timeout=min(remaining, 0.1))

    # ------------------------------------------------------------------ 内部

    def _dispatch_loop(self) -> None:
        while True:
            with self._change:
                while True:
                    pair = self._pick()
                    if pair is not None:
                        break
                    # 虚假唤醒是真实存在的，必须用循环而不是 if
                    self._change.wait()

                job, device = pair
                self._assign(job, device)

            self._pool.submit(self._run_job, job, device)

    def _pick(self) -> tuple[RunJob, Device] | None:
        """按 FIFO 找第一个"跑得了"的任务。

        跑不了（游戏被占）就跳过，绝不丢弃——它只是暂时不能跑。
        """
        for job in list(self._queue):
            if job.cancel_event.is_set():
                continue
            lock_key = job.lock_key()
            if lock_key != ALL_GAMES and lock_key in self._busy_games:
                continue
            if ALL_GAMES in self._busy_games:
                continue
            device = self._idle_device()
            if device is not None:
                return job, device
        return None

    def _idle_device(self) -> Device | None:
        return next(
            (device for device in self._devices.values() if device.status == "idle"),
            None,
        )

    def _assign(self, job: RunJob, device: Device) -> None:
        job.status = "assigned"
        job.device = device.serial
        job.assigned_at = time.time()
        job.wait_reason = None
        job.blocking_game = None
        job.blocking_device = None
        device.status = "busy"
        device.current_run = job.run_id
        device.game_key = job.game_key
        self._busy_games.add(job.lock_key())
        self._queue.remove(job)
        self._refresh_queue_state()

    def _run_job(self, job: RunJob, device: Device) -> None:
        try:
            # assigned 期间被取消：不启动执行器
            if job.cancel_event.is_set():
                job.status = "cancelled"
                return
            job.status = "running"
            job.started_at = time.time()
            code = self._executor.run(job, device)
            job.exit_code = code
            # 掉线优先于取消：设备断开时 cancel_event 也被置位，
            # 但语义上应该记成 interrupted 而不是用户取消
            if job.status == "interrupted":
                pass
            elif job.cancel_event.is_set():
                job.status = "cancelled"
            elif code == 0:
                job.status = "passed"
            else:
                job.status = "failed"
        except Exception as error:  # noqa: BLE001 - 任何执行异常都要落到任务上
            job.status = "failed"
            job.error = str(error)
        finally:
            job.finished_at = time.time()
            with self._change:
                self._release(device, job)
                self._change.notify_all()

    def _release(self, device: Device, job: RunJob) -> None:
        device.current_run = None
        device.game_key = None
        if device.status != "offline":
            device.status = "idle"
        self._busy_games.discard(job.lock_key())
        self._refresh_queue_state()

    def _refresh_queue_state(self) -> None:
        """重算排队任务的位置和等待原因，供前端展示。

        两种等待要分开：
          game_locked —— 有设备空闲，但该游戏正在别的设备上跑
          no_device   —— 没有空闲设备（不管在跑什么）
        """
        has_idle = any(device.status == "idle" for device in self._devices.values())
        busy_by_game = {
            device.game_key: device.serial
            for device in self._devices.values()
            if device.status == "busy" and device.game_key
        }
        for index, job in enumerate(self._queue, start=1):
            job.queue_position = index
            lock_key = job.lock_key()
            if lock_key != ALL_GAMES and lock_key in self._busy_games:
                job.wait_reason = "game_locked"
                job.blocking_game = job.game_key
                job.blocking_device = busy_by_game.get(job.game_key or "")
            elif ALL_GAMES in self._busy_games or not has_idle:
                job.wait_reason = "no_device"
                job.blocking_game = None
                job.blocking_device = None
            else:
                # 有设备空闲、游戏也没被占，理论上会被立刻派出去；
                # 这里只是极短窗口内的兜底
                job.wait_reason = "no_device"
