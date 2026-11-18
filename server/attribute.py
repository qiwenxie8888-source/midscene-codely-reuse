"""失败归因的模型初判（A2，第一步只做离线回放）。

定位：**规则复核器 + 建议器**。每条失败都能拿到一个"可能是哪一类 + 依据"，
规则结论原样保留、两者并列；不写 cause_category、不改 status、不影响通过率；
bug 类由白名单硬拦，只能人标。

数据来源全是现成的：A1 已经入库的 error_detail / evidence_shot，加上用例 YAML 原文、
失败位置、最近几次运行状态。不接设备、不重跑用例。

用法：
  python server/attribute.py --replay                  # 所有失败都给建议（含生命周期失败）
  python server/attribute.py --replay --only-unknown   # 省钱档：只判规则判不出的
  python server/attribute.py --replay --dry-run        # 只看会调哪些、prompt 长什么样
  python server/attribute.py --replay --limit 3 --force # 忽略"已判过 + 规则高置信"的跳过
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "server" / "data" / "smoke.db"
REPORT_ROOT = PROJECT_ROOT / "midscene_run" / "report"
RAW_DIR = PROJECT_ROOT / "server" / "data" / "ai-attributions"
SCRIPT = PROJECT_ROOT / "scripts" / "acp-ask.mjs"

# 模型只能在这四类里选；bug 必须由人标，出现即整条丢弃
AI_CATEGORIES = ("env", "stale", "case_flaw", "unknown")
MIN_CONFIDENCE = 0.4

SCHEMA = """
CREATE TABLE IF NOT EXISTS case_ai_causes (
  run_id          TEXT NOT NULL,
  source_path     TEXT NOT NULL,
  case_index      INTEGER NOT NULL,
  category        TEXT,
  reason          TEXT,
  evidence_quote  TEXT,
  confidence      REAL,
  model           TEXT,
  used_image      INTEGER,
  case_hash       TEXT,
  ai_error        TEXT,
  checked_at      TEXT,
  rule_category   TEXT,
  agreement       TEXT,
  PRIMARY KEY (run_id, source_path, case_index)
)
"""

# 老表升级：一致性统计需要"判这条时规则给的是什么"，否则规则后来改了就对不上
AI_COLUMNS = [("rule_category", "TEXT"), ("agreement", "TEXT")]

SYSTEM_RULES = """你是小游戏自动化测试的失败归因助手。只输出一个 JSON 对象：不要解释、不要 Markdown、不要使用任何工具。

判定口径（四选一，**禁止输出 bug**）：
- env 环境问题：设备离线、网络错误/超时、登录失败、依赖下载失败；重跑通常能过。
- stale 用例过期：界面改版或被弹窗遮挡，导致用例的期望（提示词、断言文字）不再成立；产品本身没坏。
- case_flaw 用例缺陷：用例写法本身有问题，例如一条指令塞多个目标、动作链过长、盲等时间不足、断言依赖不稳定元素。
- unknown 证据不足，无法判断。

产品缺陷（bug）只能由人工标注。你不得输出 bug；如果怀疑是产品问题，输出 unknown，
并在 reason 里写明"疑似产品问题，需人工确认"。

输出格式（严格一行 JSON）：
{"category":"env|stale|case_flaw|unknown","reason":"一句话依据","evidence_quote":"从观察原文里逐字复制的一句话","confidence":0.0-1.0}

硬性要求，违反会导致整条作废：
1. evidence_quote 必须是上面【模型观察原文】里**逐字复制**的一段连续文字；不许改写、不许概括、
   不许描述截图内容。原文里找不到合适句子时，category 输出 unknown。
2. 所有字符串值内**不要出现英文双引号**（会破坏 JSON）；需要引用时用「」代替。
3. 只输出这个 JSON 对象本身，前后不要有任何说明文字。
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(case_ai_causes)")}
    for name, ddl in AI_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE case_ai_causes ADD COLUMN {name} {ddl}")


