# -*- coding: utf-8 -*-
"""§13.11 CX13Q-UI-LOCK-FAIL-CLOSED — 本机偏好锁失败**不得退化为无锁保存**。

背景（两轮修复的历史）
--------------------
* §7.29 首版修复失败的原因：`_interprocess_lock` 把「打开锁文件」与「加锁」塞进**同一个**
  `except OSError`，两个进程同时首次创建锁文件时 Windows 抛 `PermissionError`
  （`⊂ OSError`）→ 被静默吞掉 → `handle=None` → **照样 yield** → 根本没加锁。
* §7.29 二次修复把"打开"改成**重试到期限**，于是退化变**罕见**了；
  但退化路径**依然存在**：期限用尽或加锁失败时仍然 `yield`，调用方照旧读改写。
  实测（本叶认领时复现）：注入三种失败，**3/3 分支 save 返回 True 且写盘**。
* 本叶把语义从"退化变罕见"改成"**失败就封口**"：拿不到锁 → 不进入临界区 → 返回失败。

验收（§13.11 原文）
------------------
  注入 `_open_lock_handle=None` / `_lock_acquire OSError` 均**零写入**；
  真实两个独立进程长持锁与更新不同项目字段无丢失，失败用户可重试且不影响另项目；
  保留原子替换与已有双进程正常保存行为；**锁时限覆盖总获取过程**。

只读性：本模块只写临时目录，不触碰工作区、不跑模型、不联网。
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agentbus import gui_state as gs  # noqa: E402

# 七个经过 `_interprocess_lock` 的保存入口：(名字, 合法入参构造器)
#
# ⚠️ 这些入参必须**真的合法**：非法入参会让 save_* 在进入锁之前就返回 False，
# 于是"锁失败注入"用例会**空跑**并假装通过。本模块用 `_assert_payloads_reach_validator`
# 把这一点变成显式断言——这个坑在本叶开发时被真实踩到过一次（见回执）。
SAVE_CALLS = (
    ("save_automatic_release_checks", lambda ws: {"enabled": True}),
    ("save_last_workspace", lambda ws: {}),
    ("save_viewport", lambda ws: {"value": {"version": 1, "scale": 2.0, "panX": 7.0,
                                            "panY": 8.0, "width": 801, "height": 601}}),
    ("save_branch_folds", lambda ws: {"branch_ids": ["branch-a", "branch-b"]}),
    ("save_member_icon_paths", lambda ws: {"values": {"alpha": r"C:\icons\alpha.exe"}}),
    ("save_notes", lambda ws: {"notes": [{"id": "note-1", "x": 0.0, "y": 0.0, "w": 200.0,
                                          "h": 120.0, "text": "hello", "color": "yellow"}]}),
    ("save_priorities", lambda ws: {"priorities": {"leaf-1": "P1"}}),
)


def _assert_payloads_reach_validator():
    """自检：每个保存入口的入参都必须能通过各自的校验器。

    否则 save_* 会在用到锁之前就返回 False，锁失败注入用例会静默空跑。
    每条断言都带消息，失败时直接说明是哪个入参不合法。
    """
    checks = (
        ("viewport", gs._validated_viewport(
            {"version": 1, "scale": 2.0, "panX": 7.0, "panY": 8.0,
             "width": 801, "height": 601})),
        ("branch_folds", gs._validated_branch_folds(["branch-a", "branch-b"])),
        ("member_icon_paths", gs._validated_member_icon_paths(
            {"alpha": r"C:\icons\alpha.exe"})),
        ("note", gs._validated_note(
            {"id": "note-1", "x": 0.0, "y": 0.0, "w": 200.0, "h": 120.0,
             "text": "hello", "color": "yellow"})),
        ("priorities", gs._validated_priorities({"leaf-1": "P1"})),
    )
    for name, value in checks:
        assert value is not None, (
            "自检失败：%s 的测试入参不合法，会导致锁失败用例空跑。"
            "请对照 gui_state 的对应校验器修正 SAVE_CALLS。" % name)


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<absent>"


@contextlib.contextmanager
def _no_lock_handle():
    """期限用尽：`_acquire_interprocess_lock` 返回 None。"""
    orig = gs._acquire_interprocess_lock
    gs._acquire_interprocess_lock = lambda *a, **k: None
    try:
        yield
    finally:
        gs._acquire_interprocess_lock = orig


@contextlib.contextmanager
def _open_raises():
    """前置失败：创建/打开锁文件抛 OSError。"""
    orig = gs._acquire_interprocess_lock

    def boom(*a, **k):
        raise OSError(13, "injected: lock file can not be created")

    gs._acquire_interprocess_lock = boom
    try:
        yield
    finally:
        gs._acquire_interprocess_lock = orig


@contextlib.contextmanager
def _lock_raises():
    """加锁本身失败：`_lock_acquire` 抛 OSError。"""
    orig = gs._lock_acquire

    def boom(*a, **k):
        raise OSError(33, "injected: locking failed")

    gs._lock_acquire = boom
    try:
        yield
    finally:
        gs._lock_acquire = orig


@contextlib.contextmanager
def _short_timeout(seconds: float):
    orig = gs.LOCK_TIMEOUT_S
    gs.LOCK_TIMEOUT_S = seconds
    try:
        yield
    finally:
        gs.LOCK_TIMEOUT_S = orig


def _invoke(name, ws, state, **extra):
    fn = getattr(gs, name)
    if name == "save_automatic_release_checks":
        return fn(path=state, **extra)
    return fn(ws, path=state, **extra)


class FailClosedTests(unittest.TestCase):
    """三种锁失败注入下，七个保存入口都必须零写入并返回 False。"""

    def setUp(self):
        # 每个用例前自检入参合法性，避免"用例空跑还假装通过"
        _assert_payloads_reach_validator()

    def _assert_fail_closed(self, inject, tag):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "proj"
            ws.mkdir()
            state = root / "ui.json"
            # 基线：正常路径必须先能写成功，否则本用例测的不是"锁失败"而是"入参非法"
            self.assertTrue(_invoke("save_viewport", ws, state,
                                    value={"version": 1, "scale": 1.0, "panX": 0.0,
                                           "panY": 0.0, "width": 800, "height": 600}),
                            "基线保存必须成功（否则用例空跑）")
            before = _digest(state)

            with _short_timeout(0.25), inject():
                for name, builder in SAVE_CALLS:
                    extra = builder(ws)
                    extra.pop("workspace", None)
                    ret = _invoke(name, ws, state, **extra)
                    self.assertIs(ret, False, "%s 在锁失败时必须返回 False [%s]" % (name, tag))
                reason = gs.last_lock_error()

            self.assertEqual(before, _digest(state), "%s：锁失败时状态文件字节必须未变" % tag)
            self.assertTrue(reason, "%s：锁失败必须给出可解释原因" % tag)

    def test_deadline_exhausted_writes_nothing(self):
        self._assert_fail_closed(_no_lock_handle, "期限用尽")

    def test_open_failure_writes_nothing(self):
        self._assert_fail_closed(_open_raises, "打开失败")

    def test_lock_failure_writes_nothing(self):
        self._assert_fail_closed(_lock_raises, "加锁失败")

    def test_retry_after_failure_succeeds(self):
        """失败用户可重试：锁恢复后同一入口必须能写成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "proj"
            ws.mkdir()
            state = root / "ui.json"
            payload = {"version": 1, "scale": 1.0, "panX": 0.0, "panY": 0.0,
                       "width": 800, "height": 600}
            with _short_timeout(0.2), _no_lock_handle():
                self.assertFalse(_invoke("save_viewport", ws, state, value=payload))
            # 锁恢复
            self.assertTrue(_invoke("save_viewport", ws, state, value=payload),
                            "锁恢复后重试必须成功")
            self.assertEqual("", gs.last_lock_error(), "成功后原因必须清空")

    def test_failure_does_not_disturb_other_project(self):
        """失败不得影响另一个项目字段：一个项目写失败，另一个项目的既有数据必须原样保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_a, ws_b = root / "projA", root / "projB"
            ws_a.mkdir()
            ws_b.mkdir()
            state = root / "ui.json"
            view_a = {"version": 1, "scale": 1.5, "panX": 1.0, "panY": 1.0,
                      "width": 800, "height": 600}
            view_b = {"version": 1, "scale": 3.0, "panX": 2.0, "panY": 2.0,
                      "width": 900, "height": 700}
            self.assertTrue(_invoke("save_viewport", ws_a, state, value=view_a))
            self.assertTrue(_invoke("save_viewport", ws_b, state, value=view_b))
            snapshot = _digest(state)

            with _short_timeout(0.2), _no_lock_handle():
                self.assertFalse(_invoke("save_viewport", ws_a, state,
                                         value={"version": 1, "scale": 7.0, "panX": 3.0,
                                                "panY": 3.0, "width": 800, "height": 600}))

            self.assertEqual(snapshot, _digest(state), "失败不得改动任何项目的既有数据")
            entries = gs._read_state(state).get("viewports") or {}
            self.assertEqual(2, len(entries), "两个项目的数据都必须还在")

    def test_normal_path_still_writes_and_replaces_atomically(self):
        """保留正常保存行为：七个入口都能写成功，且不残留 .tmp 文件（原子替换）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "proj"
            ws.mkdir()
            state = root / "ui.json"
            for name, builder in SAVE_CALLS:
                extra = builder(ws)
                extra.pop("workspace", None)
                self.assertIs(_invoke(name, ws, state, **extra), True,
                              "%s 正常路径必须成功" % name)
            leftovers = [p.name for p in root.iterdir() if ".tmp-" in p.name]
            self.assertEqual([], leftovers, "原子替换不得残留临时文件")

    def test_last_lock_error_is_specific_and_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "proj"
            ws.mkdir()
            state = root / "ui.json"
            # last_lock_error() 是**进程级**「最近一次获取尝试」的原因，不假设初始为空
            # （同一进程内先跑过的用例可能已留下原因）。这里断言的是"尝试语义"：
            # 失败 → 非空且含具体原因；紧接着成功 → 清空。
            with _short_timeout(0.2), _no_lock_handle():
                with gs._interprocess_lock(state) as locked:
                    self.assertFalse(locked)
            self.assertIn("不可用", gs.last_lock_error())
            self.assertIn("ui.json.lock", gs.last_lock_error(),
                          "原因里应带上具体锁文件路径，便于定位")
            with gs._interprocess_lock(state) as locked:
                self.assertTrue(locked)
            self.assertEqual("", gs.last_lock_error(), "成功一次后原因应清空")


