# -*- coding: utf-8 -*-
"""design_system：品牌中立的企业桌面设计令牌与响应式布局数据（纯函数，无 GUI 副作用）。

来源：Trae（平级成员），2026-10-08；任务 m-1791457584495-f44d5b（TR-DESKTOP，作业 j-b7ed91331128d934d22268834e306bd4）。
依据：TASKBOOK.md §2/§3、TASK_ASSIGNMENTS.md 公共执行约束、AGENTS.md 安全边界；
设计令牌沿用上一批已交付的 workbench_theme 取值（PALETTE/TYPE_SCALE/SPACING），本模块是其品牌中立超集。

边界（写死）：
- 纯函数、零 I/O：不 import Tk、不读文件、不起进程/模型、不访问网络。
- 品牌中立：不出现任何厂商品牌名；令牌与布局只描述视觉与几何，不描述能力或身份。
- 字号使用 Tk point（正数），Tk 自行按 scaling 换算像素；调用方不得重复缩放字体。
- 布局像素按 logical·scaling/(96/72) 有界换算，与 workbench_theme.scaled_px 同式。
"""
from __future__ import annotations

__all__ = [
    "TOKENS",
    "TYPE_SCALE",
    "SPACING",
    "LAYOUT_PROFILES",
    "LAYOUT_ORDER",
    "LAYOUT_THRESHOLDS",
    "KEYBOARD_SHORTCUTS",
    "BASELINE_SCALING",
    "MIN_SCALING",
    "MAX_SCALING",
    "DARK_TOKENS",
    "ELEVATIONS",
    "DARK_ELEVATIONS",
    "get_tokens",
    "layout_profile",
    "choose_layout",
    "scaled",
    "clamp_scaling",
    "contrast_ratio",
    "contrast_ok",
    "focus_order",
    "keyboard_shortcuts",
]

# --- 颜色令牌（品牌中立，沿用 workbench_theme.PALETTE 前 7 键并补齐状态/禁用色） ---
TOKENS: dict[str, str] = {
    "background": "#f3f5f8",
    "surface": "#ffffff",
    "text": "#1f2937",
    "secondary": "#5b6575",
    "border": "#d8dee8",
    "accent": "#2459b8",
    "accent_hover": "#1e4c9d",
    "ok": "#23764a",
    "warn": "#986a15",
    "err": "#b42318",
    "muted": "#6b7280",
    "input_bg": "#fbfbfb",
    "surface_warn": "#fffdf5",
    "disabled_bg": "#d9e1ee",
    "disabled_text": "#6b7280",
    "on_accent": "#ffffff",
}

# --- 深色模式令牌（低饱和优雅暗黑，满足 WCAG 4.5:1 对比度要求） ---
DARK_TOKENS: dict[str, str] = {
    "background": "#171a21",
    "surface": "#20242c",
    "text": "#e6e7eb",
    "secondary": "#9da3ae",
    "border": "#363b46",
    "accent": "#8c9cf0",
    "accent_hover": "#a5b3f8",
    "ok": "#4ade80",
    "warn": "#facc15",
    "err": "#f87171",
    "muted": "#9ca3af",
    "input_bg": "#1c2027",
    "surface_warn": "#312a1c",
    "disabled_bg": "#2b313d",
    "disabled_text": "#64748b",
    "on_accent": "#0f1218",
}

# --- 景深阴影阶梯系统 (Elevation Tokens) ---
ELEVATIONS: dict[str, str] = {
    "e1": "0 1px 3px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 0, 0, 0.04)",
    "e2": "0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)",
    "e3": "0 10px 15px -3px rgba(0, 0, 0, 0.12), 0 4px 6px -2px rgba(0, 0, 0, 0.05)",
    "e4": "0 20px 25px -5px rgba(0, 0, 0, 0.15), 0 10px 10px -5px rgba(0, 0, 0, 0.04)",
}

