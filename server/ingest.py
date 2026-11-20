r"""把 Midscene Test 的运行产物登记进 SQLite（ingest）。

扫描 <result-dir>/*/summary.json，抽出运行级与用例级的字段写进三张表：
  runs  —— 一次运行一行（时间、状态、通过/失败数、报告文件路径）
  cases —— 一个用例一行（所属游戏、用例集、用例名、状态、失败原因、耗时）
  games —— 游戏键与展示名（从 game.config.ts 里读 name）

失败原因来自两处：attempts[].resultFile（用例级失败）和 documents[].resultFile
（beforeAll/afterAll 失败——这时用例根本没跑，summary 里记成 notRun），提取：
  失败节点、失败阶段、错误码、错误信息摘要、完整观察文本（error_detail）、
  失败步骤的 dump（复制到 server/data/failure-dumps，避免被清理删掉）、
  失败步骤时间窗内的最后一张截图（evidence_shot）。

幂等：同一个 run_id 重复导入会先删旧行再写新行，不会产生重复数据。
不调用任何模型、不连设备，纯本地解析。

用法：
  python server/ingest.py                     # 扫描 .midscene/test-results
  python server/ingest.py --dry-run           # 只统计，不写库
  python server/ingest.py --result-dir D:\x --db server\data\smoke.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml

try:  # 包模式：uvicorn server.api:app 时由 api 导入本模块
    from .causes import CATEGORIES as CAUSE_CATEGORIES
    from .causes import classify_cause
except ImportError:  # 脚本模式：python server/ingest.py
    from causes import CATEGORIES as CAUSE_CATEGORIES
    from causes import classify_cause

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULT_DIR = PROJECT_ROOT / ".midscene" / "test-results"
DEFAULT_DB = PROJECT_ROOT / "server" / "data" / "smoke.db"
REPORT_ROOT = PROJECT_ROOT / "midscene_run" / "report"
# 失败步骤的 dump 会被 clean_runs --purge-loose 删掉，所以 ingest 时先复制一份
DUMPS_ROOT = PROJECT_ROOT / "server" / "data" / "failure-dumps"

# 报告里每条截图引用都带 capturedAt 与相对路径；同一张图可能被引用多次
SHOT_REF = re.compile(
    r'"capturedAt":(\d{13}),"mimeType":"image/(?:jpeg|png)",'
    r'"storage":"file","path":"(\./screenshots/[^"]+)"'
)
# 完整观察文本只用于归因与人工复核，截断时保留尾部（真死因通常写在最后）
ERROR_DETAIL_LIMIT = 4000

# games/<渠道>/<游戏>/<用例集>/xxx.yaml
GAME_PATH = re.compile(r"^games/([^/]+)/([^/]+)/([^/]+)/")
# 旧布局：cases/<用例集>/xxx.yaml（game_key 解析不到，留空）
LEGACY_PATH = re.compile(r"^cases/(smoke|explore)/")
# game.config.ts 里的展示名，如 name: '边缘空间',
GAME_NAME = re.compile(r"""^\s*name:\s*['"]([^'"]+)['"]""", re.MULTILINE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id           TEXT PRIMARY KEY,
  started_at       TEXT,
  ended_at         TEXT,
  duration_ms      INTEGER,
  status           TEXT,
  exit_code        INTEGER,
  suites           TEXT,
  games            TEXT,
  total            INTEGER,
  passed           INTEGER,
  failed           INTEGER,
  not_run          INTEGER,
  filtered         INTEGER,
  collection_errors INTEGER,
  final_pass_rate  REAL,
  first_pass_rate  REAL,
  schema_version   INTEGER,
  report_file      TEXT,
  result_dir       TEXT,
  ingested_at      TEXT
);

CREATE TABLE IF NOT EXISTS cases (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  project     TEXT,
  suite       TEXT,
  game_key    TEXT,
  source_path TEXT,
  case_index  INTEGER,
  name        TEXT,
  status      TEXT,
  attempts    INTEGER,
  duration_ms   INTEGER,
  failed_node   TEXT,
  failed_phase  TEXT,
  error_code    TEXT,
  error_message TEXT,
  cause_category   TEXT,
  cause_source     TEXT,
  cause_reason     TEXT,
  cause_confidence REAL,
  file_hash    TEXT,
  UNIQUE (run_id, source_path, case_index)
);

CREATE TABLE IF NOT EXISTS games (
  game_key     TEXT PRIMARY KEY,
  channel      TEXT,
  game_dir     TEXT,
  display_name TEXT,
  config_file  TEXT,
  updated_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_cases_run ON cases(run_id);
CREATE INDEX IF NOT EXISTS idx_cases_game ON cases(game_key, status);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
"""