class DeadlineSemanticsTests(unittest.TestCase):
    """锁时限必须覆盖「打开 + 加锁」的**全过程**，不是各自计时。"""

    def test_single_deadline_covers_whole_acquisition(self):
        timeout = 0.6
        clock = [100.0]
        deadlines = []
        opening_attempts = [0]
        handles = []
        class Clock:
            @staticmethod
            def monotonic():
                return clock[0]
            @staticmethod
            def sleep(seconds):
                clock[0] += seconds
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "ui.json"
            lock_path = state.with_name(state.name + '.lock')
            real_open = open
            def delayed_open(path, *args, **kwargs):
                if Path(path) == lock_path:
                    opening_attempts[0] += 1
                    if opening_attempts[0] == 1:
                        clock[0] += 0.35
                        raise OSError(13, 'scripted opening contention')
                handle = real_open(path, *args, **kwargs)
                handles.append(handle)
                return handle
            def denied_lock(handle, deadline):
                deadlines.append(deadline)
                raise OSError(33, 'scripted lock contention')
            # Scheduler/filesystem pauses are outside the algorithm's control.
            # Exercise opening time as well as locking retries and assert the
            # actual absolute deadline, not an unreliable wall-clock multiple.
            with _short_timeout(timeout), patch.object(gs, 'time', Clock), \
                    patch.object(gs, 'open', delayed_open, create=True), \
                    patch.object(gs, '_lock_acquire', denied_lock):
                t0 = clock[0]
                with gs._interprocess_lock(state) as locked:
                    self.assertFalse(locked)
                elapsed = clock[0] - t0
        self.assertGreater(opening_attempts[0], 1, '必须覆盖打开锁文件重试')
        self.assertTrue(handles, '打开重试后必须实际获得过文件句柄')
        self.assertTrue(all(handle.closed for handle in handles), '失败后必须关闭全部句柄')
        self.assertGreater(len(deadlines), 1, '必须覆盖多次加锁重试')
        self.assertTrue(all(value == t0 + timeout for value in deadlines),
                        '打开与每次加锁必须使用同一个原始截止时间')
        self.assertGreaterEqual(elapsed, timeout)
        self.assertLess(elapsed, timeout + 0.011)