DARK_ELEVATIONS: dict[str, str] = {
    "e1": "0 1px 3px rgba(0, 0, 0, 0.35), 0 1px 2px rgba(0, 0, 0, 0.25)",
    "e2": "0 4px 10px rgba(0, 0, 0, 0.45), 0 2px 4px rgba(0, 0, 0, 0.30)",
    "e3": "0 10px 24px rgba(0, 0, 0, 0.55), 0 4px 8px rgba(0, 0, 0, 0.35)",
    "e4": "0 20px 35px rgba(0, 0, 0, 0.65), 0 8px 12px rgba(0, 0, 0, 0.45)",
}

# --- 字体刻度（Tk point，正数即 point；Tk 按 scaling 换算，调用方不得再缩放） ---
TYPE_SCALE: dict[str, int] = {
    "small": 9,
    "body": 10,
    "subtitle": 11,
    "title": 18,
    "display": 22,
    "mono": 10,
}

# --- 间距（8px 基准，xs 为例外 4） ---
SPACING: dict[str, int] = {
    "xs": 4,
    "sm": 8,
    "md": 16,
    "lg": 24,
    "xl": 32,
}

# --- 响应式布局档位（逻辑像素，96dpi 基准） ---
LAYOUT_ORDER: tuple[str, ...] = ("compact", "normal", "wide")

LAYOUT_PROFILES: dict[str, dict] = {
    "compact": {
        "name": "compact",
        "min_width_px": 0,
        "card_columns": 1,
        "member_columns": 1,
        "max_member_rows": 4,
        "show_member_evidence": False,
        "wrap_title_chars": 24,
        "wrap_goal_chars": 40,
        "padding": "sm",
        "min_target_px": 32,
    },
    "normal": {
        "name": "normal",
        "min_width_px": 900,
        "card_columns": 2,
        "member_columns": 2,
        "max_member_rows": 6,
        "show_member_evidence": False,
        "wrap_title_chars": 40,
        "wrap_goal_chars": 64,
        "padding": "md",
        "min_target_px": 32,
    },
    "wide": {
        "name": "wide",
        "min_width_px": 1400,
        "card_columns": 4,
        "member_columns": 3,
        "max_member_rows": 8,
        "show_member_evidence": True,
        "wrap_title_chars": 60,
        "wrap_goal_chars": 96,
        "padding": "lg",
        "min_target_px": 32,
    },
}

LAYOUT_THRESHOLDS: tuple[tuple[str, int], ...] = (
    ("wide", 1400),
    ("normal", 900),
    ("compact", 0),
)

# --- 键盘可达规则（稳定动作 id，供外壳绑定快捷键；不依赖具体控件实现） ---
KEYBOARD_SHORTCUTS: dict[str, str] = {
    "next_action": "<Alt-n>",
    "nav_home": "<Alt-1>",
    "nav_team": "<Alt-2>",
    "nav_tasks": "<Alt-3>",
    "nav_access": "<Alt-4>",
    "nav_settings": "<Alt-5>",
    "nav_logs": "<Alt-6>",
    "focus_next": "<Tab>",
    "focus_prev": "<Shift-Tab>",
    "activate": "<Return>",
}

LAYOUT_BASE_DPI = 96
POINTS_PER_INCH = 72
BASELINE_SCALING = LAYOUT_BASE_DPI / POINTS_PER_INCH  # 96/72 ≈ 1.3333
MIN_SCALING = BASELINE_SCALING
MAX_SCALING = 4.0


def get_tokens(theme: str = "light") -> dict:
    """返回颜色令牌副本（支持 light / dark 双模态；调用方不得原地改共享令牌）。"""
    if theme == "dark":
        return dict(DARK_TOKENS)
    return dict(TOKENS)


def layout_profile(name: str) -> dict:
    """返回命名布局档位副本；未知或非字符串名称回落到 normal，不抛异常。"""
    key = name if isinstance(name, str) and name in LAYOUT_PROFILES else "normal"
    return dict(LAYOUT_PROFILES[key])


