# -*- coding: utf-8 -*-
"""工作台视觉 tokens 与页面导航配置（纯 Python，无 GUI 副作用）。

来源：Trae（平级成员），2026-10-05，任务 m-1791187019713-373b6e
（job j-534407960b61037245f56fe225e91459）。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。
依据：成果/agentbus/pilots/desktop-workbench-20261005/DESIGN.md，
以及对本项目现有 gui.py 的只读核对（未改动该文件）。

本模块只导出数据与纯函数：不引入任何 GUI 工具包、不创建窗口对象、导入无副作用。
PALETTE 是现有 gui.py COLORS 的超集映射源；实际套用与集成由 Codex 完成。
字号使用 Tk point（正数），由 Tk 自行按 scaling 换算像素；调用方不得重复缩放字体。
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

__all__ = [
    "PALETTE",
    "TYPE_SCALE",
    "SPACING",
    "NAV_ITEMS",
    "NAV_BY_ID",
    "NAV_ORDER",
    "CONTRAST_REQUIREMENTS",
    "BASELINE_SCALING",
    "MIN_SCALING",
    "MAX_SCALING",
    "space",
    "pad",
    "font_points",
    "clamp_scaling",
    "scaled_px",
    "relative_luminance",
    "contrast_ratio",
]

# --- 调色板 ---------------------------------------------------------------
# 前 7 个键与现有 gui.py COLORS 完全一致，供 Codex 无损映射；
# 其余键统一原先散落在 gui.py 中的状态色、禁用色与输入/警示底色。
PALETTE: dict[str, str] = {
    "background": "#f3f5f8",
    "surface": "#ffffff",
    "text": "#1f2937",
    "secondary": "#5b6575",
    "border": "#d8dee8",
    "accent": "#2459b8",
    "accent_hover": "#1e4c9d",
    # 状态色（日志 ok/warn/err 与“待接入”灰）。
    "ok": "#23764a",
    "warn": "#986a15",
    "err": "#b42318",
    "muted": "#6b7280",
    # 输入/字段底色与警示底色。
    "input_bg": "#fbfbfb",
    "surface_warn": "#fffdf5",
    # 禁用态。
    "disabled_bg": "#d9e1ee",
    "disabled_text": "#6b7280",
    # 主按钮上的文字。
    "on_accent": "#ffffff",
}

# --- 字体刻度（Tk point，正数即 point） -----------------------------------
# Tk 会用当前 scaling 把 point 换算成像素；调用方不得再自行缩放这些值。
TYPE_SCALE: dict[str, int] = {
    "small": 9,
    "body": 10,
    "subtitle": 11,
    "title": 18,
    "display": 22,
    "mono": 10,
}

# --- 间距（8px 基准，xs 为例外 4） -----------------------------------------
SPACING: dict[str, int] = {
    "xs": 4,
    "sm": 8,
    "md": 16,
    "lg": 24,
    "xl": 32,
}

# --- 页面导航（稳定 id 供外壳深链，不靠索引/字符串） -----------------------
NAV_ITEMS: tuple[dict[str, str], ...] = (
    {"id": "home", "title": "工作台", "subtitle": "项目 · 目标 · 成员 · 下一步"},
    {"id": "team", "title": "团队", "subtitle": "成员勾选 · 组长"},
    {"id": "tasks", "title": "任务", "subtitle": "作业 · 计划 · 待接手"},
    {"id": "access", "title": "接入与技能", "subtitle": "接入说明 · 续接 · 技能检查"},
    {"id": "settings", "title": "设置", "subtitle": "工作流 · 分发 · 诊断"},
    {"id": "logs", "title": "运行记录", "subtitle": "完整日志 · 技术细节"},
)

NAV_BY_ID: Mapping[str, dict[str, str]] = MappingProxyType(
    {item["id"]: item for item in NAV_ITEMS})

NAV_ORDER: tuple[str, ...] = tuple(item["id"] for item in NAV_ITEMS)

# --- 对比度要求（实际用到的前/背景对） -------------------------------------
# 正文与状态文字 ≥ 4.5:1；大字/UI 元素 ≥ 3:1（DESIGN 第 30、48 行）。
CONTRAST_REQUIREMENTS: tuple[tuple[str, str, float], ...] = (
    ("text", "background", 4.5),
    ("text", "surface", 4.5),
    ("text", "input_bg", 4.5),
    ("secondary", "surface", 4.5),
    ("secondary", "background", 4.5),
    ("on_accent", "accent", 4.5),
    ("ok", "surface", 4.5),
    ("warn", "surface", 4.5),
    ("warn", "surface_warn", 4.5),
    ("err", "surface", 4.5),
    ("muted", "surface", 4.5),
    ("muted", "background", 3.0),
    ("disabled_text", "disabled_bg", 3.0),
)

# --- 布局像素换算（有界） --------------------------------------------------
LAYOUT_BASE_DPI = 96
POINTS_PER_INCH = 72
BASELINE_SCALING = LAYOUT_BASE_DPI / POINTS_PER_INCH  # 96/72 ≈ 1.3333
MIN_SCALING = BASELINE_SCALING
MAX_SCALING = 4.0


def space(name: str) -> int:
    """返回命名间距（逻辑像素）。未知名称抛 KeyError。"""
    try:
        return SPACING[name]
    except KeyError:
        raise KeyError(f"未知间距刻度：{name}") from None


def pad(name: str) -> tuple[int, int]:
    """返回命名间距的 (x, y) 内边距。"""
    value = space(name)
    return (value, value)


def font_points(name: str) -> int:
    """返回 Tk point 字号（原样，不缩放）；Tk 自行按 scaling 换算像素。"""
    try:
        return TYPE_SCALE[name]
    except KeyError:
        raise KeyError(f"未知字体刻度：{name}") from None


def clamp_scaling(scaling: float) -> float:
    """把 Tk scaling（像素/点）限制在有界区间；异常输入回落到基准。"""
    try:
        value = float(scaling)
    except (TypeError, ValueError):
        return BASELINE_SCALING
    if value != value or value <= 0:  # NaN 或非正
        return BASELINE_SCALING
    return min(max(value, MIN_SCALING), MAX_SCALING)


def scaled_px(logical_px: int, scaling: float, *, min_px: int = 1,
              max_px: int = 4096) -> int:
    """把 96dpi 基准的逻辑像素换算成当前 Tk scaling 下的实际像素。

    actual = logical * scaling / (96/72)，并做有界钳制。仅用于布局尺寸；
    字体使用 point，不经过本函数，避免重复缩放。
    """
    if not isinstance(logical_px, int) or isinstance(logical_px, bool):
        raise TypeError("logical_px 必须是整数")
    if logical_px < 0:
        raise ValueError("logical_px 不能为负")
    ratio = clamp_scaling(scaling) / BASELINE_SCALING
    value = int(round(logical_px * ratio))
    return min(max(value, min_px), max_px)


def _parse_hex(hex_color: str) -> tuple[int, int, int]:
    if not isinstance(hex_color, str) or len(hex_color) != 7 or not hex_color.startswith("#"):
        raise ValueError(f"非法颜色：{hex_color!r}")
    try:
        return (int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16))
    except ValueError:
        raise ValueError(f"非法颜色：{hex_color!r}") from None


def _channel_luminance(component: int) -> float:
    c = component / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """WCAG 相对亮度，返回 [0, 1]。"""
    r, g, b = _parse_hex(hex_color)
    return (0.2126 * _channel_luminance(r)
            + 0.7152 * _channel_luminance(g)
            + 0.0722 * _channel_luminance(b))


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 对比度，返回 [1, 21]。与前后景顺序无关。"""
    lighter, darker = sorted(
        (relative_luminance(foreground), relative_luminance(background)),
        reverse=True)
    return (lighter + 0.05) / (darker + 0.05)