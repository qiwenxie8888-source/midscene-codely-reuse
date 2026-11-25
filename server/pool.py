"""设备池：把任务分配到不同设备上并行执行。

设计取舍：不重写执行细节，而是「每台设备一个 Runner 实例」。
Runner 已经具备日志泵、SSE 事件流、进度解析、取消和硬超时，
池只负责决定任务落在哪台设备、以及什么时候释放。

三条规则：
  1. 同一游戏全局串行——游戏锁是池级别的，不是设备级别
  2. 跑不了的任务留在等待队列，不占设备、不报错
  3. 设备离线时不再派新任务，正在跑的任务由 Runner 自己失败
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .devices import Device
from .runner import BusyError, Runner

# 任务落终态后不再占用游戏锁
TERMINAL_STATUSES = {"passed", "failed", "cancelled", "interrupted"}

# 轮询间隔：Runner 没有完成回调，用短轮询回收游戏锁
RELEASE_INTERVAL_S = 0.4


@dataclass
class PendingJob:
    """还没有分配到设备的任务。"""

    run_id: str
    project: str
    files: list[str]
    clean: bool
    game_key: str | None
    status: str = "queued"
    device: str | None = None
    wait_reason: str | None = None
    queue_position: int | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "files": self.files,
            "clean": self.clean,
            "game_key": self.game_key,
            "status": self.status,
            "device": self.device,
            "wait_reason": self.wait_reason,
            "queue_position": self.queue_position,
            "created_at": self.created_at,
            "mode": "project" if not self.files else "files",
        }


class DevicePool:
    def __init__(self, devices: list[Device] | None = None, max_queue: int = 3) -> None:
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._runners: dict[str, Runner] = {}
        self._waiting: list[PendingJob] = []
        self._run_owner: dict[str, str] = {}  # run_id -> device serial
        self._busy_games: dict[str, str] = {}  # game_key -> run_id
        self._game_by_run: dict[str, str] = {}  # run_id -> game_key
        self._max_queue = max_queue
        self._thread = threading.Thread(
            target=self._loop, name="device-pool", daemon=True
        )
        self._thread.start()
        self.refresh(devices or [])

    # ------------------------------------------------------------------ 设备

    def refresh(self, devices: list[Device]) -> None:
        """刷新设备列表。已有 Runner 保留（可能正在跑任务），只更新状态。"""
        with self._lock:
            online = {device.serial for device in devices}
            for device in devices:
                self._devices[device.serial] = device
                if device.serial not in self._runners:
                    self._runners[device.serial] = Runner(device_serial=device.serial)
            # 不再出现且没在跑任务的设备，标记离线
            for serial, device in self._devices.items():
                if serial not in online:
                    device.status = "offline"

    def cancel(self, run_id: str) -> str:
        with self._lock:
            for item in self._waiting:
                if item.run_id == run_id:
                    self._waiting.remove(item)
                    item.status = "cancelled"
                    self._reindex_queue()
                    return "cancelled"
            runner = self._runner_for(run_id)
            if runner is None:
                raise KeyError(run_id)
            return runner.cancel(run_id)

    # ------------------------------------------------------------------ 任务

    def start(
        self,
        project: str,
        files: list[str],
        clean: bool = True,
        game_key: str | None = None,
    ) -> PendingJob:
        with self._lock:
            if not self._runners:
                raise BusyError("没有可用设备，请先连接手机并开启 USB 调试")
            if len(self._waiting) >= self._max_queue:
                raise BusyError(f"队列已满（最多排队 {self._max_queue} 个）")

            item = PendingJob(
                # 排队期间就要有稳定 id，前端靠它轮询状态
                run_id=time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4],
                project=project,
                files=list(files or []),
                clean=clean,
                game_key=game_key,
            )
            self._waiting.append(item)
            self._dispatch()
            return item

    def get(self, run_id: str) -> Any | None:
        with self._lock:
            for item in self._waiting:
                if item.run_id == run_id:
                    return item
        runner = self._runner_for(run_id)
        if runner is None:
            return None
        return runner.get(run_id)

    def runner_for(self, run_id: str) -> Runner | None:
        return self._runner_for(run_id)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            devices = []
            for serial, device in self._devices.items():
                runner = self._runners.get(serial)
                snap = runner.snapshot() if runner else None
                running = (snap or {}).get("running")
                devices.append(
                    {
                        **device.to_dict(),
                        "status": "busy" if running else device.status,
                        "current_run": (running or {}).get("run_id"),
                        "game_key": (running or {}).get("game_key"),
                    }
                )
            return {
                "devices": devices,
                "waiting": [item.to_dict() for item in self._waiting],
                "queue_depth": len(self._waiting),
                "max_queue": self._max_queue,
                "busy_games": dict(self._busy_games),
            }

    # ------------------------------------------------------------------ 内部

    def _runner_for(self, run_id: str) -> Runner | None:
        serial = self._run_owner.get(run_id)
        if serial:
            return self._runners.get(serial)
        # 兜底：还没登记 owner 时遍历（极短窗口）
        for runner in self._runners.values():
            if runner.get(run_id) is not None:
                return runner
        return None

    def _loop(self) -> None:
        while True:
            time.sleep(RELEASE_INTERVAL_S)
            with self._lock:
                self._release_finished()
                self._dispatch()

    def _release_finished(self) -> None:
        """回收已结束任务的游戏锁。设备本身由各自的 Runner 管理。"""
        for run_id, serial in list(self._run_owner.items()):
            runner = self._runners.get(serial)
            job = runner.get(run_id) if runner else None
            if job is None or job.status in TERMINAL_STATUSES:
                game = self._game_by_run.pop(run_id, None)
                if game and self._busy_games.get(game) == run_id:
                    self._busy_games.pop(game, None)

    def _dispatch(self) -> None:
        """给等待中的任务找设备。跑不了的保留，不丢。"""
        for item in list(self._waiting):
            if item.game_key and item.game_key in self._busy_games:
                item.wait_reason = "game_locked"
                continue
            serial = self._idle_serial()
            if serial is None:
                item.wait_reason = "no_device"
                continue
            runner = self._runners[serial]
            try:
                runner.start(
                    item.project,
                    item.files,
                    item.clean,
                    item.game_key,
                    run_id=item.run_id,
                )
            except BusyError:
                item.wait_reason = "no_device"
                continue
            self._run_owner[item.run_id] = serial
            if item.game_key:
                self._busy_games[item.game_key] = item.run_id
                self._game_by_run[item.run_id] = item.game_key
            self._waiting.remove(item)
            item.status = "assigned"
            item.device = serial
            item.wait_reason = None
        self._reindex_queue()

    def _idle_serial(self) -> str | None:
        for serial, runner in self._runners.items():
            device = self._devices.get(serial)
            if device is not None and device.status == "offline":
                continue
            snap = runner.snapshot()
            if snap["running"] is None and snap["queue_depth"] == 0:
                return serial
        return None

    def _reindex_queue(self) -> None:
        for index, item in enumerate(self._waiting, start=1):
            item.queue_position = index