# 老库升级：缺列时补上，不动已有数据
CASE_COLUMNS = [
    ("duration_ms", "INTEGER"),
    ("failed_node", "TEXT"),
    ("failed_phase", "TEXT"),
    ("error_code", "TEXT"),
    ("error_message", "TEXT"),
    ("cause_category", "TEXT"),
    ("cause_source", "TEXT"),
    ("cause_reason", "TEXT"),
    ("cause_confidence", "REAL"),
    ("error_detail", "TEXT"),
    ("evidence_shot", "TEXT"),
    ("evidence_dump", "TEXT"),
    ("file_hash", "TEXT"),
]


def ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cases)")}
    for name, ddl in CASE_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE cases ADD COLUMN {name} {ddl}")


def build_case_file_index() -> dict[str, set[tuple[str, str]]]:
    """文件名 -> {(game_key, suite)}。

    单文件模式（`midscene-test <某个.yaml>`）下 summary 里的 sourcePath 只剩文件名，
    路径信息丢失，只能靠文件名反查它属于哪个渠道/游戏/用例集。
    """
    index: dict[str, set[tuple[str, str]]] = {}
    games_root = PROJECT_ROOT / "games"
    if not games_root.exists():
        return index
    for path in games_root.glob("*/*/*/*.yaml"):
        parts = path.relative_to(PROJECT_ROOT).parts
        if len(parts) != 5:  # games/<渠道>/<游戏>/<用例集>/<文件>
            continue
        _, channel, game, suite, name = parts
        index.setdefault(name, set()).add((f"{channel}/{game}", suite))
    return index


CASE_FILE_INDEX: dict[str, set[tuple[str, str]]] = build_case_file_index()


