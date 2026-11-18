#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包入口：哨兵值守台（AgentWatchtower.exe）。

和 entry.py 一样，这是个只用绝对导入的薄壳——PyInstaller 不能直接拿包内的
相对导入文件当入口。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentbus.watchtower_gui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