def clamp_scaling(scaling) -> float:
    """把 Tk scaling（像素/点）限制在有界区间；异常输入回落到基准。"""
    try:
        value = float(scaling)
    except (TypeError, ValueError):
        return BASELINE_SCALING
    if value != value or value <= 0:  # NaN 或非正
        return BASELINE_SCALING
    return min(max(value, MIN_SCALING), MAX_SCALING)


def choose_layout(width_px, scaling: float = 1.0) -> str:
    """按可用宽度（逻辑像素，已折算 DPI）选择档位；坏输入回落到 compact 之外的安全档。"""
    try:
        logical_width = float(width_px) / (clamp_scaling(scaling) / BASELINE_SCALING)
    except (TypeError, ValueError):
        return "normal"
    if logical_width != logical_width or logical_width < 0:
        return "normal"
    for name, threshold in LAYOUT_THRESHOLDS:
        if logical_width >= threshold:
            return name
    return "compact"


def scaled(logical_px: int, scaling: float, *, min_px: int = 1, max_px: int = 4096) -> int:
    """把 96dpi 基准逻辑像素换算成当前 scaling 下的实际像素，并做有界钳制。

    actual = logical * scaling / (96/72)。仅用于布局尺寸；字体用 point，不经本函数。
    """
    if not isinstance(logical_px, int) or isinstance(logical_px, bool):
        raise TypeError("logical_px 必须是整数")
    if logical_px < 0:
        raise ValueError("logical_px 不能为负")
    if logical_px == 0:
        return 0
    ratio = clamp_scaling(scaling) / BASELINE_SCALING
    return min(max(int(round(logical_px * ratio)), min_px), max_px)


def _parse_hex(hex_color: str) -> tuple[int, int, int]:
    if not isinstance(hex_color, str) or len(hex_color) != 7 or not hex_color.startswith("#"):
        raise ValueError("非法颜色：%r" % (hex_color,))
    try:
        return (int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16))
    except ValueError:
        raise ValueError("非法颜色：%r" % (hex_color,)) from None


def _channel_luminance(component: int) -> float:
    c = component / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 对比度，返回 [1, 21]；与前后景顺序无关。"""
    lum = lambda hex_color: (0.2126 * _channel_luminance(_parse_hex(hex_color)[0])
                             + 0.7152 * _channel_luminance(_parse_hex(hex_color)[1])
                             + 0.0722 * _channel_luminance(_parse_hex(hex_color)[2]))
    lighter, darker = sorted((lum(foreground), lum(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_ok(foreground: str, background: str, minimum: float = 4.5) -> bool:
    """前后景对比度是否达标；非法输入返回 False（不抛异常）。"""
    try:
        return contrast_ratio(foreground, background) >= float(minimum)
    except (TypeError, ValueError):
        return False


def focus_order(shell: dict) -> list:
    """由外壳数据推导键盘焦点顺序（稳定 id 列表）；坏输入返回空列表。

    顺序：主操作 → 四卡 → 责任行 → 导航；保证“唯一下一步”永远第一个可达。
    """
    if not isinstance(shell, dict):
        return []
    order = []
    primary = shell.get("primary_action")
    if isinstance(primary, dict) and isinstance(primary.get("id"), str):
        order.append("action:%s" % primary["id"])
    cards = shell.get("cards")
    if isinstance(cards, list):
        for card in cards:
            if isinstance(card, dict) and isinstance(card.get("id"), str):
                order.append("card:%s" % card["id"])
    # 成员焦点项：优先可见成员（键盘只可达可见行），再回落到全量成员/责任表。
    rows = shell.get("visible_members")
    if not isinstance(rows, list):
        rows = shell.get("members")
    if not isinstance(rows, list):
        rows = shell.get("responsibilities")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("member_id"), str):
                order.append("member:%s" % row["member_id"])
    nav = shell.get("nav_ids")
    if isinstance(nav, list):
        order.extend("nav:%s" % item for item in nav if isinstance(item, str))
    return order


def keyboard_shortcuts() -> dict:
    """返回快捷键映射副本。"""
    return dict(KEYBOARD_SHORTCUTS)
