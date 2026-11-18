# -*- coding: utf-8 -*-
"""WB-SKILL 统一技能中心定向回归（无模型，全部走隔离临时目录）。

来源：WorkBuddy，2026-10-08；任务 m-1791457584489-f05b6b。
运行：C:/Python314/python.exe -B -X utf8 -m unittest tests.test_skill_center -v
（在 authors/workbuddy 目录下运行）

覆盖要求：重复部署幂等、更新可预览、用户改动不得被静默覆盖、回滚只撤销 AgentBus 管理的文件、
一个宿主失败不污染其他宿主、发现≠调用、项目级与全局级、未知宿主 generic；
反例包含中文/空格路径、坏 ZIP、路径穿越、链接、错摘要、部分失败与恢复。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE))

from agentbus import host_skill_adapters as adapters          # noqa: E402
from agentbus import skill_deployer as deployer                # noqa: E402
from agentbus import skill_registry as registry                # noqa: E402
from agentbus.skill_install import SkillInstallError, _sha256  # noqa: E402

SKILL_MD_V1 = "---\nname: demo-skill\nversion: 1\n---\n# Demo skill\n第一版内容\n"
SKILL_MD_V2 = "---\nname: demo-skill\nversion: 2\n---\n# Demo skill\n第二版内容\n"


class SkillCenterCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wb-skill-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "项目 目录"            # 中文 + 空格
        self.project.mkdir()
        self.global_root = self.root / "global public"
        self.global_root.mkdir()
        self.state = self.root / "state"
        self.source = self.root / "src" / "demo-skill"
        self.source.mkdir(parents=True)
        (self.source / "SKILL.md").write_text(SKILL_MD_V1, encoding="utf-8")
        (self.source / "refs").mkdir()
        (self.source / "refs" / "notes.md").write_text("说明\n", encoding="utf-8")

    def write_source_v2(self):
        (self.source / "SKILL.md").write_text(SKILL_MD_V2, encoding="utf-8")

    def target_dir(self, host="workbuddy", scope=adapters.SCOPE_PROJECT):
        return Path(adapters.resolve_target(
            host, skill_name="demo-skill", scope=scope,
            project_root=self.project, global_root=self.global_root)["target_dir"])

    def deploy(self, host="workbuddy", scope=adapters.SCOPE_PROJECT, confirm=True, **kw):
        return deployer.deploy(host, source_dir=self.source, confirm=confirm,
                               project_root=self.project, global_root=self.global_root,
                               scope=scope, state_root=self.state, deployed_at=1.0, **kw)

    def plan(self, host="workbuddy", scope=adapters.SCOPE_PROJECT, **kw):
        return deployer.plan(host, source_dir=self.source, project_root=self.project,
                             global_root=self.global_root, scope=scope,
                             state_root=self.state, **kw)


class DeployLifecycleTests(SkillCenterCase):
    def test_plan_writes_nothing(self):
        plan = self.plan()
        self.assertEqual(plan["action"], "create")
        self.assertFalse(plan["write_permitted"])
        self.assertFalse(self.target_dir().exists())
        self.assertFalse(plan["native_discovery_claimed"])

    def test_deploy_requires_explicit_confirmation(self):
        result = self.deploy(confirm=False)
        self.assertFalse(result["deployed"])
        self.assertIn("CONFIRMATION_REQUIRED", result["reason_codes"])
        self.assertFalse(self.target_dir().exists())

    def test_deploy_writes_skill_md_and_nested_reference(self):
        result = self.deploy()
        self.assertTrue(result["deployed"], result)
        self.assertTrue(result["verified_after_write"])
        target = self.target_dir()
        self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), SKILL_MD_V1)
        self.assertEqual((target / "refs" / "notes.md").read_text(encoding="utf-8"), "说明\n")

    def test_second_deploy_is_idempotent_and_writes_nothing(self):
        self.deploy()
        before = {p.name: p.read_bytes() for p in self.target_dir().rglob("*") if p.is_file()}
        second = self.deploy()
        self.assertFalse(second["deployed"])
        self.assertEqual(second["action"], "unchanged")
        after = {p.name: p.read_bytes() for p in self.target_dir().rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_update_is_previewable_before_writing(self):
        self.deploy()
        self.write_source_v2()
        plan = self.plan()
        self.assertEqual(plan["action"], "update")
        self.assertIn("UPDATE_AVAILABLE", plan["reason_codes"])
        self.assertNotEqual(plan["source_digest"], plan["target_digest"])
        self.assertEqual((self.target_dir() / "SKILL.md").read_text(encoding="utf-8"), SKILL_MD_V1)
        result = self.deploy()
        self.assertTrue(result["deployed"])
        self.assertEqual((self.target_dir() / "SKILL.md").read_text(encoding="utf-8"), SKILL_MD_V2)

    def test_user_modification_blocks_silent_overwrite(self):
        self.deploy()
        self.write_source_v2()
        (self.target_dir() / "SKILL.md").write_text("用户手改的内容\n", encoding="utf-8")
        plan = self.plan()
        self.assertEqual(plan["action"], "conflict")
        self.assertIn("TARGET_MODIFIED", plan["reason_codes"])
        result = self.deploy()
        self.assertFalse(result["deployed"])
        self.assertEqual((self.target_dir() / "SKILL.md").read_text(encoding="utf-8"),
                         "用户手改的内容\n")

    def test_unreadable_target_is_conflict_not_crash(self):
        target = self.target_dir()
        target.mkdir(parents=True)
        (target / "SKILL.md").mkdir()                      # 目录占位，不是技能包
        plan = self.plan()
        self.assertEqual(plan["action"], "conflict")
        self.assertIn("TARGET_UNREADABLE", plan["reason_codes"])

    def test_partial_failure_rolls_back_what_we_wrote(self):
        from agentbus import skill_install
        real = skill_install._atomic_write
        calls = {"n": 0}

        def flaky(path, data):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("模拟第二条写入失败")
            return real(path, data)

        with patch.object(skill_install, "_atomic_write", flaky):
            result = self.deploy()
        self.assertFalse(result["deployed"])
        self.assertIn("TRANSACTION_FAILED", result["reason_codes"])
        self.assertFalse((self.target_dir() / "SKILL.md").exists())


class RollbackTests(SkillCenterCase):
    def test_rollback_removes_our_files_and_keeps_foreign_files(self):
        self.deploy()
        target = self.target_dir()
        (target / "user-extra.md").write_text("用户自己放的文件\n", encoding="utf-8")
        result = deployer.rollback("workbuddy", source_dir=self.source, confirm=True,
                                   project_root=self.project, state_root=self.state)
        self.assertTrue(result["rolled_back"])
        self.assertIn("SKILL.md", result["removed"])
        self.assertTrue((target / "user-extra.md").is_file())
        self.assertFalse((target / "SKILL.md").exists())

    def test_rollback_restores_previous_version(self):
        self.deploy()
        self.write_source_v2()
        self.deploy()
        self.assertEqual((self.target_dir() / "SKILL.md").read_text(encoding="utf-8"), SKILL_MD_V2)
        result = deployer.rollback("workbuddy", source_dir=self.source, confirm=True,
                                   project_root=self.project, state_root=self.state)
        self.assertTrue(result["rolled_back"])
        self.assertIn("SKILL.md", result["restored"])
        self.assertEqual((self.target_dir() / "SKILL.md").read_text(encoding="utf-8"), SKILL_MD_V1)

    def test_rollback_skips_files_the_user_modified_after_deploy(self):
        self.deploy()
        (self.target_dir() / "SKILL.md").write_text("用户改过\n", encoding="utf-8")
        result = deployer.rollback("workbuddy", source_dir=self.source, confirm=True,
                                   project_root=self.project, state_root=self.state)
        self.assertTrue(result["rolled_back"])
        self.assertEqual([item["reason"] for item in result["skipped"]], ["USER_MODIFIED_SKIPPED"])
        self.assertEqual((self.target_dir() / "SKILL.md").read_text(encoding="utf-8"), "用户改过\n")

    def test_rollback_without_record_reports_missing(self):
        result = deployer.rollback("workbuddy", source_dir=self.source, confirm=True,
                                   project_root=self.project, state_root=self.state)
        self.assertFalse(result["rolled_back"])
        self.assertIn("NO_MANAGED_RECORD", result["reason_codes"])

    def test_rollback_requires_confirmation(self):
        self.deploy()
        result = deployer.rollback("workbuddy", source_dir=self.source, confirm=False,
                                   project_root=self.project, state_root=self.state)
        self.assertFalse(result["rolled_back"])
        self.assertIn("CONFIRMATION_REQUIRED", result["reason_codes"])


class MultiHostTests(SkillCenterCase):
    def test_deploy_many_covers_known_hosts_and_isolates_failure(self):
        items = [{"host": host, "source_dir": self.source, "project_root": self.project}
                 for host in ("codex", "workbuddy", "trae", "opencode")]
        items.append({"host": "custom-agent", "source_dir": self.source,
                      "project_root": self.project})
        bad_root = self.root / "missing-root"
        items.append({"host": "trae", "source_dir": self.source, "project_root": bad_root})
        report = deployer.deploy_many(items, confirm=True, state_root=self.state, deployed_at=1.0)
        self.assertEqual(report["hosts_total"], 6)
        self.assertEqual(report["deployed"], 5)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["isolated_failures"], ["trae"])
        self.assertTrue((self.project / ".agentbus" / "skills" / "demo-skill" / "SKILL.md").is_file())
        self.assertTrue((self.project / ".codebuddy" / "skills" / "demo-skill" / "SKILL.md").is_file())

    def test_same_source_deploys_to_two_hosts_independently(self):
        first = self.deploy(host="workbuddy")
        second = self.deploy(host="opencode")
        self.assertTrue(first["deployed"] and second["deployed"])
        self.assertNotEqual(first["target_dir"], second["target_dir"])
        self.assertTrue((self.target_dir("workbuddy") / "SKILL.md").is_file())
        self.assertTrue((self.target_dir("opencode") / "SKILL.md").is_file())


class AdapterTests(unittest.TestCase):
    def test_known_hosts_are_listed_with_public_paths(self):
        hosts = adapters.list_hosts()
        for host in ("antigravity", "codex", "opencode", "trae", "workbuddy", "zcode"):
            self.assertIn(host, hosts)
        paths = adapters.host_public_paths()
        self.assertEqual(paths["codex"], ".agentbus/workflows/codex")
        self.assertIsNone(paths["antigravity"])

    def test_unknown_host_falls_back_to_generic(self):
        adapter = adapters.adapter_for("brand-new-host")
        self.assertTrue(adapter["used_generic"])
        self.assertEqual(adapter["adapter"], "generic")
        self.assertIn("UNKNOWN_HOST_GENERIC", adapter["reason_codes"])
        self.assertFalse(adapter["private_probe"])

    def test_antigravity_has_no_public_path_and_uses_generic(self):
        adapter = adapters.adapter_for("antigravity")
        self.assertTrue(adapter["used_generic"])
        self.assertIn("NO_PUBLIC_PATH_GENERIC", adapter["reason_codes"])
        self.assertFalse(adapter["documented"])

    def test_invalid_host_id_is_reported_not_guessed(self):
        for bad in ("", "Bad Host", "../escape", None, 7):
            with self.subTest(host=bad):
                adapter = adapters.adapter_for(bad)
                self.assertIn("INVALID_HOST_ID", adapter["reason_codes"])
                self.assertTrue(adapter["used_generic"])

    def test_scope_requires_explicit_root_and_never_probes(self):
        with self.assertRaises(ValueError):
            adapters.resolve_target("workbuddy", skill_name="demo-skill",
                                    scope=adapters.SCOPE_PROJECT)
        with self.assertRaises(ValueError):
            adapters.resolve_target("workbuddy", skill_name="demo-skill",
                                    scope=adapters.SCOPE_GLOBAL)
        with self.assertRaises(ValueError):
            adapters.resolve_target("workbuddy", skill_name="demo-skill", scope="machine")

    def test_global_scope_never_defaults_to_a_probed_root(self):
        # 全局级必须由调用方显式给根；不给根不猜、不探测。
        for host in ("workbuddy", "trae", "codex"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError) as caught:
                    adapters.resolve_target(host, skill_name="demo-skill",
                                            scope=adapters.SCOPE_GLOBAL)
                self.assertIn("INVALID_ROOT", str(caught.exception))

    def test_invalid_skill_name_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="wb-adapter-") as temp:
            root = Path(temp)
            for bad in ("../evil", "a/b", "", "con", "x" * 80):
                with self.subTest(name=bad):
                    with self.assertRaises(ValueError):
                        adapters.resolve_target("codex", skill_name=bad,
                                                scope=adapters.SCOPE_PROJECT,
                                                project_root=root)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wb-registry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "src" / "demo-skill"
        self.source.mkdir(parents=True)
        (self.source / "SKILL.md").write_text(SKILL_MD_V1, encoding="utf-8")
        self.registry = registry.SkillRegistry()

    def directory_source(self, name="demo-skill"):
        return registry.normalize_source("directory", name=name, location=self.source,
                                         allowed_root=self.root)

    def test_duplicate_identical_source_is_idempotent(self):
        first = self.registry.add(self.directory_source())
        second = self.registry.add(self.directory_source())
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual(len(self.registry.list()), 1)

    def test_same_name_different_source_is_a_conflict(self):
        self.registry.add(self.directory_source())
        other = self.root / "elsewhere" / "demo-skill"
        other.mkdir(parents=True)
        record = registry.normalize_source("directory", name="demo-skill", location=other,
                                           allowed_root=self.root)
        with self.assertRaises(registry.SkillRegistryError) as caught:
            self.registry.add(record)
        self.assertIn("DUPLICATE_CONFLICT", str(caught.exception))

    def test_git_source_is_pinned_only_with_commit_and_never_verified(self):
        record = registry.normalize_source(
            "git", name="remote-skill", location="https://example.com/agentbus/skills.git",
            ref="main", allowed_root=self.root)
        self.assertFalse(record["pinned"])
        self.assertFalse(record["remote_verified"])
        pinned = registry.normalize_source(
            "git", name="remote-skill", location="https://example.com/agentbus/skills.git",
            ref="main", commit="a" * 40, allowed_root=self.root)
        self.assertTrue(pinned["pinned"])
        with self.assertRaises(registry.SkillRegistryError):
            registry.normalize_source("git", name="remote-skill",
                                      location="https://example.com/x.git", ref="main",
                                      commit="xyz", allowed_root=self.root)

    def test_new_pin_on_same_repo_is_classified_not_overwritten(self):
        record = registry.normalize_source(
            "git", name="remote-skill", location="https://example.com/x.git", ref="main",
            commit="a" * 40, allowed_root=self.root)
        self.registry.add(record)
        newer = registry.normalize_source(
            "git", name="remote-skill", location="https://example.com/x.git", ref="main",
            commit="b" * 40, allowed_root=self.root)
        self.assertEqual(self.registry.classify(newer)["state"], "newer_pin")
        self.assertEqual(self.registry.get("remote-skill")["commit"], "a" * 40)

    def test_filters_and_missing_lookup(self):
        self.registry.add(self.directory_source("alpha-skill"))
        self.registry.add(self.directory_source("beta-skill"))
        self.assertEqual([item["name"] for item in self.registry.list()],
                         ["alpha-skill", "beta-skill"])
        self.assertEqual(len(self.registry.list(kind="directory")), 2)
        self.assertEqual(self.registry.list(kind="git"), [])
        self.assertEqual(len(self.registry.list(name_contains="alpha")), 1)
        self.assertEqual(self.registry.list(name_contains="zzz"), [])
        with self.assertRaises(registry.SkillRegistryError):
            self.registry.get("nope")
        with self.assertRaises(registry.SkillRegistryError):
            self.registry.list(kind="tarball")

    def test_source_normalization_does_not_mutate_input(self):
        record = {"name": "demo-skill", "kind": "directory"}
        snapshot = dict(record)
        normalized = registry.normalize_source("directory", name="demo-skill",
                                               location=self.source, allowed_root=self.root)
        self.assertEqual(record, snapshot)
        self.assertNotIn("identity", normalized)

    def test_removal_is_explicit(self):
        self.registry.add(self.directory_source())
        removed = self.registry.remove("demo-skill")
        self.assertEqual(removed["name"], "demo-skill")
        self.assertEqual(self.registry.list(), [])


class ZipSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wb-zip-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_zip(self, name, entries, *, link=None, duplicate=False):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry, data in entries:
                archive.writestr(entry, data)
            if duplicate:
                archive.writestr("SKILL.md", "重复\n")
            if link:
                info = zipfile.ZipInfo(link)
                info.external_attr = (0o120777 << 16)
                archive.writestr(info, "../outside")
        return path

    def test_valid_zip_is_registered_with_digest(self):
        path = self.make_zip("ok.zip", [("SKILL.md", SKILL_MD_V1), ("refs/notes.md", "x")])
        digest = registry.sha256_file(path)
        record = registry.normalize_source("zip", name="zip-skill", location=path,
                                           allowed_root=self.root, sha256=digest)
        self.assertTrue(record["pinned"])
        self.assertEqual(record["sha256"], digest)
        self.assertEqual(record["entry_count"], 2)

    def test_wrong_declared_digest_is_rejected(self):
        path = self.make_zip("ok.zip", [("SKILL.md", SKILL_MD_V1)])
        with self.assertRaises(registry.SkillRegistryError) as caught:
            registry.normalize_source("zip", name="zip-skill", location=path,
                                      allowed_root=self.root, sha256="0" * 64)
        self.assertIn("DIGEST_MISMATCH", str(caught.exception))

    def test_broken_zip_is_rejected(self):
        path = self.root / "broken.zip"
        path.write_bytes(b"not a zip at all")
        with self.assertRaises(registry.SkillRegistryError) as caught:
            registry.inspect_zip(path, allowed_root=self.root)
        self.assertIn("INVALID_ZIP", str(caught.exception))

    def test_zip_path_traversal_and_absolute_entries_are_rejected(self):
        for entry in ("../escape.md", "/etc/passwd", "C:/windows/x", "refs/../../up.md",
                      ".workbuddy/secret.md"):
            with self.subTest(entry=entry):
                path = self.make_zip("evil.zip", [("SKILL.md", "x"), (entry, "y")])
                with self.assertRaises(registry.SkillRegistryError) as caught:
                    registry.inspect_zip(path, allowed_root=self.root)
                self.assertIn("UNSAFE_ENTRY", str(caught.exception))

    def test_zip_symlink_entry_is_rejected(self):
        path = self.make_zip("link.zip", [("SKILL.md", "x")], link="link.md")
        with self.assertRaises(registry.SkillRegistryError) as caught:
            registry.inspect_zip(path, allowed_root=self.root)
        self.assertIn("UNSAFE_ENTRY", str(caught.exception))

    def test_zip_without_skill_md_is_rejected(self):
        path = self.make_zip("noskill.zip", [("readme.md", "x")])
        with self.assertRaises(registry.SkillRegistryError) as caught:
            registry.inspect_zip(path, allowed_root=self.root)
        self.assertIn("MISSING_ENTRY", str(caught.exception))

    def test_zip_outside_allowed_root_is_rejected(self):
        outside = self.root.parent / "outside.zip"
        outside.write_bytes(b"PK\x03\x04")
        with self.assertRaises(registry.SkillRegistryError) as caught:
            registry.inspect_zip(outside, allowed_root=self.root)
        self.assertIn("UNSAFE_PATH", str(caught.exception))


class EvidenceAndPathTests(SkillCenterCase):
    def test_install_discovery_and_call_are_separate_states(self):
        self.deploy()
        plan = self.plan()
        record = json.loads(Path(plan["record_path"]).read_text(encoding="utf-8"))
        report = deployer.evidence_report(record, host="workbuddy",
                                          scope=adapters.SCOPE_PROJECT, route="desktop",
                                          now_ts=10.0)
        self.assertEqual(report["installed"], "recorded")
        self.assertEqual(report["discovered"], "unverified")
        self.assertEqual(report["called"], "unverified")

    def test_private_directory_targets_are_refused(self):
        with self.assertRaises(ValueError):
            adapters.resolve_target("workbuddy", skill_name="demo-skill",
                                    scope=adapters.SCOPE_PROJECT,
                                    project_root=self.root / ".workbuddy")

    def test_deploy_refuses_private_project_root(self):
        private_root = self.root / ".workbuddy"
        private_root.mkdir()
        with self.assertRaises(registry.SkillRegistryError) as caught:
            deployer.deploy("workbuddy", source_dir=self.source, confirm=True,
                            project_root=private_root, state_root=self.state)
        self.assertIn("UNSAFE_PATH", str(caught.exception))

    def test_source_without_skill_md_is_refused(self):
        empty = self.root / "src" / "empty-skill"
        empty.mkdir()
        (empty / "readme.md").write_text("no entry\n", encoding="utf-8")
        with self.assertRaises(deployer.SkillDeployError) as caught:
            deployer.deploy("codex", source_dir=empty, confirm=True,
                            project_root=self.project, state_root=self.state)
        self.assertIn("INVALID_SOURCE", str(caught.exception))

    def test_global_scope_writes_under_caller_root_only(self):
        result = deployer.deploy("trae", source_dir=self.source, confirm=True,
                                 global_root=self.global_root,
                                 scope=adapters.SCOPE_GLOBAL, state_root=self.state)
        self.assertTrue(result["deployed"])
        self.assertTrue((self.global_root / ".trae" / "skills" / "demo-skill" / "SKILL.md").is_file())
        self.assertFalse((self.project / ".trae").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
