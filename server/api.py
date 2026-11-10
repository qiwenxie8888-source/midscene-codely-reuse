"""运行看板 API：读库 + 触发/观测/取消测试运行。

启动（在项目根目录执行）：
  .venv/Scripts/python -m uvicorn server.api:app --reload --port 8000

接口：
  GET  /                    页面（运行历史）
  GET  /api/health          库是否可用 + 数据量 + 当前执行态
  GET  /api/runs            运行历史（可按 game / suite / status 过滤）
  GET  /api/runs/{run_id}   单次运行明细（含用例列表与报告链接）
  POST /api/runs            起一次测试运行（排队执行）
  GET  /api/runs/{id}/events  SSE 实时日志（支持 from 序号回放）
  GET  /api/runs/{id}/log   轮询式取日志（SSE 的兜底）
  POST /api/runs/{id}/cancel 取消（排队中出队，运行中杀进程树）
  GET  /api/games           游戏清单 + 各自最近一次运行
  GET  /api/stats           按天统计的通过情况
  GET  /reports/...         Midscene 原生 HTML 报告（静态挂载）

读库部分只读；只有 POST /api/runs 会真的启动测试进程。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .case_index import (
    CaseError,
    ConflictError,
    create_case,
    delete_case,
    list_cases,
    read_case,
    refresh_index,
    save_case,
    validate_content,
)
from .game_index import create_game, list_channels, list_games as list_game_configs
from .runner import BusyError, runner

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "server" / "data" / "smoke.db"
REPORT_ROOT = (PROJECT_ROOT / "midscene_run" / "report").resolve()

app = FastAPI(title="game-smoke API", version="0.1.0")


@app.middleware("http")
async def no_store_for_assets(request, call_next):
    """页面与前端资源一律不缓存。

    开发期改完前端直接刷新就能看到，避免浏览器拿旧的 HTML/JS 导致"改了没生效"。
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith(("text/html", "application/javascript", "text/css")):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


