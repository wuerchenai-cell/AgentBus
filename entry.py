#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包入口。

PyInstaller 不能直接拿 agentbus/__main__.py 当入口 —— 那个文件里是相对导入
（from .cli import ...），被当成顶层脚本执行时会报 "attempted relative import
with no known parent package"。所以这里放一个只用绝对导入的薄壳。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentbus.__main__ import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
