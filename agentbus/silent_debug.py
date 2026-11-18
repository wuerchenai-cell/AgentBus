# -*- coding: utf-8 -*-
"""静默异常的可选调试出口（§8.7）。

背景：包内 59 处 `except …: pass` 需要分类处置——资源清理类保留并注明理由，
功能类至少落一条调试记录。但本包**不引入 logging 依赖**（保持离线、无副作用、
确定性输出），因此这里提供一个**默认完全静默**的最小出口：

- 默认（未设置 `AGENTBUS_DEBUG_SILENT=1`）：`note_silent` 是廉价 no-op，
  除一次 os.environ 查询外不做任何事，不写文件、不打印、不入环。
- 开启后：把「文件 / 行号 / 分类 / 原因」追加到**有界内存环**，供测试与
  现场诊断读取；仍**不写磁盘**，不改变任何控制流。

定位：**只观测，不决策**。它绝不抛出、绝不改变被包裹代码的返回值或异常传播。
调用方约定：仅在「已确知该异常可安全忽略」的功能类 except 中调用，
并在同一 `pass` 处保留一句中文理由注释。
"""
from __future__ import annotations

import os
from collections import deque
from typing import Any

_ENV_FLAG = "AGENTBUS_DEBUG_SILENT"
_RING_LIMIT = 256

# 有界环：仅在显式开启时写入；默认无人读、无人写。
_RING: deque[dict[str, Any]] = deque(maxlen=_RING_LIMIT)


def debug_enabled() -> bool:
    """是否显式开启了静默点调试出口（默认 False）。"""
    return os.environ.get(_ENV_FLAG, "").strip() == "1"


def note_silent(location: str, category: str, reason: str = "") -> None:
    """记录一次「有意静默」的异常处置；默认 no-op。

    location  形如 "acp.py:349"；category ∈ {cleanup, functional}。
    reason    中文一句话，说明为何可安全忽略（功能类必须给）。
    本函数永不抛出。
    """
    try:
        if not debug_enabled():
            return
        _RING.append({"location": location, "category": category, "reason": reason})
    except Exception:  # pragma: no cover - 观测器自身绝不干扰主流程
        pass  # 资源清理类：观测器自身兜底，必须永不抛出、永不改变主流程。


def drain() -> list[dict[str, Any]]:
    """取出并清空当前环内容（供测试/诊断）。默认返回空列表。"""
    items = list(_RING)
    _RING.clear()
    return items


def pending() -> int:
    """环内条数（供测试断言）。"""
    return len(_RING)
