r"""把 Midscene Test 的运行产物登记进 SQLite（ingest）。

扫描 <result-dir>/*/summary.json，抽出运行级与用例级的字段写进三张表：
  runs  —— 一次运行一行（时间、状态、通过/失败数、报告文件路径）
  cases —— 一个用例一行（所属游戏、用例集、用例名、状态、失败原因、耗时）
  games —— 游戏键与展示名（从 game.config.ts 里读 name）

失败原因来自 attempts[].resultFile（每次尝试的步骤明细），提取：
  失败节点、失败阶段、错误码、错误信息、用例耗时。

幂等：同一个 run_id 重复导入会先删旧行再写新行，不会产生重复数据。
不调用任何模型、不连设备，纯本地解析。

用法：
  python server/ingest.py                     # 扫描 .midscene/test-results
  python server/ingest.py --dry-run           # 只统计，不写库
  python server/ingest.py --result-dir D:\x --db server\data\smoke.db
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULT_DIR = PROJECT_ROOT / ".midscene" / "test-results"
DEFAULT_DB = PROJECT_ROOT / "server" / "data" / "smoke.db"

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


def read_failure(run_dir: Path, attempt: dict) -> dict:
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

    # 依次找第一个失败的步骤；beforeEach 失败时 steps 通常是空的
    for phase in ("beforeEach", "steps", "afterEach"):
        failed = next(
            (step for step in (detail.get(phase) or []) if step.get("status") == "failed"),
            None,
        )
        if failed is None:
            continue
        error = failed.get("error") or {}
        message = (error.get("message") or "").strip()
        result.update(
            failed_phase=phase,
            failed_node=failed.get("node"),
            error_code=error.get("code") or error.get("name"),
            error_message=message[:500] or None,
        )
        break

    return result


def parse_summary(summary: dict, run_dir: Path) -> tuple[dict, list[dict]]:
    stats = summary.get("summary") or {}
    projects = summary.get("projects") or []

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
            failure = read_failure(run_dir, attempts[0]) if attempts else {}

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
                }
            )

    report = summary.get("report")
    report_file = None
    if report:
        # summary 里的 report 是相对本次运行目录的路径（如 ../../../midscene_run/report/...）
        report_file = str((run_dir / report).resolve())

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
  duration_ms, failed_node, failed_phase, error_code, error_message
) VALUES (
  :run_id, :project, :suite, :game_key, :source_path, :case_index, :name, :status, :attempts,
  :duration_ms, :failed_node, :failed_phase, :error_code, :error_message
)
"""


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
        if dry_run:
            ingested += 1
            continue

        assert conn is not None
        conn.execute("DELETE FROM cases WHERE run_id = ?", (run["run_id"],))
        conn.execute(RUN_INSERT, run)
        conn.executemany(CASE_INSERT, [{**case, "run_id": run["run_id"]} for case in cases])
        ingested += 1

    if conn is not None:
        if discovered_games:
            load_games(conn, discovered_games, now)
        conn.commit()
        conn.close()

    suffix = "（dry-run，未写库）" if dry_run else f" -> {db_path}"
    print(f"\n完成：导入 {ingested} 次运行，跳过 {skipped} 个坏文件{suffix}")
    return 0


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
    args = parser.parse_args()
    return ingest(args.result_dir, args.db, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
