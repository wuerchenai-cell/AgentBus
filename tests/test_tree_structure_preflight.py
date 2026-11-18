# -*- coding: utf-8 -*-
"""结构写入前预检：`tools/tree_structure_preflight.py` 的单测。

只读静态守卫：不跑 GUI、不跑模型、不访问网络、不写任务树（仅临时目录）。
覆盖：
  * 合法树判 clean（真源 + 定位器都过）
  * **P0 回放**：非法字段（`priority`）必须被**定位到具体节点与键名**，而不是只给一句总报错
  * 逐条结构规则：重复 id / 父指向叶子 / 悬空依赖 / 自依赖 / 进度在非叶 / 断点史在非叶 /
    任务绑定在非叶 / root 带父 / 同一任务被两叶绑定 / 非法章节号 / 断点记录键不符 / 断点任务不在历史里
  * 真源报错但定位器未覆盖时，如实标注（不得假装 clean）
  * 候选 vs 活动树的差异比较（新增/删除/改动键）
  * 只读性、`--strict` 退出码、输入缺失退出码 2
  * 对真实工作区冒烟（结论由文件自身推出，不写死）
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TOOL = _ROOT / "tools" / "tree_structure_preflight.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("tree_structure_preflight", _TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


tsp = _load_tool()
tsp_truth, _REASON = tsp._load_truth()


def _tree():
    """最小合法结构：1 root + 1 branch + 1 leaf。"""
    return {
        "schema_version": 1,
        "section_registry": ["0", "1", "1.1"],
        "roots": [{"id": "project", "parent_id": None, "title": "根", "goal": "g", "section_no": "0"}],
        "branches": [{"id": "b1", "parent_id": "project", "title": "支", "goal": "g", "section_no": "1"}],
        "leaves": [{"id": "L1", "parent_id": "b1", "title": "叶", "goal": "g", "section_no": "1.1"}],
    }


def _codes(problems):
    return [p["code"] for p in problems]


@unittest.skipIf(tsp_truth is None, "无法导入真源校验器")
class LocatorTests(unittest.TestCase):
    def test_valid_tree_is_clean(self):
        tree = _tree()
        self.assertEqual([], tsp.locate(tree, tsp_truth))
        passed, message = tsp.authoritative(tree, tsp_truth)
        self.assertTrue(passed, message)

    def test_p0_replay_illegal_key_is_located_per_node(self):
        """P0 回放：非法键必须被定位到节点 id + 键名（原事故只有一句总报错）。"""
        tree = _tree()
        tree["leaves"][0]["priority"] = "P1"
        problems = tsp.locate(tree, tsp_truth)
        self.assertEqual(["illegal_keys"], _codes(problems))
        self.assertEqual("leaves L1", problems[0]["node"])
        self.assertIn("priority", problems[0]["detail"])
        # 真源同样必须判红
        passed, message = tsp.authoritative(tree, tsp_truth)
        self.assertFalse(passed)
        self.assertIn("never state/results/owner", message)

    def test_all_projection_fields_are_rejected(self):
        """投影字段族（status/owner/state/results/claim/heartbeat）一个都不许写回结构。"""
        for key in ("status", "owner", "state", "results", "claim", "heartbeat"):
            tree = _tree()
            tree["leaves"][0][key] = "x"
            problems = tsp.locate(tree, tsp_truth)
            self.assertIn("illegal_keys", _codes(problems), "字段 %s 未被拦截" % key)

    def test_illegal_key_on_branch_and_root_is_located(self):
        tree = _tree()
        tree["branches"][0]["state"] = "delivered"
        tree["roots"][0]["owner"] = "codex"
        problems = tsp.locate(tree, tsp_truth)
        self.assertEqual(["illegal_keys", "illegal_keys"], _codes(problems))
        self.assertEqual({"branches b1", "roots project"}, {p["node"] for p in problems})

    def test_duplicate_id(self):
        tree = _tree()
        tree["branches"][0]["id"] = "L1"  # 与叶子撞 id
        self.assertIn("duplicate_id", _codes(tsp.locate(tree, tsp_truth)))
        self.assertFalse(tsp.authoritative(tree, tsp_truth)[0])

    def test_parent_pointing_at_leaf(self):
        tree = _tree()
        tree["branches"][0]["parent_id"] = "L1"
        self.assertIn("bad_parent", _codes(tsp.locate(tree, tsp_truth)))

    def test_dangling_and_self_dependency(self):
        tree = _tree()
        tree["leaves"][0]["dependencies"] = ["nope"]
        self.assertIn("bad_dependency", _codes(tsp.locate(tree, tsp_truth)))
        tree2 = _tree()
        tree2["leaves"][0]["dependencies"] = ["L1"]
        self.assertIn("self_dependency", _codes(tsp.locate(tree2, tsp_truth)))

    def test_progress_percent_only_on_leaves(self):
        tree = _tree()
        tree["branches"][0]["progress_percent"] = 50
        self.assertIn("progress_on_non_leaf", _codes(tsp.locate(tree, tsp_truth)))
        tree2 = _tree()
        tree2["leaves"][0]["progress_percent"] = 101
        self.assertIn("bad_progress", _codes(tsp.locate(tree2, tsp_truth)))

    def test_root_with_parent_is_located(self):
        tree = _tree()
        tree["roots"][0]["parent_id"] = "b1"
        self.assertIn("root_with_parent", _codes(tsp.locate(tree, tsp_truth)))

    def test_task_id_only_on_leaves(self):
        tree = _tree()
        tree["branches"][0]["task_id"] = "m-1234567890123-abcdef"
        codes = _codes(tsp.locate(tree, tsp_truth))
        self.assertIn("task_on_non_leaf", codes)

    def test_same_task_bound_by_two_leaves(self):
        tree = _tree()
        tree["leaves"].append({"id": "L2", "parent_id": "b1", "title": "叶2", "goal": "g",
                               "section_no": "1.2", "task_id": "m-1234567890123-abcdef"})
        tree["leaves"][0]["task_id"] = "m-1234567890123-abcdef"
        self.assertIn("task_bound_twice", _codes(tsp.locate(tree, tsp_truth)))

    def test_bad_section_no(self):
        tree = _tree()
        tree["leaves"][0]["section_no"] = "01"
        self.assertIn("bad_section_no", _codes(tsp.locate(tree, tsp_truth)))
        tree2 = _tree()
        tree2["section_registry"] = ["0", "x"]
        self.assertIn("bad_section_no", _codes(tsp.locate(tree2, tsp_truth)))

    def test_handoff_history_rules(self):
        tree = _tree()
        tree["branches"][0]["handoff_history"] = []
        self.assertIn("handoff_on_non_leaf", _codes(tsp.locate(tree, tsp_truth)))

        tree2 = _tree()
        tree2["leaves"][0]["history_task_ids"] = ["m-1234567890123-abcdef"]
        tree2["leaves"][0]["handoff_history"] = [{"task_id": "m-1234567890123-abcdef", "agent": "workbuddy",
                                                  "conversation_id": "cv-" + "a" * 24,
                                                  "timestamp": "2026-10-13T22:00:00+08:00", "reason": "r",
                                                  "owner_confirmed_inactive": True, "extra": 1}]
        self.assertIn("bad_handoff_record", _codes(tsp.locate(tree2, tsp_truth)))

        tree3 = copy.deepcopy(tree2)
        del tree3["leaves"][0]["handoff_history"][0]["extra"]
        self.assertEqual([], tsp.locate(tree3, tsp_truth))

        tree4 = copy.deepcopy(tree3)
        tree4["leaves"][0]["handoff_history"][0]["task_id"] = "m-9999999999999-ffffff"
        self.assertIn("orphan_handoff_task", _codes(tsp.locate(tree4, tsp_truth)))

    def test_truth_error_without_locator_coverage_is_declared(self):
        """真源报错但定位器没覆盖（依赖环）时：必须如实标注，不得假装 clean。"""
        tree = _tree()
        tree["leaves"].append({"id": "L2", "parent_id": "b1", "title": "叶2", "goal": "g",
                               "section_no": "1.2", "dependencies": ["L1"]})
        tree["leaves"][0]["dependencies"] = ["L2"]
        self.assertEqual([], tsp.locate(tree, tsp_truth))
        passed, _message = tsp.authoritative(tree, tsp_truth)
        self.assertFalse(passed, "环形依赖必须被真源拦下")
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "cand.json"
            f.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
            report, code = tsp.run(f, None)
        self.assertEqual("rejected", report["verdict"])
        self.assertIn("真源报错但定位器未覆盖", report.get("note", ""))
        self.assertEqual(1, code)


class CompareTests(unittest.TestCase):
    def test_compare_reports_add_remove_and_changed_keys(self):
        before = _tree()
        after = copy.deepcopy(before)
        after["leaves"][0]["title"] = "改过"
        after["leaves"].append({"id": "L2", "parent_id": "b1", "title": "新", "goal": "g", "section_no": "1.2"})
        diff = tsp.compare(after, before)
        self.assertEqual(["L2"], diff["added"])
        self.assertEqual([], diff["removed"])
        self.assertEqual(["L1"], [entry["id"] for entry in diff["changed"]])
        self.assertIn("title", diff["changed"][0]["keys"])

    def test_compare_detects_removed_and_kind_change(self):
        before = _tree()
        after = copy.deepcopy(before)
        after["leaves"] = []
        diff = tsp.compare(after, before)
        self.assertEqual(["L1"], diff["removed"])


class CliTests(unittest.TestCase):
    def _write(self, tmp: str, payload) -> Path:
        path = Path(tmp) / "cand.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_strict_exit_zero_on_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, _tree())
            self.assertEqual(0, tsp.main(["--file", str(path), "--strict"]))

    def test_strict_exit_nonzero_on_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = _tree()
            bad["leaves"][0]["priority"] = "P1"
            path = self._write(tmp, bad)
            self.assertEqual(1, tsp.main(["--file", str(path), "--strict"]))

    def test_missing_file_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(2, tsp.main(["--file", str(Path(tmp) / "nope.json")]))

    def test_missing_inputs_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(2, tsp.main(["--workspace", str(Path(tmp))]))

    def test_read_only_inputs_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = _tree()
            bad["leaves"][0]["owner"] = "codex"
            path = self._write(tmp, bad)
            before = path.read_bytes()
            tsp.main(["--file", str(path)])
            self.assertEqual(before, path.read_bytes())


@unittest.skipIf(tsp_truth is None, "无法导入真源校验器")
class RealWorkspaceTests(unittest.TestCase):
    def test_real_tree_preflight_is_consistent(self):
        ws = next((p for p in [_ROOT, *_ROOT.parents] if (p / '.agentbus' / 'config.json').is_file()), None)
        tree_path = ws / ".agentbus" / "task-tree.json"
        if not tree_path.is_file():
            self.skipTest("未找到真实 .agentbus/task-tree.json，跳过")
        before = tree_path.read_bytes()
        report, code = tsp.run(None, ws)
        self.assertIn(report["verdict"], ("clean", "rejected"))
        # 结论必须与真源一致（不写死 clean）
        self.assertEqual(report["authoritative_ok"], report["verdict"] == "clean")
        self.assertEqual(before, tree_path.read_bytes(), "预检不得改动活动任务树")
        self.assertIn(code, (0, 1))

    def test_tool_does_not_rewrite_tree_when_candidate_is_clean(self):
        ws = next((p for p in [_ROOT, *_ROOT.parents] if (p / '.agentbus' / 'config.json').is_file()), None)
        tree_path = ws / ".agentbus" / "task-tree.json"
        if not tree_path.is_file():
            self.skipTest("未找到真实树，跳过")
        original = tree_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            cand = Path(tmp) / "cand.json"
            cand.write_bytes(original)  # 候选 == 活动树
            report, code = tsp.run(cand, ws)
            self.assertEqual("clean", report["verdict"])
            self.assertEqual(0, code)
            self.assertEqual([], diff_ids(report.get("compare_to_current") or {}))
        self.assertEqual(original, tree_path.read_bytes())


def diff_ids(diff: dict) -> list:
    return list(diff.get("added") or []) + list(diff.get("removed") or []) + list(diff.get("changed") or [])


if __name__ == "__main__":
    unittest.main()
