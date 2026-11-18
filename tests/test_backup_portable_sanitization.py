"""§13.10 CX13Q-BACKUP-SANITIZATION —— 备份导出按字段语义清理路径与敏感文本。

本模块只验证**导出/导入边界**（与 §6.11 的备份还原机制互补，不重复其功能）：
1. 机器配置（`path` / `command` / `candidate_command` / `env` / `cwd`）里的
   **Windows / POSIX / UNC 绝对路径**必须替换为重绑定占位符，而不是原样导出；
2. 启动参数里的**合成凭据**（`--token=…`、`--api-key …`、`env` 里的密钥）必须掩码或剔除；
3. 预览里的 `no_machine_paths` / `no_credentials` 必须是**扫描导出成品后算出来的**，
   不能是写死的声明——旧实现正是在有 8 条真实绝对路径的情况下仍声称 True；
4. 自由正文（成员说明、画布便签）无法逐字保证时，如实登记「需人工复核」，
   不作绝对已脱敏保证；**普通中文业务文字不得被误删**；
5. 导入时占位符**绝不覆盖本机绑定**，缺失字段如实报「需人工补齐」；
6. 不读取（更不导出）用户真实凭据内容。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch, Mock
from agentbus import gui_state
from pathlib import Path

from agentbus import backup as bk
from agentbus.backup import (
    BACKUP_FORMAT,
    IMPORT_NOTES_KEY,
    REBIND_PLACEHOLDER,
    apply_backup_restore,
    create_backup_bundle,
    preview_backup_export,
    preview_backup_restore,
)
from agentbus.bus import Bus
from agentbus.config import load, save
from agentbus.gui_state import load_notes, save_notes
from agentbus.task_tree import TaskForest
from agentbus.tree_templates import contains_local_path, scrub_local_paths

# —— 合成样本：绝不使用任何真实凭据或真实用户目录 ——
WIN_PATH = r"C:\Users\synthetic\AppData\Local\Vendor\bin\runner.exe"
WIN_ARG_PATH = r"C:\Program Files\Synthetic Tool\tool.exe"
POSIX_PATH = "/home/synthetic/project/venv/bin/python"
UNC_PATH = r"\\fileserver\share\launchers\agent.exe"
SYNTHETIC_TOKEN = "sk-" + "synthetic-NOT-A-REAL-KEY-0001"
CN_NOTE = "重要设计记录：本周完成弱光前端调试，注意保持中文原样"


def _seed_tree(forest: TaskForest) -> None:
    forest.replace_structure({
        "schema_version": 1,
        "roots": [{"id": "root", "parent_id": None, "title": "根目标", "goal": "完成架构交付"}],
        "branches": [{"id": "b1", "parent_id": "root", "title": "分支1"}],
        "leaves": [{"id": "leaf-1", "parent_id": "b1", "title": "任务1", "goal": "完成任务1"}],
    })


def restore_after_preview(workspace, bundle, **options):
    """Successful restores explicitly approve the exact current target state."""
    if 'preview_id' not in options:
        options['preview_id'] = preview_backup_restore(workspace, bundle, mode=options.get('mode', 'merge'))['preview_id']
    return apply_backup_restore(workspace, bundle, **options)


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state_patch = patch.object(gui_state, 'state_path', return_value=Path(self.temp.name) / 'ui.json')
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.workspace = Path(self.temp.name).resolve()
        self.cfg = load(self.workspace, create=True)
        self.cfg.data["director"] = "codex"
        self.cfg.data["workers"]["codex"].update({
            "enabled": True,
            "executor_enabled": True,
            "path": WIN_PATH,
            "command": [POSIX_PATH, "--workspace", WIN_ARG_PATH, "--api-key=" + SYNTHETIC_TOKEN],
            "candidate_command": [UNC_PATH, "acp"],
            "env": {"PATH": POSIX_PATH, "API_TOKEN": SYNTHETIC_TOKEN, "LANG": "zh_CN.UTF-8"},
            "cwd": UNC_PATH,
            "note": "合成成员：用于验证导出脱敏",
            "base_url": "https://relay.example.invalid",
            "secret_token": SYNTHETIC_TOKEN,
        })
        save(self.cfg)
        Bus(self.workspace / ".agentbus").init()
        _seed_tree(TaskForest(self.cfg))
        save_notes(self.workspace, [{
            "id": "note-1", "x": 1.0, "y": 2.0, "w": 240.0, "h": 180.0,
            "color": "yellow", "text": CN_NOTE, "collapsed": False,
        }])
        self.bundle = create_backup_bundle(self.workspace)
        self.codex = self.bundle["team_settings"]["workers"]["codex"]


class PathScrubbingTests(_Fixture):
    def test_windows_posix_unc_paths_become_rebind_placeholder(self):
        self.assertEqual(REBIND_PLACEHOLDER, self.codex["path"], "字段级绝对路径必须被占位")
        self.assertEqual(REBIND_PLACEHOLDER, self.codex["cwd"])
        self.assertEqual(REBIND_PLACEHOLDER, self.codex["command"][0])
        self.assertEqual(REBIND_PLACEHOLDER, self.codex["command"][2])
        self.assertEqual(REBIND_PLACEHOLDER, self.codex["candidate_command"][0])
        # 非路径参数必须原样保留（只清理该清理的）
        self.assertEqual("--workspace", self.codex["command"][1])
        self.assertEqual("acp", self.codex["candidate_command"][1])

    def test_env_path_values_scrubbed_and_secret_keys_dropped(self):
        self.assertEqual(REBIND_PLACEHOLDER, self.codex["env"]["PATH"])
        self.assertEqual("zh_CN.UTF-8", self.codex["env"]["LANG"], "普通环境变量不得被误删")
        self.assertNotIn("API_TOKEN", self.codex["env"], "env 内凭据键必须剔除")

    def test_secret_arguments_are_masked(self):
        argv = self.codex["command"]
        self.assertEqual("--api-key=***", argv[3], "内联凭据参数必须掩码")
        self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(self.bundle, ensure_ascii=False))

    def test_standalone_secret_flag_masks_following_value(self):
        self.cfg.data["workers"]["codex"]["command"] = ["runner", "--token", SYNTHETIC_TOKEN]
        save(self.cfg)
        codex = create_backup_bundle(self.workspace)["team_settings"]["workers"]["codex"]
        self.assertEqual(["runner", "--token", "***"], codex["command"],
                         "独立开关后的值必须掩码")

    def test_bundle_carries_no_real_absolute_path_of_any_flavor(self):
        raw = json.dumps(self.bundle, ensure_ascii=False)
        for sample in (WIN_PATH, WIN_ARG_PATH, POSIX_PATH, UNC_PATH):
            self.assertNotIn(sample, raw, "导出包不得包含合成绝对路径：%s" % sample)

    def test_spaced_windows_path_leaves_no_residue(self):
        """带空格的路径必须**整条**替换，不能只清到第一个空格。"""
        raw = json.dumps(self.bundle, ensure_ascii=False)
        for residue in (" Files\\Synthetic", " Program Files", " Tool\\tool.exe"):
            self.assertNotIn(residue, raw, "带空格路径清理后残留：%s" % residue)
        self.assertEqual(REBIND_PLACEHOLDER, self.codex["env"]["PATH"])
        self.assertNotIn(" Files", json.dumps(self.codex, ensure_ascii=False))

    def test_key_level_secret_and_desktop_icon_are_excluded(self):
        self.assertNotIn("secret_token", self.codex)
        self.assertNotIn("desktop_icon_path", self.codex)

    def test_missing_adapter_note_is_kept_for_importer(self):
        self.assertIn(IMPORT_NOTES_KEY, self.codex)
        self.assertIn(REBIND_PLACEHOLDER, self.codex[IMPORT_NOTES_KEY])


class PreviewHonestyTests(_Fixture):
    def test_preview_flags_are_computed_from_product_scan(self):
        prev = preview_backup_export(self.workspace)
        self.assertTrue(prev["sanitized"])
        self.assertTrue(prev["no_machine_paths"], "清理后成品不应残留本机路径")
        self.assertTrue(prev["no_credentials"])
        scope = prev["sanitization"]
        self.assertEqual(0, scope["verified_of_product"]["machine_paths_remaining"],
                         "结论必须来自对成品的复检")
        self.assertEqual(0, scope["verified_of_product"]["credential_fields_remaining"])
        self.assertGreater(scope["machine_paths_removed"], 0, "应如实报告清理了多少条路径")

    def test_free_text_is_reviewed_not_absolutely_guaranteed(self):
        scope = preview_backup_export(self.workspace)["sanitization"]
        self.assertIn("复核", scope["unverified_note"])
        self.assertEqual("machine_config_verified; free_text_unverified", scope["scope"])
        self.assertTrue(scope["review_items"], "成员说明/服务端点应列入需复核项")

    def test_service_endpoint_is_reviewed_not_silently_shipped(self):
        reasons = {item["field"]: item["reason"] for item in
                   preview_backup_export(self.workspace)["sanitization"]["review_items"]}
        self.assertIn("workers.codex.base_url", reasons)
        self.assertIn("确认", reasons["workers.codex.base_url"])

    def test_legacy_hardcoded_flags_would_have_lied(self):
        """守住根因：若把导出成品扫一遍，旧实现必然暴露剩余路径。"""
        broken = json.loads(json.dumps(self.bundle))
        broken["team_settings"]["workers"]["codex"]["path"] = WIN_PATH
        audit = bk._audit_export(broken)
        self.assertEqual(1, audit["machine_paths_remaining"])
        self.assertTrue(contains_local_path(WIN_PATH), "权威判定器必须认得盘符路径")


class ChineseTextPreservationTests(_Fixture):
    def test_ordinary_chinese_note_text_is_not_touched(self):
        notes = self.bundle["ui_preferences"]["sticky_notes"]
        self.assertEqual([CN_NOTE], [n["text"] for n in notes])

    def test_note_with_local_path_is_scrubbed_but_prose_kept(self):
        save_notes(self.workspace, [{
            "id": "note-2", "x": 1.0, "y": 2.0, "w": 240.0, "h": 180.0,
            "color": "blue", "text": "日志在 " + WIN_PATH + " 下面，请复核中文段落",
            "collapsed": False,
        }])
        notes = create_backup_bundle(self.workspace)["ui_preferences"]["sticky_notes"]
        text = notes[0]["text"]
        self.assertNotIn(WIN_PATH, text)
        self.assertIn(REBIND_PLACEHOLDER, text)
        self.assertIn("请复核中文段落", text, "路径被清理不得吞掉中文正文")

    def test_scrub_helper_leaves_http_routes_alone(self):
        text = "接口: https://relay.example.invalid/v1/chat"
        scrubbed, hits = scrub_local_paths(text, REBIND_PLACEHOLDER)
        self.assertEqual(0, hits, "显式 HTTP 路由不算本机路径")
        self.assertEqual(text, scrubbed)


class CredentialIsolationTests(_Fixture):
    def test_credential_file_content_is_never_exported(self):
        marker = "SYNTHETIC-CREDENTIAL-MARKER-DO-NOT-LEAK"
        cred = self.workspace / "synthetic_credentials.json"
        cred.write_text(json.dumps({"api_key": marker}), encoding="utf-8")
        self.cfg.data["workers"]["codex"]["token_source"] = str(cred)
        save(self.cfg)
        bundle = create_backup_bundle(self.workspace)
        raw = json.dumps(bundle, ensure_ascii=False)
        self.assertNotIn(marker, raw, "凭据文件内容绝不进入备份包")
        self.assertNotIn("token_source", bundle["team_settings"]["workers"]["codex"])


class RestoreRebindingTests(_Fixture):
    def test_import_keeps_local_binding_and_never_writes_placeholder(self):
        with tempfile.TemporaryDirectory() as other:
            target = Path(other).resolve()
            tcfg = load(target, create=True)
            local_command = [r"D:\local\bin\runner.exe", "serve"]
            tcfg.data["workers"]["codex"].update({
                "enabled": True, "executor_enabled": True,
                "path": r"D:\local\bin\runner.exe", "command": local_command,
            })
            save(tcfg)
            Bus(target / ".agentbus").init()

            plan = preview_backup_restore(target, self.bundle)["rebind_workers"]
            self.assertIn("path", plan["codex"]["kept_local"])
            self.assertIn("command", plan["codex"]["kept_local"])
            self.assertNotIn("path", plan["codex"]["needs_manual_binding"],
                             "本机已有该字段时不得报「需人工补齐」")
            self.assertNotIn(IMPORT_NOTES_KEY, plan["codex"]["kept_local"])
            self.assertNotIn(IMPORT_NOTES_KEY, plan["codex"]["needs_manual_binding"])

            result = restore_after_preview(target, self.bundle)
            self.assertTrue(result["restored"])
            restored = load(target).data["workers"]["codex"]
            self.assertEqual(r"D:\local\bin\runner.exe", restored["path"],
                             "占位符绝不能覆盖本机 launcher 路径")
            self.assertEqual(local_command, restored["command"])
            raw = json.dumps(load(target).data, ensure_ascii=False)
            self.assertNotIn(REBIND_PLACEHOLDER, raw, "本机配置不得写入占位符")
            self.assertNotIn(IMPORT_NOTES_KEY, raw, "导出说明键不得污染本机配置")

    def test_restore_reports_field_needing_manual_binding(self):
        with tempfile.TemporaryDirectory() as other:
            target = Path(other).resolve()
            tcfg = load(target, create=True)
            # 本机刻意没有 codex.path → 该字段必须报「需人工补齐」
            tcfg.data["workers"]["codex"].pop("path", None)
            save(tcfg)
            Bus(target / ".agentbus").init()
            plan = preview_backup_restore(target, self.bundle)["rebind_workers"]
            self.assertIn("path", plan["codex"]["needs_manual_binding"],
                          "本机没有该字段时必须报「需人工补齐」")
            self.assertNotIn("path", plan["codex"]["kept_local"])
            self.assertNotIn(IMPORT_NOTES_KEY, plan["codex"]["needs_manual_binding"],
                             "说明键不是待绑定字段")

    def test_dict_valued_placeholder_never_reaches_local_config(self):
        """`env` 这类字典字段：内部含占位符时同样不得写回本机（递归判定）。"""
        self.assertTrue(bk._carries_placeholder({"PATH": REBIND_PLACEHOLDER}))
        self.assertFalse(bk._carries_placeholder({"LANG": "zh_CN.UTF-8"}))
        with tempfile.TemporaryDirectory() as other:
            target = Path(other).resolve()
            tcfg = load(target, create=True)
            tcfg.data["workers"]["codex"].pop("env", None)
            save(tcfg)
            Bus(target / ".agentbus").init()
            plan = preview_backup_restore(target, self.bundle)["rebind_workers"]
            self.assertIn("env", plan["codex"]["needs_manual_binding"])
            restore_after_preview(target, self.bundle)
            raw = json.dumps(load(target).data, ensure_ascii=False)
            self.assertNotIn(REBIND_PLACEHOLDER, raw)

    def test_restore_is_idempotent_and_content_stable(self):
        with tempfile.TemporaryDirectory() as other:
            target = Path(other).resolve()
            load(target, create=True)
            Bus(target / ".agentbus").init()
            first = restore_after_preview(target, self.bundle)
            second = restore_after_preview(target, self.bundle)
            self.assertEqual(first["leaves_count"], second["leaves_count"])
            self.assertEqual(len(load_notes(target)), 1)


class DeterminismTests(_Fixture):
    def test_export_is_deterministic(self):
        a = create_backup_bundle(self.workspace)
        b = create_backup_bundle(self.workspace)
        self.assertEqual(a["bundle_sha256"], b["bundle_sha256"])
        self.assertEqual(a["sanitization"], b["sanitization"])
        self.assertEqual(BACKUP_FORMAT, a["format"])


if __name__ == "__main__":
    unittest.main()
