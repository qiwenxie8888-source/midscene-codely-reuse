"""游戏目录管理：列出渠道/游戏、新建游戏。

和 case_index 一样：磁盘上的 game.config.ts 是唯一真身，这里只读它、按模板生成新文件。
新建游戏必须连带更新 scripts/games.ts（注册表），否则跑用例会报"未注册的游戏"。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .case_index import CaseError, PROJECT_ROOT, channel_labels

GAMES_ROOT = PROJECT_ROOT / "games"
PLATFORMS_ROOT = PROJECT_ROOT / "platforms"
SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync-games.mjs"
PLATFORM_INFO = PROJECT_ROOT / "scripts" / "platform-info.mjs"

DIR_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
KEY_RE = re.compile(r"""key:\s*['"]([^'"]+)['"]""")
NAME_RE = re.compile(r"""name:\s*['"]([^'"]+)['"]""")

SKIPPED_PLATFORM_FILES = {"types", "index"}


def _platform_file(channel: str) -> Path:
    return PLATFORMS_ROOT / f"{channel}.ts"


def list_channels() -> list[dict[str, Any]]:
    """磁盘上已实现的渠道（每个 platforms/<渠道>.ts 就是一个渠道）。"""
    labels = channel_labels()
    channels = []
    if PLATFORMS_ROOT.is_dir():
        for path in sorted(PLATFORMS_ROOT.glob("*.ts")):
            if path.stem in SKIPPED_PLATFORM_FILES:
                continue
            channels.append({"channel": path.stem, "label": labels.get(path.stem, path.stem)})
    return channels


def _count_cases(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return len(list(folder.glob("*.yaml"))) + len(list(folder.glob("*.yml")))


def list_games() -> list[dict[str, Any]]:
    """扫描 games/<渠道>/<游戏>/game.config.ts。

    必须按目录扫，不能按用例文件反推：刚建的游戏还没有任何用例，按用例推会把它漏掉。
    """
    labels = channel_labels()
    games: list[dict[str, Any]] = []
    if not GAMES_ROOT.is_dir():
        return games
    for config in sorted(GAMES_ROOT.glob("*/*/game.config.ts")):
        parts = config.relative_to(PROJECT_ROOT).parts
        if len(parts) < 4:
            continue
        _, channel, game_dir = parts[0], parts[1], parts[2]
        text = config.read_text(encoding="utf-8", errors="replace")
        key_match = KEY_RE.search(text)
        name_match = NAME_RE.search(text)
        base = config.parent
        games.append(
            {
                "game_key": key_match.group(1) if key_match else f"{channel}/{game_dir}",
                "channel": channel,
                "channel_label": labels.get(channel, channel),
                "game_dir": game_dir,
                "game_name": name_match.group(1) if name_match else game_dir,
                "smoke": _count_cases(base / "smoke"),
                "explore": _count_cases(base / "explore"),
            }
        )
    return games


def _platform_card_prompt(channel: str, game_name: str) -> dict[str, Any]:
    """调用 Node 执行渠道适配器拿 cardPrompt（模板只有一份真身，在 platforms/*.ts 里）。"""
    try:
        result = subprocess.run(
            ["node", str(PLATFORM_INFO), channel, game_name],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        payload = json.loads(result.stdout or "{}")
        if payload.get("ok"):
            return payload
        raise CaseError(payload.get("message") or "读取渠道适配器失败")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise CaseError(f"读取渠道 {channel} 的适配器失败：{error}") from error


def _escape_template(text: str) -> str:
    """把用户输入放进 TS 模板字符串（反引号）里，需要转义反引号和 ${。"""
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def _config_text(game_key: str, game_dir: str, name: str, channel: str, keyword: str, card_prompt: str, menu_prompt: str, label: str) -> str:
    keyword_line = "" if keyword.strip() == name.strip() else f"    keyword: {json.dumps(keyword, ensure_ascii=False)},\n"
    return (
        "import { defineGame } from '../../types';\n"
        "\n"
        "export default defineGame({\n"
        f"  key: {json.dumps(game_key, ensure_ascii=False)},\n"
        f"  name: {json.dumps(name, ensure_ascii=False)},\n"
        f"  channel: {json.dumps(channel, ensure_ascii=False)},\n"
        "  enter: {\n"
        f"{keyword_line}"
        f"    cardPrompt:\n      {json.dumps(card_prompt, ensure_ascii=False)},\n"
        f"    menuPrompt: {json.dumps(menu_prompt, ensure_ascii=False)},\n"
        "  },\n"
        "  aiContext: `当前在测试" + label + "《" + _escape_template(name) + "》，游戏界面可能由 canvas 渲染。\n"
        "遇到用户协议、隐私政策、版本公告、登录提示、更新弹窗时，一律点\"同意\"\"允许\"或\"关闭\"继续，不要中断流程。`,\n"
        "});\n"
    )


def _explore_text(game_key: str, name: str) -> str:
    return (
        f"# 探索 01：验证进入链——能不能搜到、点进、停在《{name}》主菜单\n"
        "# 新建游戏时自动生成。第一次跑大概率失败：多半是 game.config.ts 的 menuPrompt 与实际界面不符，\n"
        "# 按报错里提示的界面情况改那一行，再跑一次。\n"
        "beforeAll:\n"
        "  - game.enter:\n"
        f"      gameKey: {game_key}\n"
        "\n"
        "cases:\n"
        f"  - name: 进入与加载-能从渠道进入《{name}》并停在主界面\n"
        "    steps:\n"
        "      - aiAssert: 当前停在游戏的主界面（不是渠道的搜索结果页，也不是加载中/黑屏）\n"
    )


def create_game(
    channel: str,
    game_dir: str,
    name: str,
    keyword: str | None,
    menu_prompt: str,
) -> dict[str, Any]:
    channel = (channel or "").strip()
    game_dir = (game_dir or "").strip()
    name = (name or "").strip()
    menu_prompt = (menu_prompt or "").strip()

    if not _platform_file(channel).is_file():
        raise CaseError(f"渠道不存在：{channel}（需先在 platforms/{channel}.ts 实现适配器）")
    if not DIR_RE.match(game_dir):
        raise CaseError("目录名只能用小写字母、数字和连字符，且必须以字母或数字开头")
    if game_dir.endswith("-") or "--" in game_dir:
        raise CaseError("目录名不能以连字符结尾，也不能出现连续连字符")
    if not name:
        raise CaseError("游戏中文名不能为空")
    if not menu_prompt:
        raise CaseError("主菜单判断（menuPrompt）不能为空：它是判断“是否进入游戏”的依据")

    target = GAMES_ROOT / channel / game_dir
    if target.exists():
        raise CaseError(f"目录已存在：games/{channel}/{game_dir}")

    info = _platform_card_prompt(channel, name)
    label = info.get("label", channel)
    card_prompt = info.get("cardPrompt", "")

    game_key = f"{channel}/{game_dir}"
    config_text = _config_text(
        game_key,
        game_dir,
        name,
        channel,
        keyword or name,
        card_prompt,
        menu_prompt,
        label,
    )

    created = False
    try:
        (target / "smoke").mkdir(parents=True)
        (target / "explore").mkdir(parents=True)
        (target / "game.config.ts").write_text(config_text, encoding="utf-8", newline="\n")
        (target / "explore" / "01-enter.yaml").write_text(
            _explore_text(game_key, name), encoding="utf-8", newline="\n"
        )
        created = True

        result = subprocess.run(
            ["node", str(SYNC_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            raise CaseError(f"注册表生成失败：{(result.stderr or result.stdout).strip()[:300]}")
    except Exception:
        if created:
            shutil.rmtree(target, ignore_errors=True)
            # 回滚注册表：目录没了，重算一次即可恢复原样
            subprocess.run(
                ["node", str(SYNC_SCRIPT)], cwd=str(PROJECT_ROOT), capture_output=True, timeout=120
            )
        raise

    return {
        "game_key": game_key,
        "path": f"games/{game_key}/game.config.ts",
        "channel": channel,
        "channel_label": label,
        "game_name": name,
        "card_prompt": card_prompt,
        "explore_case": f"games/{channel}/{game_dir}/explore/01-enter.yaml",
        "sync_output": result.stdout.strip(),
        "warnings": [
            "已生成 explore/01-enter.yaml。第一次运行大概率会失败，",
            "这是正常的：请根据报错调整 game.config.ts 里的 menuPrompt。",
        ],
    }