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
import re
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
RESULT_DIR = PROJECT_ROOT / ".midscene" / "test-results"

# midscene 的进度行：`→ step 1/7: aiTap` / `✓ step 1/7: aiTap (15337 ms)`
PHASE_LINE = re.compile(
    r"^\s*([→✓✗])\s*(step|beforeAll|afterAll|beforeEach|afterEach)\s+(\d+)/(\d+):\s*(\S+?)(?:\s*\((\d+)\s*ms\))?\s*$"
)
CONTEXT_LINE = re.compile(r"^\s*\[(project|document|case)\s+(\d+)/(\d+)\]\s*(.*)$")

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
    game_key: str | None = None  # 指定游戏后只跑该游戏目录下的用例
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
    # 本次任务产生的 Midscene 运行记录（run-full.mjs 的 run 事件 + 目录快照兜底）
    runs: list[dict[str, Any]] = field(default_factory=list)
    result_snapshot: set[str] = field(default_factory=set)
    mode: str = "files"  # files | project（全选时自动切整包，复用会话）

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "files": self.files,
            "clean": self.clean,
            "game_key": self.game_key,
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
            "mode": self.mode,
            "runs": self.runs,
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

    def start(
        self,
        project: str,
        files: list[str],
        clean: bool = True,
        game_key: str | None = None,
    ) -> RunJob:
        with self._lock:
            waiting = [jid for jid in self._queue if self._jobs[jid].status == "queued"]
            if len(waiting) >= MAX_QUEUE:
                raise BusyError(f"队列已满（最多排队 {MAX_QUEUE} 个）")

            run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
            job = RunJob(run_id=run_id, project=project, files=files, clean=clean, game_key=game_key)
            # 未指定文件即整包模式；指定了则等 _build_args 按"是否全选"再定
            job.mode = "project" if not files else "files"
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

    def _suite_file_count(self, project: str, game_key: str | None = None) -> int:
        """该套件（或该游戏该套件）共有多少个用例文件，用于判断"是否全选"。"""
        try:
            from .case_index import list_cases, refresh_index

            refresh_index()
            return len(list_cases(game=game_key, suite=project))
        except Exception:  # 索引不可用时不阻断运行，退回逐文件模式
            return 0

    def _build_args(self, job: RunJob) -> list[str]:
        """命中"整包"条件时用 --only/--project 走单进程，复用会话。

        整包条件：未指定文件，或选中文件数 == 当前游戏（未指定游戏时是整个套件）的文件数。
        """
        args = ["--project", job.project, "--json"]
        if job.game_key:
            # run-full 会把它翻译成 midscene-test 的 --only games/<game>
            args += ["--game", job.game_key]
        if job.files:
            total = self._suite_file_count(job.project, job.game_key)
            if total and len(job.files) >= total:
                job.mode = "project"
                self._publish(
                    job,
                    "state",
                    {
                        "status": "running",
                        "mode": "project",
                        "note": f"已选全部 {total} 个文件，自动改用整包模式以复用会话",
                    },
                )
            else:
                job.mode = "files"
                args += ["--files", ",".join(job.files)]
        else:
            job.mode = "project"
        if not job.clean:
            args += ["--no-clean"]
        return args

    def _execute(self, job: RunJob) -> None:
        env = dict(os.environ)
        # NODE_ENV=production 会让 pnpm 跳装 devDependencies，子进程必须抹掉
        env.pop("NODE_ENV", None)
        env["PYTHONUNBUFFERED"] = "1"

        args = self._build_args(job)
        job.result_snapshot = self._snapshot_runs()
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

        if job.status == "cancelled":
            self._finalize_cancelled(job)

        self._publish(job, "done", {"status": job.status, "exit_code": exit_code, "runs": job.runs})

    def _find_python(self) -> str:
        for candidate in (
            PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
            PROJECT_ROOT / ".venv" / "bin" / "python",
        ):
            if candidate.exists():
                return str(candidate)
        return "python"

    def _finalize_cancelled(self, job: RunJob) -> None:
        """被取消时 run-full 没走到入库步骤，这里补一次，并把这几条运行标成 cancelled。"""
        run_ids = self._new_run_ids(job)
        if not run_ids:
            return
        python = self._find_python()
        ingest = str(PROJECT_ROOT / "server" / "ingest.py")
        try:
            subprocess.run(
                [python, ingest], cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=180
            )
            subprocess.run(
                [python, ingest, "--mark-cancelled", ",".join(run_ids)],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self._publish(
                job,
                "log",
                {"stream": "runner", "line": f"已把 {len(run_ids)} 条运行标记为已取消：{', '.join(run_ids)}"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            self._publish(job, "log", {"stream": "runner", "line": f"取消后入库失败：{error}"})

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
                # 只从 stdout 解析进度行：midscene 会同时往 stdout/stderr 输出同样内容
                if name == "stdout":
                    self._parse_progress(job, line)
                continue

            event_type = parsed.pop("type")
            if event_type == "step":
                job.current_step = parsed.get("label")
                self._publish(job, "step", parsed)
            elif event_type == "cmd":
                self._publish(job, "cmd", parsed)
            elif event_type == "log":
                self._publish(job, "log", parsed)
                if parsed.get("stream") == "stdout":
                    self._parse_progress(job, parsed.get("line", ""))
            elif event_type == "result":
                self._publish(job, "result", parsed)
            elif event_type == "run":
                job.runs.append(parsed)
                self._publish(job, "run", parsed)
            else:
                self._publish(job, event_type, parsed)
        try:
            stream.close()
        except OSError:
            pass

    def _parse_progress(self, job: RunJob, line: str) -> None:
        """把 `→ step 1/7: aiTap` / `✓ step 1/7: aiTap (15337 ms)` 配对成结构化事件。"""
        context = CONTEXT_LINE.match(line)
        if context:
            kind, index, total, label = context.groups()
            self._publish(
                job,
                "context",
                {"kind": kind, "index": int(index), "total": int(total), "label": label.strip()},
            )
            return

        phase = PHASE_LINE.match(line)
        if not phase:
            return
        mark, kind, index, total, node, ms = phase.groups()
        self._publish(
            job,
            "phase",
            {
                "phase": kind,
                "index": int(index),
                "total": int(total),
                "node": node,
                "status": "running" if mark == "→" else ("ok" if mark == "✓" else "fail"),
                "ms": int(ms) if ms else None,
            },
        )

    def _snapshot_runs(self) -> set[str]:
        if not RESULT_DIR.exists():
            return set()
        return {entry.name for entry in RESULT_DIR.iterdir() if entry.is_dir}

    def _new_run_ids(self, job: RunJob) -> list[str]:
        """优先用 run-full 报上来的；没报（比如中途被杀）就用目录快照差集兜底。"""
        ids = [item.get("run_id") for item in job.runs if item.get("run_id")]
        if ids:
            return ids
        return sorted(self._snapshot_runs() - job.result_snapshot)

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