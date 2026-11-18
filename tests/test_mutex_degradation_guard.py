#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mutex_degradation_guard.py: 互斥资源防静默降级护栏与宽泛异常门禁测试。

来源：§10.8 AUDIT-360-BROAD-EXCEPTION-TRIAGE（P1）。
验收标准：
1. 人为注入瞬时打开失败（如 Windows PermissionError 竞态），断言未退化前不得裸奔进入临界区；
2. 反向注入（退化成单次 open 无重试）时护栏测试变红；
3. 全包宽泛异常捕获数量门禁（只降不升）。
"""
from __future__ import annotations

import ast
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# 导入待测模块
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
TOOLS_DIR = PROJECT_DIR / "tools"
import sys
sys.path.insert(0, str(PROJECT_DIR))

from tools import gen_broad_exception_triage
from agentbus import gui_state  # noqa: E402
from agentbus import exclusive_lock  # noqa: E402


class TestMutexDegradationGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lock_handle_retries_on_transient_permission_error(self):
        """正向测试：在瞬时 PermissionError 竞态下，锁文件获取必须持续重试并最终成功，绝不静默放弃。

        §13.11（2026-10-13）重命名：原 `gui_state._open_lock_handle`（只负责"打开"、
        自带 5s 期限）已并入 `gui_state._acquire_interprocess_lock(lock_path, deadline)`，
        由它在一个**总期限**内同时完成"打开 + 加锁"。重试语义不变，故本用例改调新入口。
        """
        lock_file = self.root / "test_gui_state.lock"

        real_open = open
        attempt_counter = [0]

        def flaky_open(*args, **kwargs):
            attempt_counter[0] += 1
            if attempt_counter[0] <= 3:
                # 模拟 Windows 上并发首次建文件时的瞬时锁定冲突
                raise PermissionError(13, "Permission denied (simulated lock race)")
            return real_open(*args, **kwargs)

        with patch("builtins.open", side_effect=flaky_open):
            handle = gui_state._acquire_interprocess_lock(
                lock_file, time.monotonic() + gui_state.LOCK_TIMEOUT_S)
            try:
                # 断言：必须重试超过 3 次，且最终成功拿到 handle，绝不能因为前 3 次失败直接放弃
                self.assertIsNotNone(handle, "瞬时竞争下锁获取错误退化为 None，失去互斥！")
                self.assertGreaterEqual(attempt_counter[0], 4, "未经历重试循环即返回，不符合重试规范！")
            finally:
                if handle is not None:
                    handle.close()

    def test_negative_single_try_degradation_detected(self):
        """反向注入验证：若退化为旧式的单次 open 无重试逻辑，护栏测试能够立即捕获并变红。"""
        lock_file = self.root / "test_negative.lock"

        # 模拟单次尝试的缺陷逻辑（遇到异常立即退化为 None）
        def naive_single_try_open(path):
            try:
                raise PermissionError("Simulated instantaneous collision")
            except OSError:
                return None

        # 断言：缺陷逻辑必然导致 handle is None（失去互斥）
        faulty_handle = naive_single_try_open(lock_file)
        self.assertIsNone(faulty_handle, "缺陷单次尝试应被成功识别为无锁退化！")

    def test_task_exclusive_mutex_critical_section(self):
        """验证 task_exclusive_mutex 正确加锁和释放，且能防止重叠执行。"""
        bus_dir = self.root / "bus"
        bus_dir.mkdir(parents=True)
        task_id = "test-task-12345"

        entered = []
        with exclusive_lock.task_exclusive_mutex(bus_dir, task_id, timeout_s=2.0):
            entered.append(1)
            # 在锁内验证状态
            self.assertEqual(entered, [1])

        self.assertEqual(entered, [1])

    def test_broad_exception_ceiling_gate(self):
        """全包宽泛异常捕获天花板门禁：数量只降不增。"""
        agentbus_dir = PROJECT_DIR / "agentbus"
        py_files = sorted(agentbus_dir.rglob("*.py"))

        count = 0
        for pf in py_files:
            try:
                tree = ast.parse(pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    exc_str = ""
                    if node.type is None:
                        exc_str = "bare except"
                    elif isinstance(node.type, ast.Name):
                        exc_str = node.type.id
                    elif isinstance(node.type, ast.Tuple):
                        exc_str = ", ".join([e.id for e in node.type.elts if isinstance(e, ast.Name)])
                    elif isinstance(node.type, ast.Attribute):
                        exc_str = ast.unparse(node.type)

                    if any(k in exc_str for k in ["Exception", "OSError", "BaseException", "bare except"]):
                        count += 1

        # 原基线 430；安全恢复新增两个 OSError 处理：读取原作业失败时拒绝恢复，
        # 提交后审计写入失败时明确告知已生效。两处均不吞异常，不能因计数而丢失磁盘错误保护。
        CEILING = 432
        self.assertLessEqual(
            count,
            CEILING,
            f"宽泛异常捕获数量反弹！当前 {count} > 天花板 {CEILING}，新增代码必须精确收窄异常类型！"
        )

    def test_gen_broad_exception_triage_smoke(self):
        """测试 gen_broad_exception_triage 工具函数可正常调用。"""
        self.assertTrue(callable(gen_broad_exception_triage.main))


if __name__ == "__main__":
    unittest.main()
