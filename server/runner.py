"""运行调度：单执行槽 + FIFO 队列 + 日志环形缓冲 + 事件广播。

为什么只允许一个任务在跑：真机只有一台，两个任务同时开会抢 adb 通道、互相点错屏幕。

对外接口（被 server/api.py 调用）：
  start(project, files, clean) -> RunJob      起一个任务（排队或立即执行）
  get(run_id) -> RunJob | None                查任务
  cancel(run_id) -> str                       取消（排队中直接出队，运行中杀进程树）
  iter_events(run_id, from_seq) -> Iterator   按序号增量产出事件，供 SSE 使用
  snapshot() -> dict                          给 /api/health 用的运行态摘要
  BusyError                                   队列已满

任务执行靠 spawn `node scripts/run-full.mjs --json`，
脚本每一步都会往 stdout 吐一行 JSON 事件（step / cmd / log / result），这里逐行解析转发。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_FULL = PROJECT_ROOT / "scripts" / "run-full.mjs"

MAX_QUEUE = 3  # 排队上限；再多就明确拒绝，而不是让用户以为点上了
MAX_LOG_LINES = 5000  # 每任务日志上限，超出丢头部（长任务也不会吃爆内存）
RUN_TIMEOUT_S = 30 * 60  # 硬超时，防止模型卡死在某个步骤上

TERMINAL_STATUS = ("passed", "failed", "cancelled", "interrupted")


class BusyError(RuntimeError):
    """队列已满。"""


@dataclass
class RunJob:
    run_id: str
    project: str
    files: list[str]
    clean: bool
    status: str = "queued"  # queued | running | passed | failed | cancelled | interrupted
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    current_step: str | None = None
    cancel_requested: bool = False
    seq: int = 0
    logs: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    process: subprocess.Popen | None = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "files": self.files,
            "clean": self.clean,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "current_step": self.current_step,
            "cancel_requested": self.cancel_requested,
            "log_lines": len(self.logs),
            "last_seq": self.seq,
            "truncated": self.truncated,
        }


class Runner:
    """单线程执行器：一个 worker 顺序消费队列。"""

    def __init__(self) -> None:
        self._jobs: dict[str, RunJob] = {}
        self._queue: deque[str] = deque()
        self._lock = threading.RLock()
        # SSE 订阅者等新事件用：有新事件或状态变化时 notify_all
        self._change = threading.Condition(self._lock)
        self._worker = threading.Thread(target=self._loop, name="runner-worker", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ 对外

    def start(self, project: str, files: list[str], clean: bool = True) -> RunJob:
        with self._lock:
            waiting = [jid for jid in self._queue if self._jobs[jid].status == "queued"]
            if len(waiting) >= MAX_QUEUE:
                raise BusyError(f"队列已满（最多排队 {MAX_QUEUE} 个）")

            run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
            job = RunJob(run_id=run_id, project=project, files=files, clean=clean)
            self._jobs[run_id] = job
            self._queue.append(run_id)
            self._publish(job, "state", {"status": job.status, "position": len(waiting)})
            self._change.notify_all()
            return job

    def get(self, run_id: str) -> RunJob | None:
        with self._lock:
            return self._jobs.get(run_id)

    def cancel(self, run_id: str) -> str:
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None:
                raise KeyError(run_id)
            if job.status in TERMINAL_STATUS:
                raise ValueError(f"任务已结束（{job.status}），无需取消")

            was = job.status
            job.cancel_requested = True
            if job.status == "queued":
                # 还没开始，直接从队列摘掉
                try:
                    self._queue.remove(run_id)
                except ValueError:
                    pass
                job.status = "cancelled"
                job.finished_at = time.time()
                self._publish(job, "state", {"status": "cancelled", "was": was})
                self._publish(job, "done", {"status": "cancelled", "exit_code": None})
            else:
                self._publish(job, "log", {"stream": "runner", "line": "收到取消请求，正在终止进程树…"})
                self._kill_tree(job)
            self._change.notify_all()
            return was

    def iter_events(self, run_id: str, from_seq: int = 0) -> Iterator[tuple[int, dict[str, Any]]]:
        """增量产出事件。先补历史，再等新事件，直到任务结束且事件发完。

        空等超时会产出一条 heartbeat，让上层能定期写 SSE 注释行，避免连接被中间层掐断。
        """
        cursor = max(0, from_seq)
        while True:
            # 注意：不能在持锁状态下 yield，否则生成器挂起时锁不释放，worker 会被卡死
            with self._change:
                job = self._jobs.get(run_id)
                if job is None:
                    raise KeyError(run_id)
                pending = [item for item in job.logs if item["seq"] > cursor]
                if not pending:
                    if job.status in TERMINAL_STATUS:
                        return
                    self._change.wait(timeout=15)
                    pending = [item for item in job.logs if item["seq"] > cursor]

            if not pending:
                # 这段时间没有新事件，交给上层发心跳
                yield cursor, {"type": "heartbeat"}
                continue

            for item in pending:
                cursor = item["seq"]
                yield cursor, item

    def log_slice(self, run_id: str, from_seq: int = 0) -> tuple[list[dict[str, Any]], int, str]:
        """给轮询式客户端用：直接取一段日志，不需要维持长连接。"""
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None:
                raise KeyError(run_id)
            items = [item for item in job.logs if item["seq"] > from_seq]
            return items, job.seq, job.status

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = [j.to_dict() for j in self._jobs.values() if j.status == "running"]
            queued = [j.to_dict() for j in self._jobs.values() if j.status == "queued"]
            return {
                "running": running[0] if running else None,
                "running_count": len(running),
                "queued": queued,
                "queue_depth": len(queued),
                "max_queue": MAX_QUEUE,
            }

    # ------------------------------------------------------------------ 内部

    def _publish(self, job: RunJob, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            if job.logs.maxlen and len(job.logs) == job.logs.maxlen:
                job.truncated = True
            job.seq += 1
            job.logs.append(
                {
                    "seq": job.seq,
                    "ts": time.strftime("%H:%M:%S"),
                    "type": event_type,
                    **payload,
                }
            )
            self._change.notify_all()

    def _loop(self) -> None:
        while True:
            with self._lock:
                while not self._queue:
                    self._change.wait(timeout=5)
                run_id = self._queue.popleft()
                job = self._jobs[run_id]
                if job.status != "queued":
                    continue
                job.status = "running"
                job.started_at = time.time()

            self._execute(job)

    def _build_args(self, job: RunJob) -> list[str]:
        args = ["--project", job.project, "--json"]
        if job.files:
            args += ["--files", ",".join(job.files)]
        if not job.clean:
            args += ["--no-clean"]
        return args

    def _execute(self, job: RunJob) -> None:
        env = dict(os.environ)
        # NODE_ENV=production 会让 pnpm 跳装 devDependencies，子进程必须抹掉
        env.pop("NODE_ENV", None)
        env["PYTHONUNBUFFERED"] = "1"

        args = self._build_args(job)
        self._publish(job, "state", {"status": "running", "command": ["node", str(RUN_FULL), *args]})

        try:
            process = subprocess.Popen(
                ["node", str(RUN_FULL), *args],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as error:
            job.status = "failed"
            job.finished_at = time.time()
            self._publish(job, "log", {"stream": "runner", "line": f"启动失败：{error}"})
            self._publish(job, "done", {"status": "failed", "exit_code": None})
            return

        with self._lock:
            job.process = process

        readers = [
            threading.Thread(target=self._pump, args=(job, process.stdout, "stdout"), daemon=True),
            threading.Thread(target=self._pump, args=(job, process.stderr, "stderr"), daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = time.time() + RUN_TIMEOUT_S

        def on_timeout() -> None:
            if process.poll() is None:
                self._publish(
                    job,
                    "log",
                    {"stream": "runner", "line": f"超过 {RUN_TIMEOUT_S // 60} 分钟硬超时，强制终止"},
                )
                job.cancel_requested = True
                self._kill_tree(job)

        timer = threading.Timer(RUN_TIMEOUT_S, on_timeout)
        timer.daemon = True
        timer.start()

        exit_code = process.wait()
        timer.cancel()
        for reader in readers:
            reader.join(timeout=5)

        with self._lock:
            job.process = None
            job.exit_code = exit_code
            job.finished_at = time.time()
            if job.cancel_requested:
                job.status = "cancelled"
            elif exit_code == 0:
                job.status = "passed"
            else:
                job.status = "failed"

        self._publish(job, "done", {"status": job.status, "exit_code": exit_code})

    def _pump(self, job: RunJob, stream, name: str) -> None:
        """把子进程的一行读出来：能解析成 JSON 事件的当事件，否则当普通日志行。"""
        if stream is None:
            return
        for raw in stream:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            parsed: dict[str, Any] | None = None
            if name == "stdout" and line.startswith("{"):
                try:
                    candidate = json.loads(line)
                    if isinstance(candidate, dict) and "type" in candidate:
                        parsed = candidate
                except json.JSONDecodeError:
                    parsed = None

            if parsed is None:
                self._publish(job, "log", {"stream": name, "line": line})
                continue

            event_type = parsed.pop("type")
            if event_type == "step":
                job.current_step = parsed.get("label")
                self._publish(job, "step", parsed)
            elif event_type == "cmd":
                self._publish(job, "cmd", parsed)
            elif event_type == "log":
                self._publish(job, "log", parsed)
            elif event_type == "result":
                self._publish(job, "result", parsed)
            else:
                self._publish(job, event_type, parsed)
        try:
            stream.close()
        except OSError:
            pass

    def _kill_tree(self, job: RunJob) -> None:
        """Windows 必须 /T 连子孙一起杀，否则 codely.exe 会变孤儿继续占着。"""
        process = job.process
        if process is None or process.poll() is not None:
            self._publish(job, "log", {"stream": "runner", "line": "进程已退出，无需终止"})
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            else:
                process.terminate()
        except (OSError, subprocess.SubprocessError) as error:
            self._publish(job, "log", {"stream": "runner", "line": f"终止进程失败：{error}"})


runner = Runner()