def connect() -> sqlite3.Connection:
    """每次请求开一个只读连接，避免多线程共享连接的问题。"""
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"数据库不存在：{DB_PATH}；请先运行 python server/ingest.py",
        )
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def to_list(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def report_url(report_file: str | None) -> str | None:
    """把库里的绝对路径映射成 /reports/... 的 URL；不在报告目录下则返回 None。"""
    if not report_file:
        return None
    try:
        relative = Path(report_file).resolve().relative_to(REPORT_ROOT)
    except (ValueError, OSError):
        return None
    return "/reports/" + relative.as_posix()


WEB_ROOT = (Path(__file__).resolve().parent / "web").resolve()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/analytics", include_in_schema=False)
def analytics_page() -> FileResponse:
    return FileResponse(WEB_ROOT / "analytics.html")


@app.get("/api")
def api_index() -> dict[str, Any]:
    return {
        "name": "game-smoke API",
        "docs": "/docs",
        "endpoints": [
            "/api/health",
            "/api/runs?game=&suite=&status=&limit=50",
            "/api/runs/{run_id}",
            "POST /api/runs",
            "/api/runs/{run_id}/events?from=0",
            "/api/runs/{run_id}/log?from=0",
            "POST /api/runs/{run_id}/cancel",
            "/api/games",
            "/api/stats?game=&days=30",
            "/api/analytics/trend?days=30",
            "/api/analytics/failures?days=30&limit=10",
            "/api/analytics/matrix?limit=10",
            "/api/analytics/compare?a=&b=",
            "/api/analytics/distribution?days=30",
            "/reports/...",
        ],
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    conn = connect()
    try:
        runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        cases = conn.execute("SELECT count(*) FROM cases").fetchone()[0]
    finally:
        conn.close()
    return {
        "ok": True,
        "db": str(DB_PATH),
        "runs": runs,
        "cases": cases,
        "report_root": str(REPORT_ROOT),
        "report_root_exists": REPORT_ROOT.exists(),
        "runner": runner.snapshot(),
    }


@app.get("/api/runs")
def list_runs(
    game: str | None = Query(None, description="游戏 key，如 douyin/edge-space"),
    suite: str | None = Query(None, description="用例集：smoke / explore"),
    status: str | None = Query(None, description="success / failed"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where = ["1 = 1"]
    params: dict[str, Any] = {}

    if game:
        where.append(
            "EXISTS (SELECT 1 FROM cases c "
            "WHERE c.run_id = r.run_id AND c.game_key = :game)"
        )
        params["game"] = game
    if suite:
        where.append("',' || coalesce(r.suites, '') || ',' LIKE :suite")
        params["suite"] = f"%,{suite},%"
    if status:
        where.append("r.status = :status")
        params["status"] = status

    condition = ' AND '.join(where)
    sql = f"""
        SELECT * FROM runs r
        WHERE {condition}
        ORDER BY r.started_at DESC
        LIMIT :limit OFFSET :offset
    """
    count_sql = f"SELECT count(*) FROM runs r WHERE {condition}"
    page_params = {**params, 'limit': limit, 'offset': offset}

    conn = connect()
    try:
        items = to_list(conn.execute(sql, page_params))
        # total 与过滤条件保持同一口径，供前端分页使用
        total = conn.execute(count_sql, params).fetchone()[0]
    finally:
        conn.close()

    for item in items:
        item["report_url"] = report_url(item.get("report_file"))
    return {"total": total, "count": len(items), "items": items}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"run 不存在：{run_id}")
        run = dict(row)
        cases = to_list(
            conn.execute(
                "SELECT id, project, suite, game_key, source_path, case_index, name, status, attempts, "
                "duration_ms, failed_node, failed_phase, error_code, error_message "
                "FROM cases WHERE run_id = ? ORDER BY case_index, id",
                (run_id,),
            )
        )
    finally:
        conn.close()

    run["report_url"] = report_url(run.get("report_file"))
    run["cases"] = cases
    return run


@app.get("/api/games")
def list_games() -> dict[str, Any]:
    conn = connect()
    try:
        rows = to_list(
            conn.execute(
                """
                SELECT c.game_key,
                       g.display_name,
                       count(*) AS case_rows,
                       sum(CASE WHEN c.status = 'success' THEN 1 ELSE 0 END) AS passed,
                       (
                         SELECT r.run_id FROM runs r
                         JOIN cases c2 ON c2.run_id = r.run_id
                         WHERE c2.game_key = c.game_key
                         ORDER BY r.started_at DESC LIMIT 1
                       ) AS last_run_id
                FROM cases c
                LEFT JOIN games g ON g.game_key = c.game_key
                WHERE c.game_key IS NOT NULL
                GROUP BY c.game_key
                ORDER BY c.game_key
                """
            )
        )
        for game in rows:
            last = game.get("last_run_id")
            game["channel"], _, game_dir = game["game_key"].partition("/")
            game["game_dir"] = game_dir
            # 展示名来自 game.config.ts；读不到就退回目录名
            game["name"] = game.get("display_name") or game_dir
            game["last_run"] = None
            if last:
                run = conn.execute(
                    "SELECT run_id, started_at, status, total, passed, failed FROM runs WHERE run_id = ?",
                    (last,),
                ).fetchone()
                if run:
                    game["last_run"] = dict(run)
            # 展示名：配置里是中文，库里只有目录名，前端可自行映射
    finally:
        conn.close()
    return {"count": len(rows), "items": rows}


@app.get("/api/stats")
def stats(
    game: str | None = Query(None, description="按游戏 key 过滤"),
    days: int = Query(30, ge=1, le=365),
) -> dict[str, Any]:
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    where = ["r.started_at >= :since", "coalesce(r.status, '') <> 'cancelled'"]
    params: dict[str, Any] = {"since": since}
    if game:
        where.append(
            "EXISTS (SELECT 1 FROM cases c "
            "WHERE c.run_id = r.run_id AND c.game_key = :game)"
        )
        params["game"] = game

    sql = f"""
        SELECT substr(r.started_at, 1, 10) AS day,
               count(*) AS runs,
               sum(r.passed) AS passed,
               sum(r.failed) AS failed,
               sum(r.not_run) AS not_run,
               cast(avg(r.duration_ms) AS INTEGER) AS avg_duration_ms
        FROM runs r
        WHERE {' AND '.join(where)}
        GROUP BY day
        ORDER BY day
    """

    conn = connect()
    try:
        items = to_list(conn.execute(sql, params))
    finally:
        conn.close()
    return {"days": days, "since": since, "game": game, "items": items}


def _since(days: int) -> str:
    return (date.today() - timedelta(days=days - 1)).isoformat()


def _scope(tables: str, days: int, game: str | None, suite: str | None):
    """拼 days / game / suite 三种过滤条件，返回 (WHERE 片段, 参数)。

    取消的运行不算通过也不算失败（是人为中断），统一排除在统计口径之外。
    """
    where = ["r.started_at >= :since", "coalesce(r.status, '') <> 'cancelled'"]
    params: dict[str, Any] = {"since": _since(days)}
    if game:
        where.append(f"c.game_key = :game")
        params["game"] = game
    if suite:
        where.append(f"c.suite = :suite")
        params["suite"] = suite
    return " AND ".join(where), params


@app.get("/api/analytics/trend")
def analytics_trend(
    days: int = Query(30, ge=1, le=365),
    game: str | None = Query(None),
    suite: str | None = Query(None),
) -> dict[str, Any]:
    """按天统计用例级通过率与平均耗时。"""
    where, params = _scope("c JOIN runs r ON r.run_id = c.run_id", days, game, suite)
    sql = f"""
        SELECT substr(r.started_at, 1, 10) AS day,
               sum(CASE WHEN c.status = 'success' THEN 1 ELSE 0 END) AS passed,
               sum(CASE WHEN c.status = 'failed' THEN 1 ELSE 0 END) AS failed,
               cast(avg(c.duration_ms) AS INTEGER) AS avg_case_ms
        FROM cases c JOIN runs r ON r.run_id = c.run_id
        WHERE {where}
        GROUP BY day
        ORDER BY day
    """
    conn = connect()
    try:
        items = to_list(conn.execute(sql, params))
    finally:
        conn.close()
    for item in items:
        done = (item["passed"] or 0) + (item["failed"] or 0)
        item["pass_rate"] = round((item["passed"] or 0) / done, 3) if done else None
    return {"days": days, "items": items}


@app.get("/api/analytics/failures")
def analytics_failures(
    days: int = Query(30, ge=1, le=365),
    game: str | None = Query(None),
    suite: str | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    """失败用例排行，附带最常见的失败节点与错误码。"""
    where, params = _scope("c JOIN runs r ON r.run_id = c.run_id", days, game, suite)
    sql = f"""
        SELECT c.name,
               c.suite,
               c.game_key,
               count(*) AS failures,
               group_concat(DISTINCT c.error_code) AS error_codes,
               group_concat(DISTINCT c.failed_node) AS failed_nodes,
               max(r.started_at) AS last_failed_at
        FROM cases c JOIN runs r ON r.run_id = c.run_id
        WHERE {where} AND c.status = 'failed'
        GROUP BY c.name
        ORDER BY failures DESC
        LIMIT :limit
    """
    params["limit"] = limit
    conn = connect()
    try:
        items = to_list(conn.execute(sql, params))
        for item in items:
            row = conn.execute(
                """
                SELECT error_message FROM cases
                WHERE name = ? AND error_message IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """,
                (item["name"],),
            ).fetchone()
            item["last_error_message"] = row[0] if row else None
    finally:
        conn.close()
    return {"days": days, "count": len(items), "items": items}


@app.get("/api/analytics/matrix")
def analytics_matrix(
    limit: int = Query(10, ge=1, le=50),
    game: str | None = Query(None),
) -> dict[str, Any]:
    """用例稳定性矩阵：行=用例，列=最近 N 次运行。"""
    conn = connect()
    try:
        run_sql = "SELECT run_id, started_at, status FROM runs"
        params: dict[str, Any] = {"limit": limit}
        if game:
            run_sql += " WHERE EXISTS (SELECT 1 FROM cases c WHERE c.run_id = runs.run_id AND c.game_key = :game)"
            params["game"] = game
        run_sql += " ORDER BY started_at DESC LIMIT :limit"
        runs = to_list(conn.execute(run_sql, params))
        if not runs:
            return {"runs": [], "rows": []}

        placeholders = ",".join("?" for _ in runs)
        case_rows = to_list(
            conn.execute(
                f"""
                SELECT run_id, name, suite, game_key, status, error_code
                FROM cases WHERE run_id IN ({placeholders})
                """,
                tuple(run["run_id"] for run in runs),
            )
        )
    finally:
        conn.close()

    order = [run["run_id"] for run in runs]
    table: dict[str, dict[str, Any]] = {}
    for row in case_rows:
        entry = table.setdefault(
            row["name"],
            {
                "name": row["name"],
                "suite": row["suite"],
                "game_key": row["game_key"],
                "cells": {},
                "failures": 0,
            },
        )
        entry["cells"][row["run_id"]] = {"status": row["status"], "error_code": row["error_code"]}
        if row["status"] == "failed":
            entry["failures"] += 1

    rows = sorted(table.values(), key=lambda item: (-item["failures"], item["name"]))
    for row in rows:
        row["cells"] = [row["cells"].get(run_id) for run_id in order]

    return {"runs": runs, "rows": rows}


@app.get("/api/analytics/compare")
def analytics_compare(
    a: str = Query(..., description="基准运行（较早）"),
    b: str = Query(..., description="对比运行（较新）"),
) -> dict[str, Any]:
    """对比两次运行：新增失败 / 新增通过 / 仍然失败。"""
    conn = connect()
    try:
        def statuses(run_id: str) -> dict[str, dict[str, Any]]:
            rows = conn.execute(
                "SELECT source_path, case_index, name, status, error_code, duration_ms "
                "FROM cases WHERE run_id = ?",
                (run_id,),
            )
            return {
                f"{row[0]}#{row[1]}": {
                    "name": row[2],
                    "status": row[3],
                    "error_code": row[4],
                    "duration_ms": row[5],
                }
                for row in rows
            }

        before, after = statuses(a), statuses(b)
    finally:
        conn.close()

    newly_failed, newly_passed, still_failed, not_run = [], [], [], []
    for key, later in after.items():
        earlier = before.get(key)
        earlier_status = earlier["status"] if earlier else None
        if later["status"] == "failed":
            if earlier_status == "failed":
                still_failed.append({**later, "key": key, "before": earlier_status})
            else:
                newly_failed.append({**later, "key": key, "before": earlier_status})
        elif later["status"] == "success" and earlier_status == "failed":
            newly_passed.append({**later, "key": key, "before": earlier_status})
        elif later["status"] == "not-run":
            not_run.append({**later, "key": key, "before": earlier_status})

    return {
        "a": a,
        "b": b,
        "counts": {
            "newly_failed": len(newly_failed),
            "newly_passed": len(newly_passed),
            "still_failed": len(still_failed),
            "not_run": len(not_run),
        },
        "newly_failed": newly_failed,
        "newly_passed": newly_passed,
        "still_failed": still_failed,
        "not_run": not_run,
    }


@app.get("/api/analytics/distribution")
def analytics_distribution(
    days: int = Query(30, ge=1, le=365),
) -> dict[str, Any]:
    """按游戏 / 用例集统计通过失败分布。"""
    where, params = _scope("c JOIN runs r ON r.run_id = c.run_id", days, None, None)
    sql = f"""
        SELECT coalesce(c.game_key, '(未关联)') AS game_key,
               coalesce(c.suite, '(未知)') AS suite,
               sum(CASE WHEN c.status = 'success' THEN 1 ELSE 0 END) AS passed,
               sum(CASE WHEN c.status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM cases c JOIN runs r ON r.run_id = c.run_id
        WHERE {where} AND c.status IN ('success', 'failed')
        GROUP BY game_key, suite
        ORDER BY failed DESC
    """
    conn = connect()
    try:
        items = to_list(conn.execute(sql, params))
    finally:
        conn.close()
    return {"days": days, "items": items}


# ---------------------------------------------------------------- 用例管理
# 磁盘上的 YAML 是唯一真身；这里只做"索引 + 读写"，不把用例存进数据库


class SaveCaseRequest(BaseModel):
    path: str
    content: str
    expected_hash: str | None = None


class ValidateCaseRequest(BaseModel):
    content: str


class CreateCaseRequest(BaseModel):
    game_key: str
    suite: str = "smoke"
    name: str
    case_name: str


@app.get("/cases", include_in_schema=False)
def cases_page() -> FileResponse:
    return FileResponse(WEB_ROOT / "cases.html")


@app.get("/api/cases")
def cases_list(
    game: str | None = Query(None),
    suite: str | None = Query(None),
    refresh: bool = Query(True, description="是否先扫盘刷新索引（按 mtime 增量解析）"),
) -> dict[str, Any]:
    stats = refresh_index() if refresh else None
    items = list_cases(game, suite)
    return {
        "total": len(items),
        "items": items,
        "suites": {"smoke": sum(1 for i in items if i["suite"] == "smoke"),
                   "explore": sum(1 for i in items if i["suite"] == "explore")},
        "refresh": stats,
    }


@app.get("/api/cases/file")
def cases_read(path: str = Query(..., description="相对项目根的用例路径")) -> dict[str, Any]:
    try:
        return read_case(path)
    except CaseError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.put("/api/cases/file")
def cases_save(payload: SaveCaseRequest) -> dict[str, Any]:
    try:
        return save_case(payload.path, payload.content, payload.expected_hash)
    except ConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except CaseError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/cases/validate")
def cases_validate(payload: ValidateCaseRequest) -> dict[str, Any]:
    return validate_content(payload.content)


@app.post("/api/cases", status_code=201)
def cases_create(payload: CreateCaseRequest) -> dict[str, Any]:
    try:
        return create_case(payload.game_key, payload.suite, payload.name, payload.case_name)
    except CaseError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/cases/file")
def cases_delete(path: str = Query(...)) -> dict[str, Any]:
    try:
        return delete_case(path)
    except CaseError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


# ---------------------------------------------------------------- 游戏目录


class CreateGameRequest(BaseModel):
    channel: str
    dir: str
    name: str
    keyword: str | None = None
    menu_prompt: str


@app.get("/api/game-tree")
def game_tree() -> dict[str, Any]:
    """渠道 + 游戏（含还没有任何用例的空游戏）。"""
    games = list_game_configs()
    channels = []
    labels = {item["channel"]: item["label"] for item in list_channels()}
    for channel, label in labels.items():
        items = [game for game in games if game["channel"] == channel]
        channels.append(
            {
                "channel": channel,
                "label": label,
                "games": items,
                "case_count": sum(game["smoke"] + game["explore"] for game in items),
            }
        )
    return {"channels": channels, "total_games": len(games)}


@app.post("/api/games", status_code=201)
def create_game_endpoint(payload: CreateGameRequest) -> dict[str, Any]:
    """新建游戏：生成 game.config.ts + 空 smoke 目录 + explore/01-enter.yaml，并刷新注册表。"""
    try:
        return create_game(
            payload.channel, payload.dir, payload.name, payload.keyword, payload.menu_prompt
        )
    except CaseError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


# ---------------------------------------------------------------- 运行控制
# 单执行槽：一台真机同时只能跑一个任务，多出来的排队（上限见 runner.MAX_QUEUE）


class StartRunRequest(BaseModel):
    """起一次运行。files 为空表示跑整个 project；指定 game_key 时只跑该游戏的用例。"""

    project: str = "smoke"
    files: list[str] = []
    clean: bool = True
    game_key: str | None = None


def _resolve_case_files(files: list[str]) -> list[str]:
    """只接受项目内的 YAML，避免接口被用来读任意路径。"""
    resolved: list[str] = []
    for item in files:
        candidate = Path(item)
        path = (candidate if candidate.is_absolute() else PROJECT_ROOT / candidate).resolve()
        try:
            relative = path.relative_to(PROJECT_ROOT)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"路径不在项目内：{item}") from error
        if path.suffix.lower() not in (".yaml", ".yml"):
            raise HTTPException(status_code=400, detail=f"只支持 YAML 用例文件：{item}")
        if not path.is_file():
            raise HTTPException(status_code=400, detail=f"文件不存在：{item}")
        resolved.append(relative.as_posix())
    return resolved


@app.post("/api/runs", status_code=201)
def start_run(payload: StartRunRequest) -> dict[str, Any]:
    """起一次测试运行。立即返回 run_id，日志走 /events 或 /log 取。"""
    if payload.project not in ("smoke", "explore"):
        raise HTTPException(status_code=400, detail=f"未知套件：{payload.project}")
    files = _resolve_case_files(payload.files)
    game_key = payload.game_key
    if game_key:
        channel, _, game = game_key.partition("/")
        if not (PROJECT_ROOT / "games" / channel / game).is_dir():
            raise HTTPException(status_code=400, detail=f"游戏目录不存在：{game_key}")
        # 只跑该游戏的用例，杜绝跨游戏混跑导致的状态污染
        if files and any(not item.startswith(f"games/{game_key}/") for item in files):
            raise HTTPException(status_code=400, detail=f"文件不属于游戏 {game_key}")
    try:
        job = runner.start(payload.project, files, payload.clean, game_key)
    except BusyError as error:
        snapshot = runner.snapshot()
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(error),
                "running_run_id": (snapshot["running"] or {}).get("run_id"),
                "queue_depth": snapshot["queue_depth"],
            },
        ) from error
    return job.to_dict()


@app.get("/api/runs/{run_id}/events")
def stream_run_events(
    run_id: str,
    from_seq: int = Query(0, alias="from", ge=0, description="从哪个序号开始补历史"),
) -> StreamingResponse:
    """SSE 实时日志。断线重连带 from 就能补回历史，不会丢前面的行。"""
    if runner.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{run_id}")

    def generate():
        try:
            for seq, event in runner.iter_events(run_id, from_seq):
                if event.get("type") == "heartbeat":
                    # SSE 注释行，只为保活，客户端会忽略
                    yield ": ping\n\n"
                    continue
                payload = json.dumps(event, ensure_ascii=False)
                yield f"id: {seq}\nevent: {event['type']}\ndata: {payload}\n\n"
        except GeneratorExit:
            return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs/{run_id}/log")
def run_log(
    run_id: str,
    from_seq: int = Query(0, alias="from", ge=0),
) -> dict[str, Any]:
    """轮询式取日志：SSE 不可用或被中间层掐断时的兜底。"""
    try:
        items, last_seq, status = runner.log_slice(run_id, from_seq)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"任务不存在：{run_id}") from error
    return {"items": items, "next": last_seq, "status": status}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict[str, Any]:
    """取消：排队中直接出队；运行中杀进程树（含 codely 子进程）。"""
    try:
        was = runner.cancel(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"任务不存在：{run_id}") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    job = runner.get(run_id)
    return {"run_id": run_id, "was": was, "status": job.status if job else "unknown"}


if REPORT_ROOT.exists():
    app.mount("/reports", StaticFiles(directory=REPORT_ROOT), name="reports")

app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")