def load_rows(conn: sqlite3.Connection, only_unknown: bool) -> list[dict]:
    """挑出候选失败行。

    默认口径＝**每条失败都给建议**：status=failed，或生命周期失败（用例没跑，
    status=not-run 但有失败阶段与错误码）。规则已判的行也包含在内——它的价值是
    交叉校验规则，而不是兜底。
    """
    sql = """
        SELECT c.run_id, c.source_path, c.case_index, c.name, c.status, c.failed_phase,
               c.failed_node, c.error_code, c.error_message, c.error_detail,
               c.evidence_shot, c.cause_category, c.cause_confidence,
               a.case_hash AS ai_case_hash, a.checked_at AS ai_checked_at
        FROM cases c
        LEFT JOIN case_ai_causes a
          ON a.run_id = c.run_id AND a.source_path = c.source_path
         AND a.case_index = c.case_index
        WHERE c.error_detail IS NOT NULL
          AND (c.status = 'failed' OR (c.error_code IS NOT NULL AND c.failed_phase IS NOT NULL))
    """
    if only_unknown:
        sql += " AND (c.cause_category IS NULL OR c.cause_category = 'unknown')"
    sql += " ORDER BY c.run_id DESC, c.source_path"
    return [dict(row) for row in conn.execute(sql).fetchall()]


def resolve_case_file(source_path: str, case_name: str | None = None) -> Path | None:
    """把 source_path 还原成磁盘上的用例文件。

    单文件模式下 summary 里的 sourcePath 只剩文件名（如 01-enter-game.yaml），
    而多个游戏会有同名用例，这时用"用例名字出现在文件里"来消歧。
    """
    direct = PROJECT_ROOT / source_path
    if direct.exists():
        return direct
    matches = list((PROJECT_ROOT / "games").glob(f"*/*/*/{source_path}"))
    if len(matches) == 1:
        return matches[0]
    if case_name:
        narrowed = []
        for candidate in matches:
            try:
                if case_name in candidate.read_text(encoding="utf-8"):
                    narrowed.append(candidate)
            except OSError:
                continue
        if len(narrowed) == 1:
            return narrowed[0]
    return None


def case_hash_of(source_path: str, case_index: int, case_name: str | None = None) -> str:
    """用例文件内容 hash：改了用例就该重判，没改就不该重复烧模型。"""
    path = resolve_case_file(source_path, case_name)
    if path is None:
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def history_of(conn: sqlite3.Connection, source_path: str, case_index: int) -> str:
    rows = conn.execute(
        """
        SELECT run_id, status FROM cases
        WHERE source_path = ? AND case_index = ?
        ORDER BY run_id DESC LIMIT 5
        """,
        (source_path, case_index),
    ).fetchall()
    if not rows:
        return "无历史"
    return "、".join(f"{row[0][:8]}={row[1]}" for row in rows)


def read_case_yaml(source_path: str, case_name: str | None = None) -> str:
    path = resolve_case_file(source_path, case_name)
    if path is None:
        return "（读不到用例原文）"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "（读不到用例原文）"


def build_prompt(row: dict, history: str, yaml_text: str, has_image: bool) -> str:
    image_note = (
        "附带一张失败瞬间的截图，请结合图片判断界面是否被遮挡或改版。"
        if has_image
        else "没有附带截图，只能依据文字证据判断；证据不足时请给 unknown。"
    )
    return f"""{SYSTEM_RULES}
{image_note}

【用例原文】
{yaml_text.strip()[:3000]}

【失败位置】
阶段 {row.get('failed_phase') or '-'} / 节点 {row.get('failed_node') or '-'} / 错误码 {row.get('error_code') or '-'}

【模型观察原文】
{(row.get('error_detail') or '')[:3000]}

【这条用例最近几次运行】
{history}
"""


def resolve_shot(row: dict) -> Path | None:
    shot = row.get("evidence_shot")
    if not shot:
        return None
    path = REPORT_ROOT / shot
    return path if path.exists() else None


def validate(text: str, detail: str) -> tuple[dict | None, str | None]:
    """返回 (结论, 错误原因)。任何一项不合格都整条丢弃——不猜、不修正。"""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None, "no_json"
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None, "bad_json"
    if not isinstance(payload, dict):
        return None, "not_object"
    category = payload.get("category")
    if category not in AI_CATEGORIES:
        return None, f"forbidden_category:{category}"
    quote = (payload.get("evidence_quote") or "").strip()
    if not quote or quote not in (detail or ""):
        return None, "ungrounded"
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        return None, "bad_confidence"
    confidence = max(0.0, min(1.0, confidence))
    if confidence < MIN_CONFIDENCE:
        category = "unknown"
    return {
        "category": category,
        "reason": str(payload.get("reason") or "")[:500],
        "evidence_quote": quote[:500],
        "confidence": confidence,
    }, None