class StaticGuardsTests(unittest.TestCase):
    """静态守卫：防止"静默退化"这个模式被重新写回来。"""

    def _lock_source(self) -> str:
        src = (_ROOT / "agentbus" / "gui_state.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_interprocess_lock":
                return ast.get_source_segment(src, node)
        self.fail("找不到 _interprocess_lock")

    def test_yields_lock_ownership_boolean(self):
        seg = self._lock_source()
        self.assertIn("yield handle is not None", seg,
                      "_interprocess_lock 必须把「是否持锁」显式交给调用方")

    def test_no_silent_degradation_swallow(self):
        """不得再出现 `except OSError:` 里把 handle 置 None 后无条件继续的模式。"""
        seg = self._lock_source()
        self.assertNotIn("handle = None\n    try:\n        yield", seg)
        # 关键：所有保存点都必须检查持锁结果
        src = (_ROOT / "agentbus" / "gui_state.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("_interprocess_lock(state_file) as locked:"),
                         src.count("if not locked:"),
                         "每个使用 _interprocess_lock 的保存点都必须检查持锁结果")

    def test_every_save_entrypoint_is_guarded(self):
        src = (_ROOT / "agentbus" / "gui_state.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        guarded = 0
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("save_"):
                continue
            seg = ast.get_source_segment(src, node) or ""
            if "_interprocess_lock(" in seg:
                self.assertIn("as locked:", seg, "%s 未检查持锁结果" % node.name)
                self.assertIn("if not locked:", seg, "%s 缺少失败封口分支" % node.name)
                guarded += 1
        self.assertEqual(len(SAVE_CALLS), guarded,
                         "受锁保护的保存入口数量变了（新增/删除入口时请同步本测试）")


if __name__ == "__main__":
    unittest.main()
