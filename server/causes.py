r"""失败定性（cause）词表与规则归因。

定性只是**标注**：不重跑用例、不改运行状态、不影响任何通过率口径。
规则只判得出 env / case_flaw / stale 三类，"bug 产品缺陷"只能由人标——
规则只会看错误码和文案，无法证明产品真的坏了，乱标 bug 会把要提单的问题淹掉。

原生依赖为零，ingest.py（当脚本跑）和 api.py（当包跑）都能导入。
"""
from __future__ import annotations

# 分类词表：key -> 展示名与配色。前端通过 /api/causes 取，保证只有一份真身
# 配色约定：过期=黄、缺陷=橙、bug=实心红（要和状态徽章的浅红区分开）、环境=蓝灰、待定=浅灰
CATEGORIES: dict[str, dict[str, str]] = {
    "env": {"label": "环境问题", "color": "#e2e8f0"},
    "stale": {"label": "用例过期", "color": "#fef9c3"},
    "case_flaw": {"label": "用例缺陷", "color": "#ffedd5"},
    "bug": {"label": "产品缺陷", "color": "#dc2626"},
    "unknown": {"label": "待定", "color": "#e4e4e7"},
}

DEFAULT_CATEGORY = "unknown"

# 环境问题：设备/网络/登录态坏了，用例和产品都没毛病，重跑通常就过
ENV_CODES = {"PROJECT_SETUP_ERROR"}
ENV_MARKERS = (
    "No Android devices",
    "device offline",
    "设备离线",
    "网络错误",
    "网络不稳定",
    "网络连接",
    "登录失败",
    "无法连接",
    "连接超时",
    "下载失败",
)

# 用例缺陷：一条指令塞了多个目标/动作链太长，撞满单步超时
TIMEOUT_CODES = {"STEP_TIMEOUT"}
LONG_CHAIN_NODES = {"aiAct", "aiTap", "game.enter", "game.ensure"}

# 用例过期：产品界面或流程变了，用例的期望（提示词/断言）不再成立
LOCATE_MARKERS = ("failed to locate element", "failed to locate")
WAIT_MARKERS = ("waitFor timeout",)
MISSING_MARKERS = ("未找到", "没有出现", "未出现", "找不到", "没有显示")


def is_valid_category(value: str) -> bool:
    return value in CATEGORIES


def label_of(category: str | None) -> str:
    return CATEGORIES.get(category or DEFAULT_CATEGORY, CATEGORIES[DEFAULT_CATEGORY])["label"]


def classify_cause(
    error_code: str | None = None,
    error_message: str | None = None,
    failed_node: str | None = None,
) -> tuple[str, str, float]:
    """按优先级给出 (分类, 理由, 置信度)。

    顺序不能换：网络失败经常同时带 failed to locate，env 必须先判。
    """
    code = error_code or ""
    message = error_message or ""
    node = failed_node or ""

    if code in ENV_CODES:
        return "env", f"环境异常：{code}", 0.9
    for marker in ENV_MARKERS:
        if marker in message:
            return "env", f"环境/网络异常：{marker}", 0.9

    if code in TIMEOUT_CODES and node in LONG_CHAIN_NODES:
        return "case_flaw", f"{node} 单步动作链撞满超时", 0.7

    # waitFor timeout 本身就是"等不到目标界面"，属于期望与现实不符，不需要再靠缺失词佐证
    if any(marker in message for marker in WAIT_MARKERS):
        return "stale", "等不到目标界面，疑似界面改版或提示词过期", 0.55

    located = any(marker in message for marker in LOCATE_MARKERS)
    missing = any(marker in message for marker in MISSING_MARKERS)
    if located and missing:
        return "stale", "界面上找不到预期元素，疑似界面或提示词过期", 0.6

    return DEFAULT_CATEGORY, "规则无法判定，需人工确认", 0.2
