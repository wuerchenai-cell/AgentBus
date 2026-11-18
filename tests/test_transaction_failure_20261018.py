# -*- coding: utf-8 -*-
"""PB-AB-RELEASE-TRANSACTION-FAILURE-20261018 — 关键写入失败必须停止提交。

故障注入覆盖：
1. quickstart 入口准备失败：明确报告，不冒充全部完成；
2. 换树前信箱清理：先隔离、失败回滚，树文件与卡片保持一致；
3. 认领转交/截止时间刷新：写、审计或删除失败时回滚，不丢任务、不双领。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from agentbus.cli import cmd_init
from agentbus.config import load, save
from agentbus.project_workflow import WorkflowError
from agentbus.quickstart import run_quickstart
from agentbus.task_tree import TaskForest, TaskTreeError
from agentbus.tree_templates import builtin_templates, import_template


class QuickstartEntryFailureTests(unittest.TestCase):
    def test_failure_is_reported_and_guide_is_not_false_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            with patch("agentbus.project_workflow.prepare_project_workflow",
                       side_effect=WorkflowError("simulated workflow conflict")):
                res = run_quickstart(ws, print_output=False)
            self.assertFalse(res["ok"])
            self.assertIn("simulated workflow conflict", res["workflow_error"])
            self.assertIn("[FAIL]", res["guide_text"])
            self.assertNotIn("零配置初始化全部完成", res["guide_text"])
            # 关键失败只停入口登记；已写入的配置与树保留，可重试恢复。
            self.assertTrue((ws / ".agentbus" / "config.json").is_file())
            self.assertTrue((ws / ".agentbus" / "task-tree.json").is_file())
            self.assertFalse((ws / ".agentbus" / "workflow.json").is_file())

    def test_cli_exit_code_reflects_entry_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(workspace=tmp, output_root=None, quickstart=True,
                                      json=False, coordinator="codex", dry_run=False)
            with patch("agentbus.project_workflow.prepare_project_workflow",
                       side_effect=WorkflowError("simulated conflict")):
                self.assertEqual(cmd_init(args), 1)


class _ForestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load(Path(self.tmp.name), create=True)
        self.cfg.data["workers"].update({
            "alpha": {"enabled": True, "executor_enabled": True, "can_spawn": False, "kind": "gui"},
            "beta": {"enabled": True, "executor_enabled": True, "can_spawn": False, "kind": "gui"},
        })
        self.cfg = save(self.cfg)
        self.forest = TaskForest(self.cfg)
        self.bus = self.forest.bus
        self.bus.init()

    def tree(self):
        structure = {"schema_version": 1,
                     "roots": [{"id": "project", "parent_id": None, "title": "Project",
                                "goal": "Ship actual outputs"}],
                     "branches": [{"id": "feature", "parent_id": "project", "title": "Feature"}],
                     "leaves": [{"id": "first", "parent_id": "feature", "title": "Build first",
                                 "goal": "Produce first output"}]}
        self.forest.replace_structure(structure)
        return structure

    def ready(self, agent):
        return self.bus.agents_dir / agent / "ready"

    def processing(self, agent):
        return self.bus.agents_dir / agent / "processing"

    def expire(self, agent, msg):
        """Expire the first-pull window so another eligible executor may transfer."""
        path = self.ready(agent) / (msg.id + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["deadline_ts"] = 1.0
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


class TreeReplacementTransactionTests(_ForestCase):
    def test_success_quarantines_cards_before_replacing_tree(self):
        self.tree()
        msg = self.bus.make("codex", "beta", "task", title="Old", body="old", result_to="codex")
        self.bus.post(msg)
        before_tree = self.forest.path.read_bytes()
        import_template(self.forest, builtin_templates()[0]["document"], replace=True)
        self.assertNotEqual(before_tree, self.forest.path.read_bytes())
        self.assertFalse((self.ready("beta") / (msg.id + ".json")).exists())
        trash = sorted((self.bus.root / "trash").glob("mailboxes-*"))
        self.assertEqual(1, len(trash))
        self.assertTrue((trash[0] / "beta" / "ready" / (msg.id + ".json")).is_file())
        self.assertIn("mailboxes_quarantined", [event.get("event") for event in self.bus.events()])

    def test_partial_quarantine_failure_restores_cards_and_keeps_tree(self):
        self.tree()
        first = self.bus.make("codex", "alpha", "task", title="A", body="a", result_to="codex")
        second = self.bus.make("codex", "beta", "task", title="B", body="b", result_to="codex")
        self.bus.post(first)
        self.bus.post(second)
        before_tree = self.forest.path.read_bytes()
        real_replace = Path.replace
        calls = {"n": 0}

        def flaky_replace(path_obj, target):
            calls["n"] += 1
            if calls["n"] == 2:
                raise PermissionError(13, "simulated move failure")
            return real_replace(path_obj, target)

        with patch.object(Path, "replace", flaky_replace):
            with self.assertRaises(PermissionError):
                import_template(self.forest, builtin_templates()[0]["document"], replace=True)
        self.assertEqual(before_tree, self.forest.path.read_bytes())
        self.assertTrue((self.ready("alpha") / (first.id + ".json")).is_file())
        self.assertTrue((self.ready("beta") / (second.id + ".json")).is_file())

    def test_tree_write_failure_restores_quarantined_cards(self):
        self.tree()
        msg = self.bus.make("codex", "beta", "task", title="Old", body="old", result_to="codex")
        self.bus.post(msg)
        before_tree = self.forest.path.read_bytes()
        with patch.object(self.forest, "_write", side_effect=OSError("simulated tree write failure")):
            with self.assertRaises(OSError):
                import_template(self.forest, builtin_templates()[0]["document"], replace=True)
        self.assertEqual(before_tree, self.forest.path.read_bytes())
        self.assertTrue((self.ready("beta") / (msg.id + ".json")).is_file())


class ClaimWriteTransactionTests(_ForestCase):
    def test_transfer_write_failure_keeps_single_ready_card(self):
        self.tree()
        msg = self.forest.dispatch_leaf("first", "beta")
        old_path = self.expire("beta", msg)
        new_path = self.ready("alpha") / (msg.id + ".json")
        with patch.object(self.bus, "_write", side_effect=OSError("simulated transfer write failure")):
            with self.assertRaises(OSError):
                self.forest.claim_leaf("first", "alpha")
        self.assertTrue(old_path.is_file())
        self.assertFalse(new_path.exists())
        self.assertFalse((self.processing("alpha") / (msg.id + ".json")).exists())
        self.assertFalse((self.processing("beta") / (msg.id + ".json")).exists())
        claimed = self.forest.claim_leaf("first", "beta")
        self.assertEqual(claimed.id, msg.id)
        self.assertEqual(1, len(list(self.processing("beta").glob("*.json"))))

    def test_transfer_audit_failure_rolls_back_new_card(self):
        self.tree()
        msg = self.forest.dispatch_leaf("first", "beta")
        old_path = self.expire("beta", msg)
        new_path = self.ready("alpha") / (msg.id + ".json")
        real_audit = self.bus.audit

        def flaky_audit(**kw):
            if kw.get("event") == "task_transferred_on_claim":
                raise OSError("simulated audit failure")
            return real_audit(**kw)

        with patch.object(self.bus, "audit", side_effect=flaky_audit):
            with self.assertRaises(OSError):
                self.forest.claim_leaf("first", "alpha")
        self.assertTrue(old_path.is_file())
        self.assertFalse(new_path.exists())
        self.assertFalse((self.processing("alpha") / (msg.id + ".json")).exists())

    def test_transfer_unlink_failure_rolls_back_new_card(self):
        self.tree()
        msg = self.forest.dispatch_leaf("first", "beta")
        old_path = self.expire("beta", msg)
        new_path = self.ready("alpha") / (msg.id + ".json")
        real_unlink = Path.unlink

        def flaky_unlink(path_obj, *args, **kwargs):
            if Path(path_obj) == old_path and not kwargs.get("missing_ok"):
                raise PermissionError(13, "simulated unlink failure")
            return real_unlink(path_obj, *args, **kwargs)

        with patch.object(Path, "unlink", flaky_unlink):
            with self.assertRaises(PermissionError):
                self.forest.claim_leaf("first", "alpha")
        self.assertTrue(old_path.is_file())
        self.assertFalse(new_path.exists())
        self.assertFalse((self.processing("alpha") / (msg.id + ".json")).exists())

    def test_deadline_refresh_write_failure_aborts_claim(self):
        self.tree()
        msg = self.forest.dispatch_leaf("first", "alpha")
        path = self.ready("alpha") / (msg.id + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["deadline_ts"] = 1.0
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        real_write = Path.write_text

        def flaky_write(path_obj, *args, **kwargs):
            if Path(path_obj) == path:
                raise PermissionError(13, "simulated deadline write failure")
            return real_write(path_obj, *args, **kwargs)

        with patch.object(Path, "write_text", flaky_write):
            with self.assertRaises(TaskTreeError):
                self.forest.claim_leaf("first", "alpha")
        self.assertTrue(path.is_file())
        self.assertFalse((self.processing("alpha") / (msg.id + ".json")).exists())
        view = next(node for node in self.forest.snapshot()["leaves"] if node["id"] == "first")
        self.assertEqual("queued", view["state"])


if __name__ == "__main__":
    unittest.main()