@lru_cache(maxsize=256)
def yaml_case_names(path_str: str) -> frozenset[str]:
    """读用例文件里的 cases[].name，用于在多个同名文件间认出正确的那份。"""
    try:
        document = yaml.safe_load(Path(path_str).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏文件不影响统计
        return frozenset()
    cases = (document or {}).get("cases") or []
    return frozenset(
        str(case["name"])
        for case in cases
        if isinstance(case, dict) and case.get("name")
    )


def case_file_hash(
    source_path: str,
    game_key: str | None,
    suite: str | None,
    case_name: str | None = None,
) -> str | None:
    """按用例文件内容算指纹，用于 flake 分代。

    只认独一无二的文件：完整路径直接读；裸文件名优先用 game_key + suite 拼。
    同名文件分属多个游戏时，再用用例名区分（单文件运行拿不到 game_key，
    一个文件名可能对应多个游戏，这时候用例名是唯一能对上的线索）。
    """
    candidate: Path | None = None
    if "/" in source_path or "\\" in source_path:
        direct = PROJECT_ROOT / source_path
        if direct.exists():
            candidate = direct
    if candidate is None and game_key and suite:
        named = PROJECT_ROOT / "games" / game_key / suite / os.path.basename(source_path)
        if named.exists():
            candidate = named
    if candidate is None:
        matches = list(
            (PROJECT_ROOT / "games").glob(f"*/*/*/{os.path.basename(source_path)}")
        )
        if len(matches) == 1:
            candidate = matches[0]
        elif case_name:
            matched = [
                path
                for path in matches
                if case_name in yaml_case_names(str(path))
            ]
            if len(matched) == 1:
                candidate = matched[0]
                # 从路径补出 game_key，才能把该游戏的配置一起算进指纹
                if not game_key:
                    rel = candidate.relative_to(PROJECT_ROOT / "games")
                    if len(rel.parts) >= 3:
                        game_key = f"{rel.parts[0]}/{rel.parts[1]}"
    if candidate is None:
        return None
    try:
        content = candidate.read_bytes()
    except OSError:
        return None
    # 游戏的进入链配置（cardPrompt / menuPrompt）同样决定用例行为，一起算进指纹：
    # 只改用例 YAML 会切代，改配置也要切，否则改造后的数据会和旧数据混算
    if game_key:
        try:
            content += (PROJECT_ROOT / "games" / game_key / "game.config.ts").read_bytes()
        except OSError:
            pass
    return hashlib.sha256(content).hexdigest()[:12]


def classify(
    source_path: str,
    index: dict[str, set[tuple[str, str]]] | None = None,
) -> tuple[str | None, str | None]:
    """从用例文件路径反推 (game_key, suite)。解析不出就返回 None，绝不猜。"""
    match = GAME_PATH.match(source_path)
    if match:
        channel, game, suite = match.groups()
        return f"{channel}/{game}", suite
    match = LEGACY_PATH.match(source_path)
    if match:
        return None, match.group(1)
    # 单文件模式：sourcePath 只剩文件名，用文件索引反查；同名文件分属多个游戏时不猜
    if "/" not in source_path and "\\" not in source_path:
        candidates = (index if index is not None else CASE_FILE_INDEX).get(source_path)
        if candidates and len(candidates) == 1:
            return next(iter(candidates))
    return None, None


def truncate_tail(text: str, limit: int) -> str:
    """超长文本保留尾部——失败原因通常写在最后一句。"""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"…（前 {omitted} 字已省略）\n{text[-limit:]}"


def error_chain_text(error: dict | None) -> str | None:
    """把 error.message 连同 cause 链拼成一段可读证据。

    外层 message 常是 `Node "x" failed: <原因>`，cause 里是同一个原因的全文，
    所以被包含的重复段落要丢掉；同时把各层错误码记在末尾，便于规则匹配。
    """
    messages: list[str] = []
    codes: list[str] = []
    node = error if isinstance(error, dict) else {}
    for _ in range(5):
        message = str(node.get("message") or "").strip()
        code = node.get("code") or node.get("name")
        if code:
            codes.append(str(code))
        if message:
            kept = [item for item in messages if item not in message]
            if len(kept) == len(messages) and any(message in item for item in messages):
                pass  # 新消息已被更长的旧消息覆盖
            else:
                messages = kept
                messages.append(message)
        node = node.get("cause") if isinstance(node.get("cause"), dict) else {}
        if not node:
            break
    if not messages:
        return None
    text = "\n[cause] ".join(messages)
    if codes:
        text = f"{text}\n[codes] {' > '.join(dict.fromkeys(codes))}"
    return truncate_tail(text, ERROR_DETAIL_LIMIT)


@lru_cache(maxsize=8)
def load_shot_refs(html_path: str) -> dict[str, int]:
    """解析 e2e 报告里的截图引用：相对路径 -> 截图时间戳（毫秒）。

    报告是几 MB 的打包页面，按运行缓存一次即可；同一张图会被引用多次，按路径去重。
    """
    try:
        text = Path(html_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    refs: dict[str, int] = {}
    for match in SHOT_REF.finditer(text):
        captured, relative = int(match.group(1)), match.group(2)
        refs[relative] = max(refs.get(relative, 0), captured)
    return refs


def parse_epoch_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def find_failure_shot(
    report_file: str | Path | None,
    started_at: str | None,
    ended_at: str | None,
) -> str | None:
    """在 e2e 报告里找失败步骤时间窗内的最后一张截图。

    返回相对报告根目录的路径（`/reports` 静态挂载可直接访问）。
    命中不了返回 None：旧报告、早期自定义节点都可能没有图，调用方不要当错误。
    """
    if not report_file:
        return None
    start = parse_epoch_ms(started_at)
    end = parse_epoch_ms(ended_at)
    if start is None or end is None:
        return None
    html_path = Path(report_file)
    refs = load_shot_refs(str(html_path))
    hits = [
        (captured, relative)
        for relative, captured in refs.items()
        if start - 1000 <= captured <= end + 1000
    ]
    if not hits:
        return None
    _, relative = max(hits)
    return f"{html_path.parent.name}/{relative.removeprefix('./')}"


def save_dump_sources(run_id: str, sources: list | None) -> str | None:
    """把失败步骤的 dump 复制到 server/data/failure-dumps/<runId>/。

    dump 会被 clean_runs --purge-loose 删掉，所以必须在 ingest（清理之前）留存。
    一个步骤可能有多份（多个 execution），存成 JSON 数组文本；一份都没存到就返回 None。
    """
    saved: list[str] = []
    target = DUMPS_ROOT / run_id
    for source in sources or []:
        raw = source.get("dumpPath") if isinstance(source, dict) else None
        if not raw:
            continue
        dump = Path(raw)
        if not dump.exists():
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            dest = target / dump.name
            if not dest.exists():
                shutil.copy2(dump, dest)
            saved.append(dest.relative_to(PROJECT_ROOT).as_posix())
        except OSError:
            continue
    return json.dumps(saved, ensure_ascii=False) if saved else None


def failure_from_step(
    run_id: str,
    phase: str,
    failed: dict,
    report_file: str | Path | None,
) -> dict:
    """把一个失败步骤转成入库字段（用例级与文档级共用）。"""
    error = failed.get("error") or {}
    message = (error.get("message") or "").strip()
    return {
        "failed_phase": phase,
        "failed_node": failed.get("node"),
        "error_code": error.get("code") or error.get("name"),
        "error_message": message[:500] or None,
        "error_detail": error_chain_text(error),
        "evidence_dump": save_dump_sources(run_id, (failed.get("report") or {}).get("sources")),
        "evidence_shot": find_failure_shot(
            report_file, failed.get("startedAt"), failed.get("endedAt")
        ),
    }


def read_failure(
    run_dir: Path,
    attempt: dict,
    report_file: str | Path | None = None,
) -> dict:
    """从 attempt 的 resultFile 里提取失败原因与耗时。取不到就返回空字典。"""
    relative = attempt.get("resultFile")
    if not relative:
        return {}
    path = run_dir / relative
    try:
        detail = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    result: dict = {"duration_ms": detail.get("durationMs")}

    # 依次找真正让用例失败的步骤；beforeEach 失败时 steps 通常是空的。
    # 标了 continue-on-error 的步骤（continuedAfterError=true）只是"允许失败"，
    # 不是死因——否则会把被吞掉的超时误当成失败原因。
    for phase in ("beforeEach", "steps", "afterEach"):
        failed_steps = [
            step for step in (detail.get(phase) or []) if step.get("status") == "failed"
        ]
        if not failed_steps:
            continue
        failed = next(
            (step for step in failed_steps if not step.get("continuedAfterError")),
            failed_steps[0],
        )
        result.update(failure_from_step(run_dir.name, phase, failed, report_file))
        break

    return result


def read_document_failure(
    run_dir: Path,
    document: dict,
    report_file: str | Path | None = None,
) -> dict:
    """beforeAll/afterAll 失败时，证据在 document.json 里而不是 attempts 里。

    这类失败意味着用例根本没跑（summary 里记成 notRun），但"进不去游戏"恰恰是最常见的
    失败形态，不覆盖它归因体系就是瞎的：summary.cases[].attempts 为空，只有这里能拿到
    错误码与模型当时的观察文本。
    """
    relative = document.get("resultFile")
    if not relative:
        return {}
    try:
        detail = json.loads((run_dir / relative).read_text(encoding="utf-8"))
    except Exception:
        return {}

    for phase in ("beforeAll", "beforeEach", "afterAll"):
        failed_steps = [
            step for step in (detail.get(phase) or []) if step.get("status") == "failed"
        ]
        if not failed_steps:
            continue
        failed = next(
            (step for step in failed_steps if not step.get("continuedAfterError")),
            failed_steps[0],
        )
        failure = failure_from_step(run_dir.name, phase, failed, report_file)
        # 用例没跑，耗时只能取失败步骤自己的
        failure["duration_ms"] = failed.get("durationMs")
        # 文档级步骤没有 step.report，dump 挂在 document 的 reportSources 上
        if not failure.get("evidence_dump"):
            failure["evidence_dump"] = save_dump_sources(
                run_dir.name, detail.get("reportSources")
            )
        return failure
    return {}


def parse_summary(summary: dict, run_dir: Path) -> tuple[dict, list[dict]]:
    stats = summary.get("summary") or {}
    projects = summary.get("projects") or []

    # summary 里的 report 是相对本次运行目录的路径（如 ../../../midscene_run/report/...），
    # 失败截图要从这个 e2e 报告的 screenshots 里找，所以先算出来
    report_file: str | None = None
    report = summary.get("report")
    if report:
        report_file = str((run_dir / report).resolve())

    cases: list[dict] = []
    games: set[str] = set()
    suites: set[str] = set()

    for project in projects:
        project_name = project.get("name")
        if project_name:
            suites.add(project_name)
        for case in project.get("cases") or []:
            source_path = (case.get("sourcePath") or "").lstrip("/")
            game_key, suite = classify(source_path)
            if not suite:
                suite = project_name
            if game_key:
                games.add(game_key)

            attempts = case.get("attempts") or []
            failure = read_failure(run_dir, attempts[0], report_file) if attempts else {}

            # 只对失败的用例做规则定性；成功/未跑的行留空
            cause_category = cause_source = cause_reason = None
            cause_confidence = None
            if case.get("status") == "failed":
                category, reason, confidence = classify_cause(
                    failure.get("error_code"),
                    failure.get("error_message"),
                    failure.get("failed_node"),
                )
                cause_category, cause_source, cause_reason, cause_confidence = (
                    category,
                    "rule",
                    reason,
                    confidence,
                )

            cases.append(
                {
                    "project": project_name,
                    "suite": suite,
                    "game_key": game_key,
                    "source_path": source_path,
                    "case_index": case.get("caseIndex"),
                    "name": case.get("name"),
                    "status": case.get("status"),
                    "attempts": len(attempts),
                    "duration_ms": failure.get("duration_ms"),
                    "failed_node": failure.get("failed_node"),
                    "failed_phase": failure.get("failed_phase"),
                    "error_code": failure.get("error_code"),
                    "error_message": failure.get("error_message"),
                    "error_detail": failure.get("error_detail"),
                    "evidence_shot": failure.get("evidence_shot"),
                    "evidence_dump": failure.get("evidence_dump"),
                    "cause_category": cause_category,
                    "cause_source": cause_source,
                    "cause_reason": cause_reason,
                    "cause_confidence": cause_confidence,
                }
            )

        # beforeAll/afterAll 失败时用例是 notRun，证据只在 document.json 里；
        # 这里把错误信息贴到同一 source_path 的行上，但**不改 status**——
        # 改了会让用例级统计和 summary.json 对不上，而这段数据只服务于归因。
        for document in project.get("documents") or []:
            if document.get("status") != "failed":
                continue
            lifecycle = read_document_failure(run_dir, document, report_file)
            if not lifecycle:
                continue
            document_path = (document.get("sourcePath") or "").lstrip("/")
            document_name = Path(document_path).name
            for case in cases:
                # 单文件模式下 summary 里只剩文件名，目录模式下才是完整路径，
                # 所以两种形态都要能对上
                case_path = case.get("source_path") or ""
                if case.get("error_code"):
                    continue
                if case_path != document_path and Path(case_path).name != document_name:
                    continue
                case.update(lifecycle)
                category, reason, confidence = classify_cause(
                    lifecycle.get("error_code"),
                    lifecycle.get("error_message"),
                    lifecycle.get("failed_node"),
                )
                case.update(
                    cause_category=category,
                    cause_source="rule",
                    cause_reason=reason,
                    cause_confidence=confidence,
                )

    run = {
        "run_id": summary.get("runId") or run_dir.name,
        "started_at": summary.get("startedAt"),
        "ended_at": summary.get("endedAt"),
        "duration_ms": summary.get("durationMs"),
        "status": summary.get("status"),
        "exit_code": summary.get("exitCode"),
        "suites": ",".join(sorted(suites)) or None,
        "games": ",".join(sorted(games)) or None,
        "total": stats.get("total"),
        "passed": stats.get("passed"),
        "failed": stats.get("failed"),
        "not_run": stats.get("notRun"),
        "filtered": stats.get("filtered"),
        "collection_errors": stats.get("collectionErrors"),
        "final_pass_rate": stats.get("finalPassRate"),
        "first_pass_rate": stats.get("firstPassRate"),
        "schema_version": summary.get("schemaVersion"),
        "report_file": report_file,
        "result_dir": str(run_dir),
    }
    return run, cases


def load_games(conn: sqlite3.Connection, game_keys: set[str], now: str) -> int:
    """把用例里出现的 game_key 登记到 games 表，并从 game.config.ts 取展示名。

    配置是 TypeScript，Python 不解析 TS，只按约定抓 'name: ...' 这一行；
    抓不到就退回目录名，不影响其他功能。
    """
    for game_key in sorted(game_keys):
        channel, _, game_dir = game_key.partition("/")
        config_file = PROJECT_ROOT / "games" / channel / game_dir / "game.config.ts"
        display_name = None
        if config_file.exists():
            match = GAME_NAME.search(config_file.read_text(encoding="utf-8"))
            display_name = match.group(1) if match else None
        conn.execute(
            """
            INSERT INTO games (game_key, channel, game_dir, display_name, config_file, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_key) DO UPDATE SET
              channel = excluded.channel,
              game_dir = excluded.game_dir,
              display_name = excluded.display_name,
              config_file = excluded.config_file,
              updated_at = excluded.updated_at
            """,
            (
                game_key,
                channel,
                game_dir,
                display_name,
                str(config_file) if config_file.exists() else None,
                now,
            ),
        )
    return len(game_keys)


RUN_INSERT = """
INSERT INTO runs (
  run_id, started_at, ended_at, duration_ms, status, exit_code, suites, games,
  total, passed, failed, not_run, filtered, collection_errors,
  final_pass_rate, first_pass_rate, schema_version, report_file, result_dir, ingested_at
) VALUES (
  :run_id, :started_at, :ended_at, :duration_ms, :status, :exit_code, :suites, :games,
  :total, :passed, :failed, :not_run, :filtered, :collection_errors,
  :final_pass_rate, :first_pass_rate, :schema_version, :report_file, :result_dir, :ingested_at
)
ON CONFLICT(run_id) DO UPDATE SET
  started_at = excluded.started_at,
  ended_at = excluded.ended_at,
  duration_ms = excluded.duration_ms,
  status = excluded.status,
  exit_code = excluded.exit_code,
  suites = excluded.suites,
  games = excluded.games,
  total = excluded.total,
  passed = excluded.passed,
  failed = excluded.failed,
  not_run = excluded.not_run,
  filtered = excluded.filtered,
  collection_errors = excluded.collection_errors,
  final_pass_rate = excluded.final_pass_rate,
  first_pass_rate = excluded.first_pass_rate,
  schema_version = excluded.schema_version,
  report_file = excluded.report_file,
  result_dir = excluded.result_dir,
  ingested_at = excluded.ingested_at
"""

CASE_INSERT = """
INSERT OR IGNORE INTO cases (
  run_id, project, suite, game_key, source_path, case_index, name, status, attempts,
  duration_ms, failed_node, failed_phase, error_code, error_message,
  error_detail, evidence_shot, evidence_dump,
  cause_category, cause_source, cause_reason, cause_confidence, file_hash
) VALUES (
  :run_id, :project, :suite, :game_key, :source_path, :case_index, :name, :status, :attempts,
  :duration_ms, :failed_node, :failed_phase, :error_code, :error_message,
  :error_detail, :evidence_shot, :evidence_dump,
  :cause_category, :cause_source, :cause_reason, :cause_confidence, :file_hash
)
"""

# 重跑 ingest 不能按当前文件重算历史行的指纹：否则改完用例再跑一次 ingest，
# 旧运行会被伪装成新版本的成绩，分代就失效了。有旧值就沿用，包括空值。
FILE_HASH_SELECT = """
SELECT source_path, case_index, file_hash FROM cases WHERE run_id = ?
"""

# 人工改判的结论要在重跑 ingest 时保住：先快照，重写完再贴回去
MANUAL_CAUSE_SELECT = """
SELECT source_path, case_index, cause_category, cause_reason, cause_confidence
FROM cases WHERE run_id = ? AND cause_source = 'manual'
"""

MANUAL_CAUSE_UPDATE = """
UPDATE cases
SET cause_category = ?, cause_reason = ?, cause_confidence = ?, cause_source = 'manual'
WHERE run_id = ? AND source_path = ? AND case_index = ?
"""


def load_manual_causes(
    conn: sqlite3.Connection, run_id: str
) -> dict[tuple[str, int], tuple[str | None, str | None, float | None]]:
    """取出这次运行里已人工改判的行。"""
    rows = conn.execute(MANUAL_CAUSE_SELECT, (run_id,)).fetchall()
    return {
        (row[0], row[1]): (row[2], row[3], row[4])
        for row in rows
        if row[0] is not None and row[1] is not None
    }


def restore_manual_causes(
    conn: sqlite3.Connection,
    run_id: str,
    saved: dict[tuple[str, int], tuple[str | None, str | None, float | None]],
) -> int:
    """把人工结论贴回重写完的用例行；规则结论不参与，永远可被重算覆盖。"""
    restored = 0
    for (source_path, case_index), (category, reason, confidence) in saved.items():
        cursor = conn.execute(
            MANUAL_CAUSE_UPDATE, (category, reason, confidence, run_id, source_path, case_index)
        )
        restored += cursor.rowcount
    return restored


def ingest(result_dir: Path, db_path: Path, dry_run: bool) -> int:
    summaries = sorted(result_dir.glob("*/summary.json"))
    if not summaries:
        print(f"没有找到 summary.json：{result_dir}")
        return 0

    conn = None
    if not dry_run:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        ensure_columns(conn)

    ingested = skipped = 0
    discovered_games: set[str] = set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for summary_path in summaries:
        run_dir = summary_path.parent
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - 单个坏文件不影响其他运行
            print(f"跳过 {run_dir.name}：{error}")
            skipped += 1
            continue

        run, cases = parse_summary(summary, run_dir)
        run["ingested_at"] = now
        discovered_games.update(
            case["game_key"] for case in cases if case["game_key"]
        )

        failures = sum(1 for case in cases if case["status"] == "failed")
        with_error = sum(1 for case in cases if case["error_code"])
        print(
            f"{run['run_id']}  status={run['status']}  "
            f"总={run['total']} 通过={run['passed']} 失败={run['failed']}  "
            f"用例行={len(cases)}  有失败原因={with_error}  游戏={run['games'] or '-'}"
        )
        if failures:
            counts: dict[str, int] = {}
            for case in cases:
                if case["status"] != "failed":
                    continue
                key = case["cause_category"] or "unknown"
                counts[key] = counts.get(key, 0) + 1
            detail = "  ".join(
                f"{CAUSE_CATEGORIES.get(key, {}).get('label', key)}={value}"
                for key, value in sorted(counts.items(), key=lambda item: -item[1])
            )
            print(f"    定性：{detail}")
        if dry_run:
            ingested += 1
            continue

        assert conn is not None
        # 先记住人工改判过的行，重写完再贴回去（规则结论允许被重算）
        saved = load_manual_causes(conn, run["run_id"])
        # 指纹同理：这张表以前入过库的话，沿用当时的指纹，不按当前文件重算
        saved_hashes = {
            (row[0], row[1]): row[2]
            for row in conn.execute(FILE_HASH_SELECT, (run["run_id"],)).fetchall()
            if row[0] is not None and row[1] is not None
        }
        conn.execute("DELETE FROM cases WHERE run_id = ?", (run["run_id"],))
        conn.execute(RUN_INSERT, run)
        rows = []
        for case in cases:
            key = (case["source_path"], case["case_index"])
            file_hash = (
                saved_hashes[key]
                if key in saved_hashes
                else case_file_hash(
                    case["source_path"], case["game_key"], case["suite"], case.get("name")
                )
            )
            rows.append({**case, "run_id": run["run_id"], "file_hash": file_hash})
        conn.executemany(CASE_INSERT, rows)
        restored = restore_manual_causes(conn, run["run_id"], saved)
        if restored:
            print(f"    保留人工定性：{restored} 条")
        ingested += 1

    if conn is not None:
        if discovered_games:
            load_games(conn, discovered_games, now)
        conn.commit()
        conn.close()

    suffix = "（dry-run，未写库）" if dry_run else f" -> {db_path}"
    print(f"\n完成：导入 {ingested} 次运行，跳过 {skipped} 个坏文件{suffix}")
    return 0


def mark_cancelled(db_path: Path, run_ids: list[str]) -> int:
    """把被取消的运行标成 cancelled。取消时 run-full 被强杀，summary 可能不完整，
    这里直接改状态，让看板把它和"失败"区分开（不计入通过率）。"""
    if not run_ids:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        changed = 0
        for run_id in run_ids:
            cursor = conn.execute("UPDATE runs SET status = 'cancelled' WHERE run_id = ?", (run_id,))
            if cursor.rowcount == 0:
                # 运行目录存在但没被导入（summary 没写完），补一条最小记录
                conn.execute(
                    "INSERT INTO runs (run_id, status, ingested_at) VALUES (?, 'cancelled', ?)",
                    (run_id, datetime.now(timezone.utc).isoformat()),
                )
            changed += 1
        conn.commit()
    finally:
        conn.close()
    return changed


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="把 Midscene Test 运行产物导入 SQLite")
    parser.add_argument(
        "--result-dir",
        type=Path,
        default=DEFAULT_RESULT_DIR,
        help="运行产物根目录（默认 .midscene/test-results）",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写库")
    parser.add_argument(
        "--mark-cancelled",
        default=None,
        help="把逗号分隔的 run_id 标记为已取消（取消运行后由 runner 调用）",
    )
    args = parser.parse_args()
    if args.mark_cancelled:
        ids = [item.strip() for item in args.mark_cancelled.split(",") if item.strip()]
        count = mark_cancelled(args.db, ids)
        print(f"已标记 {count} 条运行为 cancelled")
        return 0
    return ingest(args.result_dir, args.db, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
