"""用例索引：磁盘上的 YAML 是唯一真身，这里只维护一份可随时重建的缓存。

为什么不让数据库存用例正文：
  1. midscene-test 只认磁盘文件，跑之前一定要落盘，"数据库为准"等于每次多一层搬运；
  2. 用例要进 Git，靠 diff 追改动；内容进库后 Git 就看不见了；
  3. pnpm validate / CI / run-full 全都按文件路径工作。

所以这里只做三件事：
  - 扫描 games/<渠道>/<游戏>/<套件>/*.yaml，把"文件名→用例名/指纹"记进 case_files 表；
  - 提供读写用例文件的接口（保存前校验、比对指纹、自动备份）；
  - 提供新增（空白骨架）与删除（先备份再删）。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GAMES_ROOT = PROJECT_ROOT / "games"
DB_PATH = PROJECT_ROOT / "server" / "data" / "smoke.db"
BACKUP_ROOT = PROJECT_ROOT / ".backup" / "cases"
NODE_SPEC_CANDIDATES = ("midscene-node-spec.smoke.md", "midscene-node-spec.explore.md")

SUITES = ("smoke", "explore")

# 文件名规则：小写字母/数字/连字符，必须以字母或数字开头
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SEQ_RE = re.compile(r"^(\d+)-")
NODE_ROW_RE = re.compile(r"^\|\s*([A-Za-z][\w.]*)\s*\|")
# 渠道中文名来自 platforms/<渠道>.ts 的 label；游戏中文名来自 game.config.ts 的 name
PLATFORM_LABEL_RE = re.compile(r"""^\s*label:\s*['"]([^'"]+)['"]""", re.MULTILINE)
GAME_NAME_RE = re.compile(r"""^\s*name:\s*['"]([^'"]+)['"]""", re.MULTILINE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS case_files (
  path         TEXT PRIMARY KEY,
  game_key     TEXT,
  suite        TEXT,
  seq          INTEGER,
  case_name    TEXT,
  case_count   INTEGER,
  mtime        REAL,
  content_hash TEXT,
  parse_error  TEXT,
  parsed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_case_files_suite ON case_files(suite, game_key);
"""


class CaseError(ValueError):
    """用例文件操作失败（内容非法、路径越界等），由 API 转成 400。"""


class ConflictError(CaseError):
    """文件在编辑期间被外部改过，由 API 转成 409。"""


# ---------------------------------------------------------------- 基础设施


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def resolve_case_path(rel: str) -> Path:
    """把接口传来的路径限制在 games/ 下，杜绝路径穿越。"""
    candidate = Path(rel)
    if candidate.is_absolute():
        path = candidate.resolve()
    else:
        path = (PROJECT_ROOT / candidate).resolve()
    try:
        path.relative_to(GAMES_ROOT)
    except ValueError as error:
        raise CaseError(f"路径不在 games/ 下：{rel}") from error
    if path.suffix.lower() not in (".yaml", ".yml"):
        raise CaseError(f"只支持 YAML 用例文件：{rel}")
    return path


def load_node_names() -> set[str]:
    """从 `pnpm nodes` 生成的 spec 里读节点名，读不到就返回空集合（跳过节点校验）。"""
    names: set[str] = set()
    for name in NODE_SPEC_CANDIDATES:
        path = PROJECT_ROOT / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = NODE_ROW_RE.match(line)
            if match:
                names.add(match.group(1))
    return names


def parse_game_key(rel_path: str) -> tuple[str | None, str | None, int | None]:
    """games/<渠道>/<游戏>/<套件>/<文件> → (game_key, suite, seq)。"""
    parts = Path(rel_path).parts
    if len(parts) < 5 or parts[0] != "games":
        return None, None, None
    _, channel, game, suite, name = parts[0], parts[1], parts[2], parts[3], parts[4]
    seq_match = SEQ_RE.match(name)
    seq = int(seq_match.group(1)) if seq_match else None
    return f"{channel}/{game}", suite, seq


# ---------------------------------------------------------------- 校验


