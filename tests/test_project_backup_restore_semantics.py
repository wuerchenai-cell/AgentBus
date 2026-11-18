"""§13.7 CX13Q-RESTORE-SEMANTICS —— 还原模式与节点偏好映射真实且幂等。

与 §6.11 的既有备份/还原功能测试互补：本模块只验证**还原语义**——
1. 预览必须给出**真实最终集合**（旧实现预览 3→2，实际追加成 5）；
2. 三种模式（导入副本 / 合并 / 替换恢复）语义明确，替换属危险操作且需显式确认；
3. 同一包 + 同一目标**重复应用不新增重复节点**（幂等）；
4. 折叠/优先级与任务树**同一 id 映射**，还原后必须解析到真实节点；
5. 团队成员是**合并**语义：本机独有成员保留，预览不得谎称「删除」；
6. 明确声明这是拓扑/团队/偏好备份，**不是**含信箱与产物的完整灾备。
"""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch, Mock
from agentbus import gui_state
from pathlib import Path

from agentbus.backup import (
    BACKUP_SCOPE_NOTE,
    DEFAULT_RESTORE_MODE,
    RESTORE_MODES,
    apply_backup_restore,
    create_backup_bundle,
    preview_backup_restore,
)
from agentbus.bus import Bus
from agentbus.config import load, save
from agentbus.gui_state import load_branch_folds, load_notes, load_priorities, save_branch_folds, save_priorities
from agentbus.task_tree import TaskForest

SOURCE_TREE = {
    "schema_version": 1,
    "roots": [{"id": "root", "parent_id": None, "title": "根目标", "goal": "完成交付"}],
    "branches": [{"id": "b1", "parent_id": "root", "title": "分支1"}],
    "leaves": [
        {"id": "leaf-1", "parent_id": "b1", "title": "任务1", "goal": "完成任务1"},
        {"id": "leaf-2", "parent_id": "b1", "title": "任务2", "goal": "完成任务2",
         "dependencies": ["leaf-1"]},
    ],
}

TARGET_TREE = {
    "schema_version": 1,
    "roots": [{"id": "troot", "parent_id": None, "title": "本机根", "goal": "本机目标"}],
    "branches": [{"id": "tb1", "parent_id": "troot", "title": "本机分支"}],
    "leaves": [
        {"id": "t-leaf-1", "parent_id": "tb1", "title": "本机任务1", "goal": "本机1"},
        {"id": "t-leaf-2", "parent_id": "tb1", "title": "本机任务2", "goal": "本机2"},
        {"id": "t-leaf-3", "parent_id": "tb1", "title": "本机任务3", "goal": "本机3"},
    ],
}


def _make_workspace(path: Path, tree=None, with_local_member=False):
    cfg = load(path, create=True)
    cfg.data["director"] = "codex"
    cfg.data["workers"]["codex"].update({"enabled": True, "executor_enabled": True})
    if with_local_member:
        cfg.data["workers"]["trae"].update({"enabled": True, "executor_enabled": True})
    save(cfg)
    Bus(path / ".agentbus").init()
    if tree is not None:
        TaskForest(cfg).replace_structure(json.loads(json.dumps(tree)))
    return cfg


def restore_after_preview(workspace, bundle, **options):
    """Successful restores explicitly approve the exact current target state."""
    if 'preview_id' not in options:
        options['preview_id'] = preview_backup_restore(workspace, bundle, mode=options.get('mode', 'merge'))['preview_id']
    return apply_backup_restore(workspace, bundle, **options)


class RestoreSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state_patch = patch.object(gui_state, 'state_path', return_value=Path(self.tmp.name) / 'ui.json')
        state_patch.start()
        self.addCleanup(state_patch.stop)
        base = Path(self.tmp.name)
        self.source = base / "source"
        self.source.mkdir()
        _make_workspace(self.source, SOURCE_TREE)
        forest = TaskForest(load(self.source))
        save_branch_folds(self.source, ["b1"])
        save_priorities(self.source, {"leaf-1": "P0", "leaf-2": "P1"})
        self.assertEqual(2, len(forest.snapshot()["leaves"]))
        self.bundle = create_backup_bundle(self.source)

    def _target(self, name, tree=None, with_local_member=False):
        path = Path(self.tmp.name) / name
        path.mkdir()
        _make_workspace(path, tree, with_local_member)
        return path

    # ---------- 1. 预览必须给出真实最终集合 ----------

    def test_preview_reports_real_final_set_for_merge(self):
        target = self._target("t1", TARGET_TREE)
        preview = preview_backup_restore(target, self.bundle, mode="merge")
        plan = preview["plan"]
        self.assertEqual("merge", plan["mode"])
        self.assertEqual(3, plan["current_leaves"])
        # 模板共 1 root + 1 branch + 2 leaves = 4 个节点，且与本机节点 id 不冲突
        self.assertEqual(4, plan["template_nodes"])
        self.assertEqual(4, plan["added_nodes"])
        self.assertEqual(5, plan["current_nodes"], "本机 1 根 + 1 枝 + 3 叶 = 5 个节点")
        self.assertEqual(5, plan["final_leaves"], "3 个本机 + 2 个模板 = 5")
        self.assertEqual(9, plan["final_nodes"], "5 本机 + 4 模板 = 9 个节点")
        self.assertIn("恢复后 5 个任务", preview["diff_summary"]["tree_leaves_change"])

    def test_apply_matches_the_preview_final_set(self):
        """预览说 5，实际就必须是 5（旧实现在此变成 5 而预览写 2）。"""
        target = self._target("t2", TARGET_TREE)
        preview = preview_backup_restore(target, self.bundle, mode="merge")
        result = restore_after_preview(target, self.bundle, mode="merge",
                                      preview_id=preview["preview_id"])
        actual = len(TaskForest(load(target)).snapshot()["leaves"])
        self.assertEqual(preview["plan"]["final_leaves"], actual)
        self.assertEqual(5, actual)
        self.assertEqual(actual, result["leaves_count"], "回报的叶子数必须是真实结果")

    # ---------- 2. 幂等 ----------

    def test_repeated_apply_does_not_duplicate_nodes(self):
        target = self._target("t3", TARGET_TREE)
        preview = preview_backup_restore(target, self.bundle, mode="merge")
        first = restore_after_preview(target, self.bundle, mode="merge",
                                     preview_id=preview["preview_id"])
        second = restore_after_preview(target, self.bundle, mode="merge")
        third = restore_after_preview(target, self.bundle, mode="merge")
        self.assertEqual(first["leaves_count"], second["leaves_count"])
        self.assertEqual(first["leaves_count"], third["leaves_count"])
        self.assertEqual(5, len(TaskForest(load(target)).snapshot()["leaves"]),
                         "重复应用不得新增重复节点（旧实现 2→4 持续膨胀）")
        after = preview_backup_restore(target, self.bundle, mode="merge")
        self.assertTrue(after["plan"]["already_applied"], "第二次起应识别为已应用")
        self.assertEqual(after["plan"]["final_leaves"], after["plan"]["current_leaves"])

    def test_different_bundles_do_not_collide(self):
        target = self._target("t4", TARGET_TREE)
        other_source = Path(self.tmp.name) / "other"
        other_source.mkdir()
        _make_workspace(other_source, SOURCE_TREE)
        save_priorities(other_source, {"leaf-2": "P2"})
        other_bundle = create_backup_bundle(other_source)
        self.assertNotEqual(self.bundle["bundle_sha256"], other_bundle["bundle_sha256"])
        restore_after_preview(target, self.bundle, mode="merge")
        restore_after_preview(target, other_bundle, mode="merge")
        self.assertEqual(7, len(TaskForest(load(target)).snapshot()["leaves"]),
                         "不同包应各自并入（3 本机 + 2 + 2），互不覆盖")

    # ---------- 3. 替换恢复：危险 + 显式确认 + 保护在途 ----------

    def test_replace_requires_explicit_confirmation(self):
        target = self._target("t5", TARGET_TREE)
        with self.assertRaises(ValueError) as ctx:
            restore_after_preview(target, self.bundle, mode="replace")
        self.assertIn("confirm_replace", str(ctx.exception))

    def test_replace_refused_when_target_already_has_task_bindings(self):
        """§6.11 的 replace_structure 护栏：有任务绑定就不许丢弃 → 我们提前诚实拒绝。"""
        target = self._target("t6", TARGET_TREE)
        forest = TaskForest(load(target))
        message = forest.claim_leaf("t-leaf-1", "codex")
        self.assertTrue(message.id)
        preview = preview_backup_restore(target, self.bundle, mode="replace")
        self.assertGreater(preview["plan"]["bound_leaves"], 0)
        self.assertTrue(preview["replace_blocked_by_bindings"])
        with self.assertRaises(ValueError) as ctx:
            restore_after_preview(target, self.bundle, mode="replace", confirm_replace=True)
        # 该叶子同时「在途」与「有任务绑定」，两条护栏都可先行拒绝；只要求确实被拒
        self.assertIn("拒绝替换恢复", str(ctx.exception))
        self.assertEqual(3, len(TaskForest(load(target)).snapshot()["leaves"]),
                         "被拒绝时不得有任何写入")

    def test_replace_on_empty_target_drops_local_only_nodes(self):
        target = self._target("t7")            # 空目标：无节点、无绑定
        preview = preview_backup_restore(target, self.bundle, mode="replace")
        self.assertEqual(2, preview["plan"]["final_leaves"])
        self.assertTrue(preview["requires_replace_confirmation"])
        result = restore_after_preview(target, self.bundle, mode="replace",
                                      confirm_replace=True,
                                      preview_id=preview["preview_id"])
        self.assertEqual(2, result["leaves_count"])
        self.assertEqual(2, len(TaskForest(load(target)).snapshot()["leaves"]))

    def test_unknown_mode_is_rejected(self):
        target = self._target("t8")
        with self.assertRaises(ValueError):
            preview_backup_restore(target, self.bundle, mode="explode")
        with self.assertRaises(ValueError):
            restore_after_preview(target, self.bundle, mode="explode")
        self.assertEqual(("append", "merge", "replace"), RESTORE_MODES)
        self.assertEqual("merge", DEFAULT_RESTORE_MODE)

    # ---------- 4. 折叠/优先级必须解析到真实节点 ----------

    def test_folds_and_priorities_resolve_to_real_nodes_after_restore(self):
        target = self._target("t9", TARGET_TREE)
        restore_after_preview(target, self.bundle, mode="merge")
        forest = TaskForest(load(target))
        snap = forest.snapshot()
        live = {n["id"] for kind in ("roots", "branches", "leaves") for n in snap.get(kind, [])}

        priorities = load_priorities(target)
        self.assertEqual({"P0", "P1"}, set(priorities.values()))
        self.assertTrue(set(priorities).issubset(live),
                        "优先级键必须解析到真实节点，未解析：%s"
                        % sorted(set(priorities) - live))

        folds = load_branch_folds(target)
        self.assertEqual(1, len(folds), "折叠分支应被保留且唯一")
        self.assertTrue(set(folds).issubset(live),
                        "折叠键必须解析到真实分支，未解析：%s" % sorted(set(folds) - live))
        # 折叠必须落在备份带来的那个分支上，而不是本机既有分支
        imported = live - {"troot", "tb1", "t-leaf-1", "t-leaf-2", "t-leaf-3"}
        self.assertTrue(set(folds).issubset(imported))

    def test_export_records_mapping_summary_and_drops_unresolvable_entries(self):
        save_priorities(self.source, {"leaf-1": "P0", "ghost-leaf": "P2"})
        save_branch_folds(self.source, ["b1", "ghost-branch"])
        bundle = create_backup_bundle(self.source)
        mapping = bundle["metadata"]["preference_mapping"]
        self.assertEqual(1, mapping["priorities_mapped"])
        self.assertEqual(1, mapping["folds_mapped"])
        self.assertIn("card_priorities:ghost-leaf", mapping["dropped"])
        self.assertIn("collapsed_branches:ghost-branch", mapping["dropped"])

    # ---------- 5. 成员是合并语义 ----------

    def test_member_merge_policy_keeps_local_members(self):
        # 构造一个「不含 trae」的包（模拟来自没有该成员的机器），
        # 从而在目标侧形成真正的「本机独有成员」。
        bundle = json.loads(json.dumps(self.bundle))
        bundle["team_settings"]["workers"].pop("trae", None)
        self.assertNotIn("trae", bundle["team_settings"]["workers"])

        target = self._target("t10", TARGET_TREE, with_local_member=True)
        bundle.pop("bundle_sha256", None)
        bundle["bundle_sha256"] = hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        preview = preview_backup_restore(target, bundle, mode="merge")
        diff = preview["diff_summary"]
        self.assertIn("trae", diff["worker_members_diff"]["kept_local_only"])
        self.assertNotIn("trae", diff["worker_members_diff"]["added"])
        self.assertIn("trae", diff["final_worker_members"],
                      "本机独有成员必须出现在最终集合里")
        self.assertIn("合并", diff["worker_members_diff"]["policy"])
        restore_after_preview(target, bundle, mode="merge")
        self.assertIn("trae", load(target).data["workers"],
                      "还原后本机独有成员必须仍在（不得被删除）")

    def test_scope_note_disclaims_full_disaster_recovery(self):
        preview = preview_backup_restore(self._target("t11"), self.bundle)
        self.assertIn("不是完整灾备", preview["scope_note"])
        self.assertEqual(BACKUP_SCOPE_NOTE, preview["scope_note"])
        self.assertIn("不含信箱", self.bundle["metadata"]["scope_note"])


if __name__ == "__main__":
    unittest.main()
