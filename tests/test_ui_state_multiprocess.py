"""§7.29 本机 UI 状态的**跨进程**不丢更新回归。

问题：`gui_state.py` 的"读→改→写"只由**线程内** `RLock` 保护。两个独立 EXE 进程各自
持有自己的 RLock，于是可交错为：P1 读 → P2 读 → P1 写 → P2 写，**P2 覆盖 P1**（丢更新）。
即使 `os.replace` 保证不出现半截文件，也挡不住这种逻辑丢失。

修法：给 5 个写者的"读→改→写"再加一层**进程间**互斥（同目录 `.lock` 文件）。
    进程内串行 + 进程间串行 + 替换原子 = 不丢更新。

**首版修复为何仍然丢更新（本文件的重点回归）**：第一版用
    `try: handle = open(lock_path, "a+b") ... except OSError: handle = None`
把"打开锁文件"和"加锁"包在同一个 `except OSError` 里。而两个进程**同时**首次打开
一个尚不存在的锁文件时，Windows 会让其中一个拿到 `PermissionError`（共享冲突，实测
约 1/3 概率）；`PermissionError` 是 `OSError` 的子类，于是被静默吞掉 → `handle=None`
→ **`yield` 时根本没加锁**，互斥形同虚设。现在把"打开"与"加锁"拆开并**重试到期限**，
只有环境真坏到打不开才退化；`test_lock_open_race_*` 两个用例专门守住这条。

本测试用**两个真实子进程**验证（任务书要求），并自带对照：
  * `test_concurrent_processes_do_not_lose_updates`       — 现在的代码 → 双方都在
  * `test_repeated_first_open_races_do_not_lose_updates`  — 连续 5 轮全新锁文件 → 轮轮双全
  * `test_lock_is_what_prevents_the_loss`                 — 把进程间锁打回 no-op → 复现丢失
  * `test_transient_open_failure_is_retried_not_swallowed`— 注入一次 PermissionError → 仍要拿到锁

为让竞态**确定出现**，子进程会在读到状态后短暂 sleep，把"读→写"窗口拉宽
（这不是制造 bug，而是把原本窗口极窄、难以观测的竞态变成可稳定复现）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_INTEGRATION = str(Path(__file__).resolve().parents[1])
if _INTEGRATION not in sys.path:
    sys.path.insert(0, _INTEGRATION)

from agentbus import gui_state as gs

_WORKER = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agentbus import gui_state as gs

state_file = Path(sys.argv[2])
ws = Path(sys.argv[3])
delay = float(sys.argv[4])
disable_lock = sys.argv[5] == "1"
ready_marker = Path(sys.argv[6])
go_marker = Path(sys.argv[7])
ws.mkdir(parents=True, exist_ok=True)

if disable_lock:                     # 对照组：把进程间锁打回 no-op
    import contextlib
    @contextlib.contextmanager
    def _no_lock(_state_file):
        yield
    gs._interprocess_lock = _no_lock

_orig_read = gs._read_state
def slow_read(path):
    value = _orig_read(path)
    time.sleep(delay)                # 拉宽"读→改→写"窗口，令交错稳定出现
    return value
gs._read_state = slow_read

# 汇合点：两个进程都到齐后父进程才放行，保证它们**同时在窗口内**（消除启动抖动）。
ready_marker.write_text("1", encoding="utf-8")
deadline = time.time() + 60
while not go_marker.exists():
    if time.time() > deadline:
        raise SystemExit("rendezvous timeout")
    time.sleep(0.005)

gs.save_viewport(ws, {"version": 1, "scale": 1.0, "panX": 1, "panY": 1,
                      "width": 800, "height": 600}, path=state_file)
gs.save_branch_folds(ws, ["b-1"], path=state_file)
print("done", ws.name)
'''


class _Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_file = self.root / "ui.json"
        self.ws_a = self.root / "projA"
        self.ws_b = self.root / "projB"
        self.worker = self.root / "worker.py"
        self.worker.write_text(_WORKER, encoding="utf-8")
        self.go = self.root / "go"

    def _run_pair(self, *, delay, disable_lock):
        ready = [self.root / ("ready-%s" % ws.name) for ws in (self.ws_a, self.ws_b)]
        procs = [
            subprocess.Popen([sys.executable, str(self.worker), _INTEGRATION,
                              str(self.state_file), str(ws), str(delay),
                              "1" if disable_lock else "0",
                              str(marker), str(self.go)])
            for ws, marker in zip((self.ws_a, self.ws_b), ready)
        ]
        # 等两个进程都进入汇合点，再放行——保证窗口重叠
        deadline = time.time() + 60
        while not all(m.exists() for m in ready):
            if time.time() > deadline:
                for proc in procs:
                    proc.kill()
                self.fail("两个子进程未能在超时前汇合")
            time.sleep(0.01)
        self.go.write_text("1", encoding="utf-8")
        for proc in procs:
            proc.wait(timeout=120)
        return {
            "a_viewport": gs.load_viewport(self.ws_a, path=self.state_file),
            "b_viewport": gs.load_viewport(self.ws_b, path=self.state_file),
            "a_folds": gs.load_branch_folds(self.ws_a, path=self.state_file),
            "b_folds": gs.load_branch_folds(self.ws_b, path=self.state_file),
        }


class CrossProcessTests(_Harness):
    def test_concurrent_processes_do_not_lose_updates(self):
        result = self._run_pair(delay=0.6, disable_lock=False)
        self.assertIsNotNone(result["a_viewport"], "projA 的视口不得被 projB 覆盖")
        self.assertIsNotNone(result["b_viewport"], "projB 的视口不得被 projA 覆盖")
        self.assertTrue(result["a_folds"], "projA 的折叠状态不得丢失")
        self.assertTrue(result["b_folds"], "projB 的折叠状态不得丢失")

    def test_lock_is_what_prevents_the_loss(self):
        """对照：把进程间锁打回 no-op，同样的并发就会丢更新——证明锁是承重的。"""
        result = self._run_pair(delay=0.6, disable_lock=True)
        lost = (result["a_viewport"] is None or result["b_viewport"] is None)
        self.assertTrue(lost,
                        "若取掉进程间锁也不丢，说明本测试没能制造出竞态窗口（不可信）")

    def test_repeated_first_open_races_do_not_lose_updates(self):
        """首轮 `open` 竞态是丢更新的高发场景：连续 5 轮**全新锁文件**，必须轮轮双全。

        每轮都用新的状态文件（=新的 `.lock`），把"两个进程同时首次打开锁文件"这一
        竞态重复触发；首版修复在这里会以约 1/3 的概率丢更新。
        """
        for round_no in range(5):
            self.go.unlink(missing_ok=True)
            self.state_file = self.root / ("ui-%d.json" % round_no)
            self.ws_a = self.root / ("rA-%d" % round_no)
            self.ws_b = self.root / ("rB-%d" % round_no)
            result = self._run_pair(delay=0.4, disable_lock=False)
            self.assertIsNotNone(result["a_viewport"],
                                 "第 %d 轮：projA 视口丢失（首轮 open 竞态又漏了）" % round_no)
            self.assertIsNotNone(result["b_viewport"],
                                 "第 %d 轮：projB 视口丢失（首轮 open 竞态又漏了）" % round_no)
            self.assertTrue(result["a_folds"], "第 %d 轮：projA 折叠状态丢失" % round_no)
            self.assertTrue(result["b_folds"], "第 %d 轮：projB 折叠状态丢失" % round_no)


class LockOpenRaceTests(unittest.TestCase):
    """守住 §7.29 的真实根因：瞬时打开失败**不得**静默降级为不加锁。

    §13.11 更新（2026-10-13）：契约从「彻底拿不到锁时**退化为无锁写入**」
    改为「**失败封口**——拿不到锁就不进入临界区，由调用方返回可解释失败」。
    因此本类中三处断言随之更新：

      * 重试语义不变（瞬时失败必须重试，不得首次即放弃）；
      * 期限语义不变（持续失败必须**重试到期限**才放弃）；
      * **降级语义改变**：`_interprocess_lock` 不再是"照样 yield 让调用方写"，
        而是 **yield 一个布尔**，为 False 时要求调用方不得读改写。
        （调用方 `gui_state.save_*` 的封口行为见 `test_gui_state_lock_failures.py`。）
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_transient_open_failure_is_retried_not_swallowed(self):
        """注入一次 PermissionError（模拟首轮 open 竞态），仍须拿到真实锁句柄。"""
        lock_path = self.root / "state.json.lock"
        real_open = os.open
        attempts = []

        def flaky_open(path, flags, mode=0o777):
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, mode)

        with mock.patch.object(gs.os, "open", flaky_open):
            handle = gs._acquire_interprocess_lock(
                lock_path, time.monotonic() + gs.LOCK_TIMEOUT_S)
        self.assertIsNotNone(
            handle, "瞬时打开失败必须重试到期限，而不是返回 None（= 静默放弃互斥）")
        self.assertGreaterEqual(len(attempts), 2, "应当发生过重试")
        try:
            gs._lock_release(handle)
        finally:
            handle.close()

    def test_persistent_open_failure_gives_up_only_after_deadline(self):
        """持续打不开时返回 None（期限用尽才放弃），且放弃前必须真的重试过。"""
        lock_path = self.root / "state.json.lock"
        calls = {"n": 0}

        def always_denied(path, flags, mode=0o777):
            calls["n"] += 1
            raise PermissionError(13, "Permission denied")

        original_timeout = gs.LOCK_TIMEOUT_S
        gs.LOCK_TIMEOUT_S = 0.05              # 缩短期限，保持用例快速
        try:
            with mock.patch.object(gs.os, "open", always_denied):
                handle = gs._acquire_interprocess_lock(
                    lock_path, time.monotonic() + gs.LOCK_TIMEOUT_S)
        finally:
            gs.LOCK_TIMEOUT_S = original_timeout
        self.assertIsNone(handle, "期限内始终打不开才允许放弃")
        self.assertGreaterEqual(calls["n"], 2, "放弃前必须真的重试过")

    def test_lock_wrapper_reports_not_locked_when_unopenable(self):
        """§13.11 契约：拿不到锁时**不得抛异常**，但必须**明确报告未持锁**。

        旧契约（§7.29）是"照样 yield 让调用方写"，那正是本叶要改掉的静默降级。
        """
        state_file = self.root / "state.json"

        def no_lock(*_a, **_k):
            return None

        with mock.patch.object(gs, "_acquire_interprocess_lock", no_lock):
            entered = False
            with gs._interprocess_lock(state_file) as locked:
                entered = True
            self.assertTrue(entered, "不得抛异常阻断调用方（既有契约）")
            self.assertIs(locked, False,
                          "拿不到锁必须报告未持锁，调用方据此封口而非继续写")
        self.assertTrue(gs.last_lock_error(), "未持锁必须留下可解释原因")

    def test_lock_wrapper_reports_locked_on_success(self):
        """对照：正常拿到锁时必须报告已持锁（防止把成功路径也判成失败）。"""
        state_file = self.root / "state.json"
        with gs._interprocess_lock(state_file) as locked:
            self.assertIs(locked, True)
        self.assertEqual("", gs.last_lock_error(), "成功后原因应清空")


class LockMechanismTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_file = self.root / "ui.json"
        self.ws = self.root / "proj"
        self.ws.mkdir()

    def test_all_writers_take_the_interprocess_lock(self):
        """每个"读-改-写"写者都必须串行化。

        不写死具体条数：写者会随新增状态族自然增长（例如后来加进来的
        `save_priorities`），写死 `== 5` 会因为**别人合法扩展**而误报。改为
        「加锁点 ≥ 写回点」——新增写者若忘了加锁即变红，而合法扩展不会。
        """
        src = Path(gs.__file__).read_text(encoding="utf-8")
        locked = src.count("_interprocess_lock(state_file)")
        writes = src.count("_write_state(state_file, state)")
        self.assertGreaterEqual(locked, 5, "至少 5 个写者应接入进程间锁")
        self.assertGreaterEqual(
            locked, writes,
            "每个读-改-写写者都必须在 _interprocess_lock 保护下写回"
            "（加锁点 %d < 写回点 %d）" % (locked, writes))

    def test_readers_are_not_serialised(self):
        """读者不需要跨进程锁：os.replace 原子，读到的必是完整文件。"""
        src = Path(gs.__file__).read_text(encoding="utf-8")
        for reader in ("_read_state(state_file)",):
            self.assertIn(reader, src)
        self.assertIn("os.replace", src, "写入必须保持原子替换")

    def test_lock_is_released_after_save(self):
        self.assertTrue(gs.save_viewport(
            self.ws, {"version": 1, "scale": 1.0, "panX": 0, "panY": 0,
                      "width": 800, "height": 600}, path=self.state_file))
        # 能再次进入即说明上一把锁已释放（否则会在这里超时）
        self.assertTrue(gs.save_branch_folds(self.ws, ["b-1"], path=self.state_file))

    def test_lock_file_lives_beside_state_not_in_project(self):
        gs.save_viewport(self.ws, {"version": 1, "scale": 1.0, "panX": 0, "panY": 0,
                                   "width": 800, "height": 600}, path=self.state_file)
        lock_path = self.state_file.with_name(self.state_file.name + ".lock")
        self.assertTrue(lock_path.exists(), "锁文件应位于状态文件旁")
        leaked = [p for p in self.ws.rglob("*.lock")]
        self.assertEqual([], leaked, "不得在项目目录里留下锁文件")

    def test_state_file_remains_valid_json(self):
        for index in range(5):
            gs.save_viewport(self.ws, {"version": 1, "scale": 1.0, "panX": index,
                                       "panY": index, "width": 800, "height": 600},
                             path=self.state_file)
        payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIn("viewports", payload)


if __name__ == "__main__":
    unittest.main()