def validate_content(content: str, node_names: set[str] | None = None) -> dict[str, Any]:
    """校验 YAML 文本。返回 {ok, message, case_names, nodes}，不写任何文件。"""
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f"第 {mark.line + 1} 行" if mark else "未知位置"
        problem = getattr(error, "problem", None) or str(error)
        return {"ok": False, "message": f"YAML 语法错误（{where}）：{problem}", "case_names": []}

    if not isinstance(document, dict):
        return {"ok": False, "message": "顶层必须是映射（至少要有 cases:）", "case_names": []}

    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        return {"ok": False, "message": "缺少 cases 列表，或列表为空", "case_names": []}

    known = load_node_names() if node_names is None else node_names
    names: list[str] = []
    unknown: list[str] = []

    def check_steps(steps: Any, where: str) -> str | None:
        if not isinstance(steps, list):
            return f"{where}: steps 必须是列表"
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict) or len(step) != 1:
                return f"{where} 第 {index} 步：必须形如 `- 节点名: 输入`（只有一个键）"
            node = next(iter(step))
            if known and node not in known:
                unknown.append(f"{where} 第 {index} 步：未知节点 `{node}`")
        return None

    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            return {"ok": False, "message": f"cases 第 {index} 项必须是映射", "case_names": []}
        name = case.get("name")
        if not name:
            return {"ok": False, "message": f"cases 第 {index} 项缺少 name", "case_names": []}
        names.append(str(name))
        error = check_steps(case.get("steps"), f"cases[{index}]")
        if error:
            return {"ok": False, "message": error, "case_names": []}

    # beforeAll / afterAll 用同一套节点，一起校验
    for hook in ("beforeAll", "afterAll"):
        if hook in document:
            error = check_steps(document[hook], hook)
            if error:
                return {"ok": False, "message": error, "case_names": []}

    if unknown:
        shown = "；".join(unknown[:3])
        more = f"（共 {len(unknown)} 处）" if len(unknown) > 3 else ""
        return {"ok": False, "message": f"存在未知节点：{shown}{more}", "case_names": names}

    return {"ok": True, "message": f"语法正常，{len(names)} 条用例", "case_names": names}


# ---------------------------------------------------------------- 索引


def scan_disk() -> list[dict[str, Any]]:
    """把磁盘上的用例文件列出来（不读库、不解析）。"""
    found: list[dict[str, Any]] = []
    if not GAMES_ROOT.exists():
        return found
    for suite in SUITES:
        for path in sorted(GAMES_ROOT.glob(f"*/*/{suite}/*.yaml")) + sorted(
            GAMES_ROOT.glob(f"*/*/{suite}/*.yml")
        ):
            rel = relative(path)
            stat = path.stat()
            game_key, suite_name, seq = parse_game_key(rel)
            found.append(
                {
                    "path": rel,
                    "abs": path,
                    "game_key": game_key,
                    "suite": suite_name,
                    "seq": seq,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }
            )
    return found


