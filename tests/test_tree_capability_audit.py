# -*- coding: utf-8 -*-
"""§10.17 能力路由守卫：`tools/tree_capability_audit.py` 的单测。

只读静态守卫：不执行 GUI、不跑模型、不访问网络、不写工作区。
覆盖：
  * 死叶检出——含未登记能力名 / 无成员同时具备全部所需能力
  * 单点路由检出——所需能力仅 1 个成员具备
  * 无死叶时判定 clean；--strict 退出码语义
  * 只读性（审计不改动输入文件）
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.project_paths import host_project_root

_TOOL = _ROOT / "tools" / "tree_capability_audit.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("tree_capability_audit", _TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


tca = _load_tool()


def _config(workers):
    return {"schema_version": 1, "workers": workers}


def _tree(leaves, branches=None):
    return {"schema_version": 1, "roots": [], "branches": branches or [], "leaves": leaves}


def _leaf(sec, caps, title="t"):
    return {"id": "X-" + sec.replace(".", "_"), "section_no": sec, "title": title,
            "required_capabilities": list(caps)}


class CapabilityAuditTests(unittest.TestCase):
    # ---------- 能力注册表 ----------
    def test_registered_capabilities_indexes_members(self):
        cfg = _config({
            "a": {"capabilities": ["python", "css"]},
            "b": {"capabilities": ["python"]},
            "c": {},
        })
        idx = tca.registered_capabilities(cfg)
        self.assertEqual(["a", "b"], idx["python"])
        self.assertEqual(["a"], idx["css"])
        self.assertNotIn("", idx)

    def test_members_without_capabilities_are_listed(self):
        cfg = _config({"a": {"capabilities": ["python"]}, "b": {}, "c": {"capabilities": []}})
        rep = tca.audit(_tree([]), cfg)
        self.assertEqual(["b", "c"], rep["members_without_capabilities"])

    # ---------- 缺陷一：死叶 ----------
    def test_unknown_capability_makes_a_dead_leaf(self):
        cfg = _config({"a": {"capabilities": ["python"]}})
        tree = _tree([_leaf("1.1", ["python", "bus-protocol"], "死叶")])
        rep = tca.audit(tree, cfg)
        self.assertEqual(1, rep["counts"]["dead_leaves"])
        dead = rep["dead_nodes"][0]
        self.assertEqual(["bus-protocol"], dead["unknown_capabilities"])
        self.assertEqual([], dead["eligible_agents"])
        self.assertIn("未登记能力名", dead["reason"])
        self.assertEqual("dead_nodes_present", rep["verdict"])

    def test_known_capabilities_but_no_single_member_holds_all(self):
        """两个能力各自存在，但没有任何成员同时具备 → 同样是死叶。"""
        cfg = _config({"a": {"capabilities": ["python"]}, "b": {"capabilities": ["css"]}})
        tree = _tree([_leaf("1.1", ["python", "css"])])
        rep = tca.audit(tree, cfg)
        self.assertEqual(1, rep["counts"]["dead_leaves"])
        self.assertEqual([], rep["dead_nodes"][0]["unknown_capabilities"])
        self.assertIn("无任何成员", rep["dead_nodes"][0]["reason"])

    def test_intersection_semantics(self):
        """候选成员必须是全部所需能力的交集，不是并集。"""
        cfg = _config({
            "a": {"capabilities": ["python"]},
            "b": {"capabilities": ["python", "testing"]},
            "c": {"capabilities": ["testing"]},
        })
        rep = tca.audit(_tree([_leaf("1.1", ["python", "testing"])]), cfg)
        self.assertEqual(0, rep["counts"]["dead_leaves"])
        self.assertEqual(1, rep["counts"]["single_holder_nodes"])
        self.assertEqual(["b"], rep["single_holder_nodes"][0]["eligible_agents"])

    # ---------- 缺陷二：单点路由 ----------
    def test_single_holder_is_reported(self):
        cfg = _config({"a": {"capabilities": ["python", "css"]}})
        rep = tca.audit(_tree([_leaf("1.1", ["css"])]), cfg)
        self.assertEqual(0, rep["counts"]["dead_leaves"])
        self.assertEqual(1, rep["counts"]["single_holder_nodes"])

    def test_multi_holder_is_not_flagged(self):
        cfg = _config({"a": {"capabilities": ["python"]}, "b": {"capabilities": ["python"]}})
        rep = tca.audit(_tree([_leaf("1.1", ["python"])]), cfg)
        self.assertEqual(0, rep["counts"]["dead_leaves"])
        self.assertEqual(0, rep["counts"]["single_holder_nodes"])

    # ---------- 无条件下不计入 ----------
    def test_nodes_without_conditions_are_counted_separately(self):
        cfg = _config({"a": {"capabilities": ["python"]}})
        tree = _tree([_leaf("1.1", []), _leaf("1.2", [])])
        rep = tca.audit(tree, cfg)
        self.assertEqual(2, rep["counts"]["nodes_without_conditions"])
        self.assertEqual(0, rep["counts"]["nodes_with_conditions"])
        self.assertEqual("clean", rep["verdict"])

    # ---------- 排序稳定性 ----------
    def test_dead_nodes_sorted_by_section(self):
        """章节号按数字元组排序：§2 先于 §10（自然读法），而非字典序。"""
        cfg = _config({"a": {"capabilities": ["python"]}})
        tree = _tree([_leaf("10.2", ["nope"]), _leaf("2.1", ["nope"]), _leaf("10.1", ["nope"])])
        rep = tca.audit(tree, cfg)
        self.assertEqual(["2.1", "10.1", "10.2"],
                         [d["section_no"] for d in rep["dead_nodes"]])

    # ---------- clean 判定 ----------
    def test_clean_when_no_dead_nodes(self):
        cfg = _config({"a": {"capabilities": ["python"]}, "b": {"capabilities": ["python"]}})
        rep = tca.audit(_tree([_leaf("1.1", ["python"])]), cfg)
        self.assertEqual("clean", rep["verdict"])

    # ---------- 只读性 ----------
    def test_audit_does_not_modify_inputs(self):
        cfg = _config({"a": {"capabilities": ["python"]}})
        tree = _tree([_leaf("1.1", ["nope"])])
        cfg_before = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        tree_before = json.dumps(tree, sort_keys=True, ensure_ascii=False)
        tca.audit(tree, cfg)
        self.assertEqual(cfg_before, json.dumps(cfg, sort_keys=True, ensure_ascii=False))
        self.assertEqual(tree_before, json.dumps(tree, sort_keys=True, ensure_ascii=False))

    # ---------- 真实工作区冒烟（只读，不依赖具体数值） ----------
    def test_real_workspace_smoke(self):
        ws = host_project_root()
        if ws is None:
            self.skipTest("Real project capability audit requires explicit host scope")
        tree_path = ws / ".agentbus" / "task-tree.json"
        cfg_path = ws / ".agentbus" / "config.json"
        if not (tree_path.is_file() and cfg_path.is_file()):
            self.skipTest("未找到真实工作区的 .agentbus 输入，跳过冒烟")
        rep = tca.audit(json.loads(tree_path.read_text(encoding="utf-8")),
                        json.loads(cfg_path.read_text(encoding="utf-8")))
        # 只断言结构，不断言具体数量（项目在演进，数量会变）
        self.assertIn(rep["verdict"], ("clean", "dead_nodes_present"))
        self.assertGreaterEqual(rep["counts"]["nodes_total"], 1)
        self.assertIsInstance(rep["registered_capabilities"], list)
        self.assertIsInstance(rep["dead_nodes"], list)

    # ---------- 渲染与退出码 ----------
    def test_render_mentions_defect_sections(self):
        cfg = _config({"a": {"capabilities": ["python"]}})
        rep = tca.audit(_tree([_leaf("1.1", ["nope"])]), cfg)
        text = tca.render(rep)
        self.assertIn("缺陷一", text)
        self.assertIn("缺陷二", text)
        self.assertIn("dead_nodes_present", text)

    def test_strict_exit_code_is_nonzero_on_dead_nodes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agentbus").mkdir()
            (root / ".agentbus" / "config.json").write_text(
                json.dumps(_config({"a": {"capabilities": ["python"]}})), encoding="utf-8")
            (root / ".agentbus" / "task-tree.json").write_text(
                json.dumps(_tree([_leaf("1.1", ["nope"])])), encoding="utf-8")
            code = tca.main(["--workspace", str(root), "--strict"])
            self.assertEqual(1, code)

    def test_strict_exit_code_is_zero_when_clean(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agentbus").mkdir()
            (root / ".agentbus" / "config.json").write_text(
                json.dumps(_config({"a": {"capabilities": ["python"]}, "b": {"capabilities": ["python"]}})),
                encoding="utf-8")
            (root / ".agentbus" / "task-tree.json").write_text(
                json.dumps(_tree([_leaf("1.1", ["python"])])), encoding="utf-8")
            self.assertEqual(0, tca.main(["--workspace", str(root), "--strict"]))

    def test_missing_inputs_exit_two(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(2, tca.main(["--workspace", tmp]))


if __name__ == "__main__":
    unittest.main()
