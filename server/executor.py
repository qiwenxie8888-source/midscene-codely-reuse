"""执行器：把"跑一个任务"抽象成可替换的实现。

调度器只负责决定「谁在哪台设备上跑」，具体怎么跑由执行器负责：
  RealExecutor  —— 真正启动 run-full.mjs 子进程，注入 ANDROID_DEVICE_ID
  FakeExecutor  —— 用 sleep 模拟，不接设备、不调模型

拆开的好处是没有第二台设备时也能验证调度逻辑（并发、排队、取消），
接入真机时只换实现，调度器一行不用改。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Protocol

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_FULL = PROJECT_ROOT / "scripts" / "run-full.mjs"


class Executor(Protocol):
    def run(self, job, device) -> int:
        """执行一个任务，返回退出码。实现方要自行处理 job.cancel_event。"""
        ...


class FakeExecutor:
    """假执行器：sleep 模拟耗时，用来在没有设备时验证调度逻辑。"""

    def __init__(self, duration: float = 1.0) -> None:
        self.duration = duration
        self.calls: list[dict] = []

    def run(self, job, device) -> int:
        started = time.time()
        record = {
            "run_id": job.run_id,
            "game_key": job.game_key,
            "device": device.serial,
            "started": started,
            "finished": None,
            "cancelled": False,
        }
        self.calls.append(record)
        duration = getattr(job, "fake_duration", None) or self.duration
        while time.time() - started < duration:
            if job.cancel_event.is_set():
                record["cancelled"] = True
                record["finished"] = time.time()
                return -1
            time.sleep(0.02)
        record["finished"] = time.time()
        return 0


class RealExecutor:
    """真实执行器：启动 run-full.mjs，设备 id 通过环境变量传给子进程。"""

    def __init__(self, timeout_s: int = 30 * 60) -> None:
        self.timeout_s = timeout_s

    def _build_args(self, job) -> list[str]:
        args = ["node", str(RUN_FULL), "--project", job.project]
        if job.files:
            args += ["--files", ",".join(job.files)]
        if job.game_key:
            args += ["--game", job.game_key]
        if not job.clean:
            args.append("--no-clean")
        return args

    def run(self, job, device) -> int:
        env = dict(os.environ)
        # NODE_ENV=production 会让 pnpm 跳过 devDependencies，子进程必须抹掉
        env.pop("NODE_ENV", None)
        env["PYTHONUNBUFFERED"] = "1"
        # 关键：每台设备一个独立环境，不能继承父进程里可能存在的旧值
        env["ANDROID_DEVICE_ID"] = device.serial
        # 让清理脚本只处理本任务的进程，避免并行时互杀
        env["CODELY_TASK_ID"] = job.run_id

        try:
            process = subprocess.Popen(
                self._build_args(job),
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError:
            return 1

        job.process = process
        deadline = time.time() + self.timeout_s
        try:
            while process.poll() is None:
                if job.cancel_event.is_set() or time.time() > deadline:
                    self._kill(process)
                    return -1
                time.sleep(0.2)
            return process.returncode or 0
        finally:
            job.process = None

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                )
            else:
                process.kill()
        except OSError:
            pass


def make_executor(mode: str | None = None, fake_duration: float = 1.0) -> Executor:
    """按环境变量选择执行器，默认真实执行。"""
    mode = (mode or os.environ.get("RUNNER_EXECUTOR") or "real").lower()
    if mode == "fake":
        return FakeExecutor(duration=fake_duration)
    return RealExecutor()