def refresh_index(force: bool = False) -> dict[str, int]:
    """按 mtime 判断哪些文件需要重新解析，写进 case_files；磁盘上已不存在的行删掉。"""
    disk = scan_disk()
    node_names = load_node_names()
    stats = {"scanned": len(disk), "parsed": 0, "cached": 0, "removed": 0, "errors": 0}

    with connect() as conn:
        existing = {
            row["path"]: row for row in conn.execute("SELECT * FROM case_files")
        }
        seen: set[str] = set()

        for item in disk:
            rel = item["path"]
            seen.add(rel)
            cached = existing.get(rel)
            if (
                not force
                and cached
                and cached["mtime"] is not None
                and abs(float(cached["mtime"]) - item["mtime"]) < 1e-6
            ):
                stats["cached"] += 1
                continue

            raw = item["abs"].read_bytes()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw.decode("utf-8", errors="replace")

            result = validate_content(content, node_names)
            case_names = result.get("case_names") or []
            parse_error = None
            if not result["ok"]:
                parse_error = result["message"]
                stats["errors"] += 1

            conn.execute(
                """
                INSERT INTO case_files
                  (path, game_key, suite, seq, case_name, case_count, mtime, content_hash, parse_error, parsed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                  game_key=excluded.game_key, suite=excluded.suite, seq=excluded.seq,
                  case_name=excluded.case_name, case_count=excluded.case_count,
                  mtime=excluded.mtime, content_hash=excluded.content_hash,
                  parse_error=excluded.parse_error, parsed_at=excluded.parsed_at
                """,
                (
                    rel,
                    item["game_key"],
                    item["suite"],
                    item["seq"],
                    case_names[0] if case_names else None,
                    len(case_names),
                    item["mtime"],
                    hash_bytes(raw),
                    parse_error,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            stats["parsed"] += 1

        for rel in existing:
            if rel not in seen:
                conn.execute("DELETE FROM case_files WHERE path = ?", (rel,))
                stats["removed"] += 1

    return stats


def list_cases(game: str | None = None, suite: str | None = None) -> list[dict[str, Any]]:
    """列出用例，附带渠道/游戏的中文名（从 TS 配置里抓，抓不到退回 key）。"""
    labels = channel_labels()
    names = game_names()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if game:
        clauses.append("game_key = ?")
        params.append(game)
    if suite:
        clauses.append("suite = ?")
        params.append(suite)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT path, game_key, suite, seq, case_name, case_count, mtime, parse_error
            FROM case_files WHERE {' AND '.join(clauses)}
            ORDER BY game_key, suite, coalesce(seq, 9999), path
            """,
            params,
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        channel, _, game_dir = (item["game_key"] or "").partition("/")
        item["channel"] = channel or None
        item["channel_label"] = labels.get(channel, channel or None)
        item["game_dir"] = game_dir or None
        item["game_name"] = names.get(item["game_key"], game_dir or None)
        items.append(item)
    return items


def channel_labels() -> dict[str, str]:
    """platforms/<渠道>.ts 里的 label → {渠道key: 中文名}。读不到就空，前端退回 key。"""
    labels: dict[str, str] = {}
    platforms = PROJECT_ROOT / "platforms"
    if not platforms.is_dir():
        return labels
    for path in platforms.glob("*.ts"):
        if path.stem in ("types", "index"):
            continue
        match = PLATFORM_LABEL_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            labels[path.stem] = match.group(1)
    return labels


def game_names() -> dict[str, str]:
    """games/<渠道>/<游戏>/game.config.ts 里的 name → {game_key: 中文名}。"""
    names: dict[str, str] = {}
    if not GAMES_ROOT.is_dir():
        return names
    for config in GAMES_ROOT.glob("*/*/game.config.ts"):
        parts = config.relative_to(PROJECT_ROOT).parts
        if len(parts) < 4:
            continue
        _, channel, game_dir = parts[0], parts[1], parts[2]
        match = GAME_NAME_RE.search(config.read_text(encoding="utf-8", errors="replace"))
        if match:
            names[f"{channel}/{game_dir}"] = match.group(1)
    return names


# ---------------------------------------------------------------- 读写


def read_case(rel: str) -> dict[str, Any]:
    path = resolve_case_path(rel)
    if not path.is_file():
        raise CaseError(f"文件不存在：{rel}")
    raw = path.read_bytes()
    return {
        "path": relative(path),
        "content": raw.decode("utf-8"),
        "hash": hash_bytes(raw),
        "mtime": path.stat().st_mtime,
    }


def backup(path: Path, bucket: str) -> str:
    target_dir = BACKUP_ROOT / bucket / time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / path.resolve().relative_to(PROJECT_ROOT)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(path.read_bytes())
    return relative(target)


def save_case(rel: str, content: str, expected_hash: str | None = None) -> dict[str, Any]:
    path = resolve_case_path(rel)
    if not path.is_file():
        raise CaseError(f"文件不存在：{rel}")

    current = hash_bytes(path.read_bytes())
    if expected_hash and expected_hash != current:
        raise ConflictError("文件已被外部修改，请刷新后再保存")

    result = validate_content(content)
    if not result["ok"]:
        raise CaseError(result["message"])

    backup_path = backup(path, "edits")
    # 显式用 newline="\n"：Windows 上默认会把 \n 写成 \r\n，导致 git diff 满屏变化
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content if content.endswith("\n") else content + "\n")

    refresh_index()
    return {
        "path": relative(path),
        "hash": hash_bytes(path.read_bytes()),
        "backup": backup_path,
        "message": result["message"],
    }


SKELETON = """# {title}
# 一条用例一个文件：beforeAll 用 game.ensure（已在主菜单则跳过，省掉重复进游戏）
beforeAll:
  - game.ensure:
      gameKey: {game_key}

cases:
  - name: {case_name}
    steps:
      # 一次只给一个目标，并描述位置与颜色：canvas 界面上相似图标很多，
      # 放多个目标会让模型反复试探（实测会撞满步骤超时）
      - aiTap:
          prompt: <待填写：点什么>
      - aiWaitFor:
          prompt: <待填写：等到什么界面出现>
          options:
            timeoutMs: 20000
      - aiAssert: <待填写：断言什么>
"""


def check_new_name(game_key: str, suite: str, name: str) -> list[str]:
    """返回警告列表；不合法直接抛 CaseError。"""
    if not name:
        raise CaseError("文件名不能为空")
    if not NAME_RE.match(name):
        raise CaseError("文件名只能用小写字母、数字和连字符，且必须以字母或数字开头")
    if name.endswith("-") or "--" in name:
        raise CaseError("文件名不能以连字符结尾，也不能出现连续连字符")
    if len(name) > 60:
        raise CaseError("文件名不能超过 60 个字符")

    channel, _, game = game_key.partition("/")
    if not channel or not game:
        raise CaseError(f"游戏标识不合法：{game_key}")
    if suite not in SUITES:
        raise CaseError(f"套件只能是 {'/'.join(SUITES)}")

    folder = GAMES_ROOT / channel / game / suite
    if not folder.is_dir():
        raise CaseError(f"目录不存在：{relative(folder)}")
    for existing in folder.iterdir():
        if existing.stem.lower() == name.lower():
            raise CaseError(f"同目录下已存在同名用例：{existing.name}")

    warnings: list[str] = []
    seq_match = SEQ_RE.match(name)
    if seq_match and seq_match.group(1):
        seq = int(seq_match.group(1))
        for existing in folder.iterdir():
            other = SEQ_RE.match(existing.stem)
            if other and int(other.group(1)) == seq:
                warnings.append(f"序号 {seq:02d} 已被 {existing.name} 使用")
                break
    else:
        warnings.append("建议命名为「序号-功能」形式（如 05-newbie-guide），便于排序")
    return warnings


def create_case(
    game_key: str,
    suite: str,
    name: str,
    case_name: str,
    content: str | None = None,
) -> dict[str, Any]:
    warnings = check_new_name(game_key, suite, name)
    if content is None:
        seq_title = SEQ_RE.match(name)
        title = f"冒烟 {name}" if suite == "smoke" else f"探索 {name}"
        if seq_title:
            title = f"冒烟 {seq_title.group(1)}：<占位，请改成一句话说明这条用例在验什么>" if suite == "smoke" else f"探索 {seq_title.group(1)}：<占位>"
        content = SKELETON.format(title=title, game_key=game_key, case_name=case_name)

    result = validate_content(content)
    if not result["ok"]:
        raise CaseError(result["message"])

    channel, _, game = game_key.partition("/")
    path = (GAMES_ROOT / channel / game / suite / f"{name}.yaml").resolve()
    if path.exists():
        raise CaseError(f"文件已存在：{relative(path)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content if content.endswith("\n") else content + "\n")

    refresh_index()
    return {"path": relative(path), "warnings": warnings, "message": result["message"]}


def delete_case(rel: str) -> dict[str, Any]:
    path = resolve_case_path(rel)
    if not path.is_file():
        raise CaseError(f"文件不存在：{rel}")
    backup_path = backup(path, "deleted")
    path.unlink()
    refresh_index()
    return {"path": rel, "backup": backup_path}