def save_raw(run_id: str, source_path: str, case_index: int, data: dict) -> None:
    try:
        target = RAW_DIR / run_id
        target.mkdir(parents=True, exist_ok=True)
        name = f"{Path(source_path).stem}_{case_index}.json"
        (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="失败归因的模型初判（离线回放）")
    parser.add_argument("--replay", action="store_true", help="只读历史数据，不接设备（当前唯一模式）")
    parser.add_argument("--no-image", action="store_true", help="不带截图（图会让单条耗时涨到 80–300s 且偶发超时）")
    parser.add_argument("--only-unknown", action="store_true", help="省钱档：只判规则判不出的行")
    parser.add_argument("--limit", type=int, default=5, help="本批最多判几条，默认 5")
    parser.add_argument("--force", action="store_true", help="已判过的也重判")
    parser.add_argument("--dry-run", action="store_true", help="只打印选中的行与 prompt，不调模型")
    parser.add_argument("--model", default="codely-vl")
    # 带截图的那条明显更慢（实测 141.8s / 86.4s / 超时 180s），所以默认放宽到 300s
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    if not args.replay:
        print("当前只实现了 --replay（离线回放）；接设备/接看板的模式等评估完再加。")
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)
    conn.commit()

    rows = load_rows(conn, args.only_unknown)
    if not rows:
        print("没有可判的行（生命周期失败也算，需要 error_detail 已入库）")
        return 0

    tasks, prepared, skipped = [], [], 0
    for row in rows:
        case_hash = case_hash_of(row["source_path"], row["case_index"], row.get("name"))
        # 省钱档：上次判过、用例没改、规则又很确定 → 跳过。想让 AI 复核规则就加 --force
        if (
            not args.force
            and row.get("ai_checked_at")
            and (row.get("cause_confidence") or 0) >= 0.9
            and row.get("ai_case_hash") == case_hash
        ):
            skipped += 1
            continue
        history = history_of(conn, row["source_path"], row["case_index"])
        yaml_text = read_case_yaml(row["source_path"], row.get("name"))
        shot = None if args.no_image else resolve_shot(row)
        images = [str(shot)] if shot else []
        prompt = build_prompt(row, history, yaml_text, bool(shot))
        task_id = f"{row['run_id']}|{row['source_path']}|{row['case_index']}"
        tasks.append({"id": task_id, "text": prompt, "images": images})
        prepared.append(
            {
                **row,
                "task_id": task_id,
                "history": history,
                "case_hash": case_hash,
                "image": str(shot) if shot else None,
                "prompt": prompt,
            }
        )
        if len(prepared) >= args.limit:
            break

    print(
        f"选中 {len(prepared)} 条"
        f"（{'仅 unknown' if args.only_unknown else '全部失败'}，跳过已判高置信 {skipped} 条，"
        f"模型 {args.model}）"
    )
    if args.dry_run:
        for item in prepared:
            print(f"\n--- {item['task_id']}")
            print(f"  规则结论: {item['cause_category']} | 图: {item['image'] or '无'}")
            print(item["prompt"][:700])
        return 0

    def run_batch(task_list: list[dict]) -> dict:
        payload = json.dumps(
            {
                "model": args.model,
                "cwd": str(PROJECT_ROOT),
                "timeoutMs": args.timeout_ms,
                # 停滞检测：想得久不算失败，只有开始输出后长时间无新内容才判死
                "idleMs": 150_000,
                "maxMs": max(args.timeout_ms, 600_000),
                "tasks": task_list,
            },
            ensure_ascii=False,
        )
        proc = subprocess.run(
            ["node", str(SCRIPT)],
            input=payload.encode("utf-8"),
            capture_output=True,
            cwd=str(PROJECT_ROOT),
        )
        if proc.returncode != 0:
            print("ACP 进程失败：" + proc.stderr.decode("utf-8", "replace")[-600:])
            return {}
        out: dict = {}
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[parsed.get("id")] = parsed
        return out

    results = run_batch(tasks)

    # 带图那条实测成功率约一半：超时/停滞时去掉截图再判一次，纯文字稳定得多
    degraded: set[str] = set()
    retry_tasks = []
    for item in prepared:
        got = results.get(item["task_id"]) or {}
        err = str(got.get("error") or "")
        if item["image"] and err and ("timed out" in err or "stalled" in err or "exceeded max" in err):
            retry_tasks.append({"id": item["task_id"], "text": item["prompt"], "images": []})
    if retry_tasks:
        print(f"带图失败 {len(retry_tasks)} 条，去掉截图重判一次…")
        for task in retry_tasks:
            got = run_batch([task]).get(task["id"])
            if got and not got.get("error"):
                results[task["id"]] = got
                degraded.add(task["id"])

    now = datetime.now(timezone.utc).isoformat()
    ok = failed = 0
    agree_count: dict[str, int] = {}
    for item in prepared:
        result = results.get(item["task_id"]) or {"error": "no_response"}
        if result.get("error"):
            verdict, error = None, str(result["error"])[:300]
            ms = result.get("ms")
        else:
            verdict, error = validate(result.get("text"), item["error_detail"] or "")
            ms = result.get("ms")
        save_raw(item["run_id"], item["source_path"], item["case_index"], {**item, "raw": result})

        # 一致性：判这条时的规则结论要快照下来，规则词表以后改了也不会污染历史统计
        rule_category = item.get("cause_category")
        if not verdict:
            agreement = None
        elif not rule_category:
            agreement = "rule_empty"
        elif verdict["category"] == rule_category:
            agreement = "same"
        else:
            agreement = "different"

        conn.execute(
            """
            INSERT INTO case_ai_causes (
              run_id, source_path, case_index, category, reason, evidence_quote,
              confidence, model, used_image, case_hash, ai_error, checked_at,
              rule_category, agreement
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id, source_path, case_index) DO UPDATE SET
              -- 失败重判不能抹掉以前的有效结论：只有这次真的判出来才覆盖
              category=COALESCE(excluded.category, case_ai_causes.category),
              reason=COALESCE(excluded.reason, case_ai_causes.reason),
              evidence_quote=COALESCE(excluded.evidence_quote, case_ai_causes.evidence_quote),
              confidence=COALESCE(excluded.confidence, case_ai_causes.confidence),
              agreement=COALESCE(excluded.agreement, case_ai_causes.agreement),
              rule_category=COALESCE(excluded.rule_category, case_ai_causes.rule_category),
              model=excluded.model, used_image=excluded.used_image,
              case_hash=excluded.case_hash, ai_error=excluded.ai_error,
              checked_at=excluded.checked_at
            """,
            (
                item["run_id"],
                item["source_path"],
                item["case_index"],
                verdict["category"] if verdict else None,
                verdict["reason"] if verdict else None,
                verdict["evidence_quote"] if verdict else None,
                verdict["confidence"] if verdict else None,
                args.model,
                1 if item["image"] and item["task_id"] not in degraded else 0,
                item["case_hash"],
                error,
                now,
                rule_category,
                agreement,
            ),
        )
        conn.commit()
        if agreement:
            agree_count[agreement] = agree_count.get(agreement, 0) + 1

        took = f"{round((ms or 0) / 1000, 1)}s" if ms else "-"
        if verdict:
            ok += 1
            mark = {"same": "一致", "different": "分歧", "rule_empty": "规则为空"}[agreement]
            print(f"\n[OK] {item['task_id']}  ACP {took}")
            print(f"  AI: {verdict['category']} {verdict['confidence']:.2f} | 规则: {rule_category or '-'} → {mark}")
            print(f"  依据: {verdict['reason']}")
            print(f"  引用: {verdict['evidence_quote'][:80]}")
        else:
            failed += 1
            print(f"\n[DROP] {item['task_id']}  ACP {took}")
            print(f"  原因: {error}")

    print(
        f"\n完成：有效 {ok} 条，丢弃 {failed} 条；"
        f"规则一致性 一致={agree_count.get('same', 0)} 分歧={agree_count.get('different', 0)} "
        f"规则为空={agree_count.get('rule_empty', 0)} -> {args.db}"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
