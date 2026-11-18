"""Numbered task references and portable structure contracts, 2026-10-09."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agentbus.config import load, save
from agentbus.claim_fencing import captured_ticket
from agentbus.task_tree import TaskForest, TaskTreeError
from agentbus.tree_templates import (builtin_templates, export_template, import_template,
                                     preview_template, validate_template, adapt_external_tree)


class TreePortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name), create=True)
        self.cfg.data["workers"]["alpha"] = {"enabled": True, "executor_enabled": True,
                                               "kind": "gui", "human_in_loop": True}
        self.cfg = save(self.cfg)
        self.forest = TaskForest(self.cfg)

    def structure(self):
        return {"schema_version": 1,
            "roots": [{"id": "project", "title": "Reusable product", "goal": "Deliver useful work"}],
            "branches": [{"id": "build", "title": "Implementation", "parent_id": "project"}],
            "leaves": [
                {"id": "first", "title": "Build foundation", "goal": "Create module",
                 "handoff": "Continue at public API", "parent_id": "build",
                 "expected_artifacts": ["src/foundation.py"]},
                {"id": "second", "title": "Verify integration", "goal": "Verify artifacts",
                 "parent_id": "build", "dependencies": ["first"]}]}

    def seed(self):
        self.forest.replace_structure(self.structure())
        return self.forest.snapshot()

    def test_hierarchical_numbers_persist_and_id_or_chapter_resolve(self):
        tree = self.seed()
        self.assertEqual([n["section_no"] for n in tree["roots"]], ["0"])
        self.assertEqual([n["section_no"] for n in tree["branches"]], ["1"])
        self.assertEqual([n["section_no"] for n in tree["leaves"]], ["1.1", "1.2"])
        stored = json.loads(self.forest.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["leaves"][0]["section_no"], "1.1")
        self.assertEqual(self.forest.resolve_reference("1.1")["id"], "first")
        self.assertEqual(self.forest.resolve_reference("build")["section_no"], "1")
        self.assertEqual(TaskForest._leaf(tree, "1.1")["id"], "first")

    def test_reorder_and_new_nodes_do_not_renumber_existing_references(self):
        self.seed()
        tree = self.structure()
        tree["leaves"].reverse()
        tree["leaves"].insert(0, {"id": "third", "title": "New item", "parent_id": "build"})
        self.forest.replace_structure(tree)
        numbers = {n["id"]: n["section_no"] for n in self.forest.snapshot()["leaves"]}
        self.assertEqual(numbers, {"first": "1.1", "second": "1.2", "third": "1.3"})

    def test_deleted_chapters_stay_reserved_and_cannot_be_reused_explicitly(self):
        self.seed()
        tree = self.structure()
        tree["leaves"] = [tree["leaves"][0]]
        self.forest.replace_structure(tree)
        self.forest.seed_leaf({"id": "new", "title": "New", "parent_id": "build"})
        self.assertEqual(self.forest.resolve_reference("new")["section_no"], "1.3")
        with self.assertRaises(TaskTreeError):
            self.forest.seed_leaf({"id": "reuse", "title": "Reuse", "section_no": "1.2"}, branch_id="build")

    def test_duplicate_malformed_or_changed_chapter_is_rejected(self):
        self.seed()
        for change in ("1.2", "../../bad", "01.1", "1.0", 1):
            tree = self.structure()
            tree["leaves"][0]["section_no"] = change
            with self.subTest(change=change), self.assertRaises(TaskTreeError):
                self.forest.replace_structure(tree)

    def test_legacy_snapshot_is_read_only_but_next_edit_persists_chapters(self):
        self.forest.root.mkdir(parents=True, exist_ok=True)
        self.forest.path.write_text(json.dumps(self.structure()), encoding="utf-8")
        before = self.forest.path.read_bytes()
        self.assertEqual(self.forest.snapshot()["leaves"][0]["section_no"], "1.1")
        self.assertEqual(self.forest.path.read_bytes(), before)
        self.forest.seed_leaf({"id": "third", "title": "new"}, branch_id="build")
        self.assertEqual(json.loads(self.forest.path.read_text())["leaves"][0]["section_no"], "1.1")

    def test_ambiguous_id_and_chapter_reference_refused(self):
        self.seed()
        before = self.forest.path.read_bytes()
        with self.assertRaises(TaskTreeError):
            self.forest.seed_leaf({"id": "1.1", "title": "Ambiguous label"}, branch_id="build")
        self.assertEqual(before, self.forest.path.read_bytes())
        crafted = self.forest.snapshot()
        crafted["leaves"][0]["id"] = "1"
        with self.assertRaises(TaskTreeError):
            TaskForest._leaf(crafted, "1")

    def test_chapter_dispatch_and_checkpoint_audit_canonical_leaf(self):
        self.seed()
        message = self.forest.dispatch_leaf("1.1", "alpha")
        self.assertEqual(message.to, "alpha")
        claimed = self.forest.claim_leaf("1.1", "alpha")
        self.forest.checkpoint_leaf("1.1", "alpha", "Continue at actual output",
                                    captured_ticket(claimed))
        events = list(self.forest.bus.events())
        self.assertTrue(any(e.get("event") == "task_tree_dispatched" and e.get("leaf_id") == "first" for e in events))
        self.assertTrue(any(e.get("event") == "task_tree_checkpoint" and e.get("leaf_id") == "first" for e in events))

    def test_export_keeps_goals_dependencies_and_relative_artifacts_only(self):
        self.seed()
        self.forest.dispatch_leaf("first", "alpha")
        document = export_template(self.forest)
        encoded = json.dumps(document)
        for forbidden in ("job_id", "task_id", "result_to", "owner", "claim_id", "heartbeat", "history_task_ids"):
            self.assertNotIn('"' + forbidden + '"', encoded)
        self.assertEqual(document["leaves"][0]["expected_artifacts"], ["src/foundation.py"])
        self.assertEqual(document["leaves"][1]["dependencies"], [document["leaves"][0]["id"]])
        self.assertEqual(document["leaves"][0]["section_no"], "1.1")
        self.assertNotIn(self.forest.resolve_reference("first")["task_id"], encoded)

    def test_export_scrubs_configured_roots_and_other_absolute_text_paths(self):
        tree = self.structure()
        tree["leaves"][0]["goal"] = "Use " + str(self.cfg.workspace) + "/src/foundation.py"
        tree["leaves"][0]["handoff"] = r"Input lives at C:\Users\someone\private.txt"
        self.forest.replace_structure(tree)
        document = export_template(self.forest)
        text = json.dumps(document, ensure_ascii=False)
        self.assertNotIn(str(self.cfg.workspace), text)
        self.assertNotIn("someone", text)
        self.assertIn("[项目目录]", document["leaves"][0]["goal"])

    def test_roundtrip_across_projects_starts_unbound_and_preserves_dependencies(self):
        self.seed()
        document = export_template(self.forest)
        other = TaskForest(load(Path(self.temp.name) / "other-project", create=True))
        imported = import_template(other, document)
        self.assertEqual(imported["leaves"][0]["state"], "queued")
        self.assertIsNone(imported["leaves"][0]["task_id"])
        self.assertEqual(imported["leaves"][1]["dependencies"], [imported["leaves"][0]["id"]])
        self.assertFalse((other.root / "jobs").exists())
        self.assertFalse((other.root / "agents").exists())

    def test_gemini_export_runtime_fields_are_stripped_but_structure_is_imported(self):
        document = {
            "schema_version": 1,
            "project_name": "外部协作树",
            "generated_at": "2026-10-09T12:00:00Z",
            "roots": [{"id": "root", "parent_id": None, "title": "交付目标",
                       "goal": "完成可复用工具", "state": "done", "owner": "old-agent",
                       "task_id": "m-old-root", "job_id": "j-old-root",
                       "results": [{"private": "old result"}], "artifacts": [],
                       "claimable": False, "eligible_agents": ["old-agent"],
                       "history_task_ids": ["m-old-history"], "section_no": "0"}],
            "branches": [{"id": "branch", "parent_id": "root", "title": "实现",
                          "goal": "实现模块", "state": "active", "owner": "old-agent",
                          "history_task_ids": ["m-old-branch"]}],
            "leaves": [{"id": "leaf-a", "parent_id": "branch", "title": "实现接口",
                        "goal": "完成接口文件", "handoff": "由下一位继续验证",
                        "expected_artifacts": ["src/adapter.py"],
                        "required_capabilities": ["python"], "state": "done",
                        "owner": "old-agent", "task_id": "m-old-leaf",
                        "job_id": "j-old-leaf", "results": "old result",
                        "artifacts": [{"path": "C:/old-project/private.txt"}],
                        "claimable": False, "eligible_agents": ["old-agent"],
                        "history_task_ids": ["m-old-history"], "progress_percent": 100,
                        "recommendation_reason": "old recommendation",
                        "recommended_agent": "old-agent", "unmet_dependencies": [],
                        "evidence_error": "old evidence"},
                       {"id": "leaf-b", "parent_id": "branch", "title": "复核接口",
                        "goal": "检查接口行为", "dependencies": ["leaf-a"],
                        "unmet_dependencies": [], "state": "blocked"}],
        }

        portable, warnings = adapt_external_tree(document)
        self.assertEqual(portable["title"], "外部协作树")
        self.assertEqual(len(portable["roots"]), 1)
        self.assertEqual(len(portable["branches"]), 1)
        self.assertEqual(len(portable["leaves"]), 2)
        self.assertEqual(portable["leaves"][0]["goal"], "完成接口文件")
        self.assertEqual(portable["leaves"][0]["expected_artifacts"], ["src/adapter.py"])
        self.assertEqual(portable["leaves"][0]["required_capabilities"], ["python"])
        self.assertEqual(portable["leaves"][1]["dependencies"], ["leaf-a"])
        warning_text = " ".join(warnings)
        self.assertIn("重新编号", warning_text)
        for stale_field in ("history_task_ids", "claimable", "eligible_agents",
                            "progress_percent", "recommended_agent", "unmet_dependencies",
                            "evidence_error", "results", "owner", "task_id", "job_id"):
            self.assertIn(stale_field, warning_text)
        encoded = json.dumps(portable, ensure_ascii=False)
        for stale_value in ("old-agent", "m-old-leaf", "j-old-leaf", "old result",
                            "private.txt", "old recommendation", "old evidence"):
            self.assertNotIn(stale_value, encoded)

        other = TaskForest(load(Path(self.temp.name) / "gemini-import", create=True))
        import_template(other, portable)
        imported = other._structure()
        self.assertEqual(len(imported["roots"]), 1)
        self.assertEqual(len(imported["branches"]), 1)
        self.assertEqual(len(imported["leaves"]), 2)
        leaves = {node["title"]: node for node in imported["leaves"]}
        self.assertEqual(leaves["实现接口"]["section_no"], "1.1")
        self.assertEqual(leaves["复核接口"]["section_no"], "1.2")
        self.assertEqual(leaves["复核接口"]["dependencies"], [leaves["实现接口"]["id"]])
        for kind in ("roots", "branches", "leaves"):
            for node in imported[kind]:
                self.assertFalse({"owner", "task_id", "job_id", "results", "state"} & set(node))

    def test_import_append_preserves_live_task_claim_and_previous_bytes(self):
        self.seed()
        claimed = self.forest.claim_leaf("first", "alpha")
        active = self.forest.bus.agents_dir / "alpha" / "processing" / (claimed.id + ".json")
        digest = hashlib.sha256(active.read_bytes()).hexdigest()
        before = self.forest.resolve_reference("first")
        imported = import_template(self.forest, builtin_templates()[0]["document"])
        self.assertEqual(hashlib.sha256(active.read_bytes()).hexdigest(), digest)
        self.assertEqual(self.forest.resolve_reference("first"), before)
        self.assertEqual(len(imported["roots"]), 2)
        self.assertEqual(imported["roots"][1]["section_no"], "0.1")
        self.assertEqual(imported["leaves"][0]["task_id"], claimed.id)

    def test_repeated_import_new_namespaces_and_unique_numbers(self):
        doc = builtin_templates()[0]["document"]
        import_template(self.forest, doc)
        view = import_template(self.forest, doc)
        identities = [n["id"] for key in ("roots", "branches", "leaves") for n in view[key]]
        numbers = [n["section_no"] for key in ("roots", "branches", "leaves") for n in view[key]]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_imported_root_dispatch_uses_its_own_goal_and_job(self):
        self.seed()
        original = self.forest.dispatch_leaf("first", "alpha")
        view = import_template(self.forest, builtin_templates()[1]["document"])
        leaf = next(n for n in view["leaves"] if n["title"] == "明确问题与判断标准")
        message = self.forest.dispatch_leaf(leaf["section_no"], "alpha")
        self.assertNotEqual(message.job_id, original.job_id)
        job = json.loads((self.forest.root / "jobs" / (message.job_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(job["goal"], "研究与报告")

    def test_template_rejects_execution_fields_and_path_attacks_without_write(self):
        doc = builtin_templates()[0]["document"]
        bads = []
        for key, value in (("owner", "alpha"), ("task_id", "m-old"), ("claim_id", "ticket"),
                           ("state", "delivered"), ("__proto__", {})):
            bad = copy.deepcopy(doc)
            bad["leaves"][0][key] = value
            bads.append(bad)
        for path in ("../outside.txt", "C:/secret.txt", "\\\\host\\share\\x", "/tmp/secret", ".workbuddy/x", ".agentbus/config.json", "src/../bad", "src\\file.py"):
            bad = copy.deepcopy(doc)
            bad["leaves"][0]["expected_artifacts"] = [path]
            bads.append(bad)
        for bad in bads:
            with self.subTest(bad=bad["leaves"][0]), self.assertRaises(TaskTreeError):
                import_template(self.forest, bad)
        self.assertFalse(self.forest.path.exists())

    def test_template_rejects_unknown_dependency_cycles_duplicate_id_and_huge_text(self):
        doc = builtin_templates()[0]["document"]
        variants = []
        bad = copy.deepcopy(doc)
        bad["leaves"][0]["dependencies"] = ["missing"]
        variants.append(bad)
        bad = copy.deepcopy(doc)
        bad["leaves"][0]["dependencies"] = [bad["leaves"][-1]["id"]]
        variants.append(bad)
        bad = copy.deepcopy(doc)
        bad["leaves"][1]["id"] = bad["leaves"][0]["id"]
        variants.append(bad)
        bad = copy.deepcopy(doc)
        bad["leaves"][0]["goal"] = "x" * 100001
        variants.append(bad)
        for bad in variants:
            with self.assertRaises(TaskTreeError):
                validate_template(bad)

    def test_preview_and_builtins_are_valid_data_not_executable(self):
        # The two original presets keep their pinned shape; presets added later
        # (the industry library) are checked structurally instead of by a magic
        # node count, so growing the library cannot silently relax this guard.
        pinned = {"software-project": (9, 5), "research-project": (9, 5)}
        for item in builtin_templates():
            document = item["document"]
            self.assertEqual(validate_template(document), document)
            preview = preview_template(document)
            self.assertEqual(preview["node_count"],
                             preview["roots"] + preview["branches"] + preview["leaves"])
            self.assertEqual(preview["roots"], 1)
            self.assertGreaterEqual(preview["leaves"], 5)
            self.assertTrue(preview["warnings"])
            if item["id"] in pinned:
                self.assertEqual((preview["node_count"], preview["leaves"]), pinned[item["id"]])
        doc = builtin_templates()[0]["document"]
        marker = Path(self.temp.name) / "must-not-exist"
        doc["leaves"][0]["goal"] = "Run arbitrary instruction only after user task acceptance"
        doc["leaves"][0]["handoff"] = "__import__('os').system('touch must-not-exist')"
        import_template(self.forest, doc)
        self.assertFalse(marker.exists())

    def test_long_template_ids_import_with_bounded_namespace(self):
        doc = builtin_templates()[0]["document"]
        old = doc["leaves"][0]["id"]
        long_id = "n" * 128
        doc["leaves"][0]["id"] = long_id
        for node in doc["leaves"]:
            node["dependencies"] = [long_id if value == old else value for value in node["dependencies"]]
        view = import_template(self.forest, doc)
        self.assertTrue(all(len(node["id"]) <= 128 for kind in ("roots", "branches", "leaves") for node in view[kind]))
        self.assertNotIn(long_id, json.dumps(view))

    def test_input_limits_nonfinite_unicode_and_local_text_are_rejected(self):
        document = builtin_templates()[0]["document"]
        variants = []
        for version in (True, float("nan"), 2):
            doc = copy.deepcopy(document)
            doc["schema_version"] = version
            variants.append(doc)
        for text in ("\x00invalid", "\ud800", "/custom/private/source.py", "file:///Users/private"):
            doc = copy.deepcopy(document)
            doc["leaves"][0]["goal"] = text
            variants.append(doc)
        doc = copy.deepcopy(document)
        doc["leaves"] = [{"id": "leaf-" + str(i), "title": "Many", "parent_id": doc["branches"][0]["id"]}
                          for i in range(1001)]
        variants.append(doc)
        for doc in variants:
            with self.assertRaises(TaskTreeError):
                validate_template(doc)

    def test_structure_depth_limit_and_invalid_registry_fail_cleanly(self):
        tree = self.structure()
        previous = "project"
        tree["branches"] = []
        for index in range(66):
            identity = "nested-" + str(index)
            tree["branches"].append({"id": identity, "parent_id": previous, "title": "Nested"})
            previous = identity
        tree["leaves"] = [{"id": "leaf", "parent_id": previous, "title": "Deep"}]
        with self.assertRaises(TaskTreeError):
            self.forest.replace_structure(tree)
        for invalid in ({}, [{"bad": "registry"}], ["1.0"]):
            tree = self.structure()
            tree["section_registry"] = invalid
            with self.assertRaises(TaskTreeError):
                self.forest.replace_structure(tree)

    def test_concurrent_imports_append_without_lost_updates(self):
        document = builtin_templates()[0]["document"]
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(import_template, self.forest, document) for _ in range(4)]
            for future in futures:
                future.result(timeout=15)
        view = self.forest.snapshot()
        self.assertEqual(len(view["roots"]), 4)
        self.assertEqual(len(view["leaves"]), 20)
        refs = [node["section_no"] for kind in ("roots", "branches", "leaves") for node in view[kind]]
        self.assertEqual(len(refs), len(set(refs)))

    def test_import_failed_atomic_publish_keeps_existing_tree(self):
        self.seed()
        before = self.forest.path.read_bytes()
        with patch("agentbus.task_tree.os.replace", side_effect=OSError("simulated locked destination")):
            with self.assertRaises(OSError):
                import_template(self.forest, builtin_templates()[0]["document"])
        self.assertEqual(self.forest.path.read_bytes(), before)
        self.assertEqual(list(self.forest.root.glob("task-tree.*.tmp")), [])

    def test_urls_are_portable_but_artifact_case_collisions_are_not(self):
        document = builtin_templates()[0]["document"]
        document["leaves"][0]["goal"] = "Read https://github.com/project/repository and cite it"
        self.assertEqual(validate_template(document)["leaves"][0]["goal"], document["leaves"][0]["goal"])
        document["leaves"][0]["expected_artifacts"] = ["src/File.py", "src/file.py"]
        with self.assertRaises(TaskTreeError):
            validate_template(document)

    def test_export_cleans_old_runtime_text_and_preserves_goal_after_path(self):
        tree = self.structure()
        tree["leaves"][0]["goal"] = r"Read C:\old\source.txt then implement the new public API."
        tree["leaves"][0]["handoff"] = (
            "Old task m-1791457584489-f05b6b, job j-b7ed91331128d934d22268834e306bd4, "
            "revision r-cdef881858336918798c18bb8f096f25; "
            'claim_id="abcdef123456" claim_epoch=3; continue from actual code.')
        self.forest.replace_structure(tree)
        document = export_template(self.forest)
        leaf = document["leaves"][0]
        self.assertIn("then implement the new public API.", leaf["goal"])
        self.assertIn("continue from actual code.", leaf["handoff"])
        for value in ("m-179145", "j-b7ed", "r-cdef", "abcdef123456", "claim_epoch", "claim_id"):
            self.assertNotIn(value, leaf["handoff"])
        self.assertIn("已清理", document["description"])
        self.assertTrue(any("已清理" in message for message in preview_template(document)["warnings"]))

    def test_quoted_paths_with_spaces_and_old_relative_records_are_scrubbed(self):
        tree = self.structure()
        tree["leaves"][0]["goal"] = 'Read "C:\\Program Files\\Old Tool\\input.txt" then produce a new file.'
        tree["leaves"][0]["handoff"] = "See .agentbus/jobs/j-old-example.json and preserve the handoff summary."
        self.forest.replace_structure(tree)
        document = export_template(self.forest)
        leaf = document["leaves"][0]
        self.assertNotIn("Old Tool", leaf["goal"])
        self.assertIn("then produce a new file.", leaf["goal"])
        self.assertNotIn(".agentbus/jobs/", leaf["handoff"])
        self.assertIn("preserve the handoff summary.", leaf["handoff"])

    def test_import_rejects_local_paths_old_text_fields_and_allows_https_documents(self):
        document = builtin_templates()[0]["document"]
        for field in ("goal", "handoff"):
            for text in (r"C:\private\file.txt", r"\\server\share\file", r"\rooted\file.txt",
                         "/single-file", "/自定义/资料.md", "file:///custom/file.txt", "m-old-example",
                         "claim_id=oldticket", "See .agentbus/agents/codex/done/old.json"):
                doc = copy.deepcopy(document)
                doc["leaves"][0][field] = text
                with self.subTest(field=field, text=text), self.assertRaises(TaskTreeError):
                    validate_template(doc)
        for url in ("https://learn.microsoft.com/en-us/docs/path", "https://github.com/org/project", "https://example.com/?q=hello"):
            document["leaves"][0]["goal"] = "Read " + url + " and implement only the documented behavior."
            self.assertEqual(validate_template(document)["leaves"][0]["goal"], document["leaves"][0]["goal"])

    def test_relative_artifacts_reject_controls_devices_trailing_dot_or_space(self):
        document = builtin_templates()[0]["document"]
        for path in ("src/nul\x00.txt", "src/tab\t.txt", "src/new\nline.txt", "src/del\x7f.txt",
                     "src/CON", "NUL.txt", "src/COM1.log", "LPT¹.txt", "src/CONIN$.txt",
                     "src/file.", "src/file ", "src/folder./file.txt", 'src/a"b.txt', "src/a|b.txt"):
            document["leaves"][0]["expected_artifacts"] = [path]
            with self.subTest(path=path), self.assertRaises(TaskTreeError):
                validate_template(document)

    def test_api_routes_survive_export_and_validation_without_cleanup(self):
        tree = self.structure()
        goal = "Implement GET /api/users and POST /api/users. Keep the handler idempotent."
        handoff = "The next API route /v1/accounts accepts PATCH /v1/accounts/{id}."
        tree["leaves"][0].update(goal=goal, handoff=handoff)
        self.forest.replace_structure(tree)
        document = export_template(self.forest)
        self.assertEqual(document["leaves"][0]["goal"], goal)
        self.assertEqual(document["leaves"][0]["handoff"], handoff)
        self.assertEqual(validate_template(document)["leaves"][0]["goal"], goal)
        self.assertEqual(document["description"], "")
        for field in ("goal", "handoff"):
            doc = copy.deepcopy(document)
            doc["leaves"][0][field] = "Read /tmp/private/input.txt then implement GET /api/users."
            with self.assertRaises(TaskTreeError):
                validate_template(doc)

    def test_branch_dependencies_gate_child_claim_and_dispatch_until_receipt(self):
        tree = {"schema_version": 1,
            "roots": [{"id": "project", "title": "App"}],
            "branches": [{"id": "http", "title": "HTTP", "parent_id": "project"},
                         {"id": "ui", "title": "UI", "parent_id": "project", "dependencies": ["http"]}],
            "leaves": [{"id": "server", "title": "Server", "parent_id": "http"},
                       {"id": "button", "title": "Button", "parent_id": "ui"}]}
        self.forest.replace_structure(tree)
        view = TaskForest._leaf(self.forest.snapshot(), "button")
        self.assertEqual(view["effective_dependencies"], ["http"])
        self.assertEqual(view["unmet_dependencies"], ["http"])
        self.assertFalse(view["claimable"])
        for action in (self.forest.claim_leaf, self.forest.dispatch_leaf):
            with self.assertRaises(TaskTreeError):
                action("button", "alpha")
        message = self.forest.claim_leaf("server", "alpha")
        self.forest.bus.heartbeat("alpha", message, captured_ticket(message),
                                  note="test dependency task started")
        self.forest.bus.commit_result("alpha", message, captured_ticket(message), body="Implemented server", exit_code=0)
        self.assertTrue(TaskForest._leaf(self.forest.snapshot(), "button")["claimable"])
        self.assertEqual(self.forest.dispatch_leaf("button", "alpha").to, "alpha")

    def test_cross_root_dependencies_propagate_and_inherited_cycles_reject(self):
        tree = {"schema_version": 1,
            "roots": [{"id": "foundation", "title": "Foundation"},
                      {"id": "product", "title": "Product", "dependencies": ["foundation"]}],
            "branches": [], "leaves": [
                {"id": "core", "title": "Core", "parent_id": "foundation"},
                {"id": "extension", "title": "Extension", "parent_id": "product"}]}
        self.forest.replace_structure(tree)
        self.assertFalse(TaskForest._leaf(self.forest.snapshot(), "extension")["claimable"])
        tree["roots"][0]["dependencies"] = ["extension"]
        with self.assertRaises(TaskTreeError):
            self.forest.replace_structure(tree)

    def test_stale_config_does_not_dispatch_or_claim_removed_member(self):
        self.seed()
        current = load(self.cfg.workspace)
        current.data["workers"]["alpha"]["enabled"] = False
        save(current)
        before = self.forest.path.read_bytes()
        for action in (self.forest.dispatch_leaf, self.forest.claim_leaf):
            with self.assertRaises(TaskTreeError):
                action("1.1", "alpha")
        self.assertEqual(before, self.forest.path.read_bytes())
        self.assertFalse((self.forest.root / "jobs").exists())

    def test_stale_config_does_not_ignore_removed_capability(self):
        current = load(self.cfg.workspace)
        current.data["workers"]["alpha"]["capabilities"] = ["edit"]
        current = save(current)
        forest = TaskForest(current)
        tree = self.structure()
        tree["leaves"][0]["required_capabilities"] = ["edit"]
        forest.replace_structure(tree)
        changed = load(current.workspace)
        changed.data["workers"]["alpha"]["capabilities"] = []
        save(changed)
        with self.assertRaises(TaskTreeError):
            forest.dispatch_leaf("1.1", "alpha")

    def test_seed_default_reuses_imported_single_root_and_multiple_roots_require_choice(self):
        view = import_template(self.forest, builtin_templates()[0]["document"])
        root = view["roots"][0]["id"]
        self.forest.seed_leaf({"id": "extra", "title": "Additional work"})
        self.assertEqual(self.forest.resolve_reference("extra")["parent_id"], root)
        self.assertFalse(any(node["id"] == node["parent_id"] for node in self.forest._structure()["branches"]))
        import_template(self.forest, builtin_templates()[1]["document"])
        before = self.forest.path.read_bytes()
        with self.assertRaises(TaskTreeError):
            self.forest.seed_leaf({"id": "ambiguous", "title": "Choose root"})
        self.assertEqual(before, self.forest.path.read_bytes())
        self.forest.seed_leaf({"id": "explicit", "title": "Selected root"}, root_id=root)
        self.assertEqual(self.forest.resolve_reference("explicit")["parent_id"], root)


class IndustryPresetAndHandoverTests(unittest.TestCase):
    """Fixed contract for the industry presets and the Markdown handover digest."""

    NEW_IDS = ("agile-web-delivery", "ai-agent-research", "business-mvp")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name), create=True)
        self.cfg.data["workers"]["alpha"] = {"enabled": True, "executor_enabled": True,
                                             "kind": "gui", "human_in_loop": True}
        self.cfg = save(self.cfg)
        self.templates = {item["id"]: item for item in builtin_templates()}

    def fresh_forest(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = load(Path(folder.name), create=True)
        cfg.data["workers"]["alpha"] = {"enabled": True, "executor_enabled": True,
                                        "kind": "gui", "human_in_loop": True}
        return TaskForest(save(cfg))

    # ---- 1. presets ----------------------------------------------------

    def test_original_presets_keep_their_order_and_defaults(self):
        items = builtin_templates()
        self.assertEqual([item["id"] for item in items[:2]],
                         ["software-project", "research-project"])
        document = self.templates["software-project"]["document"]
        self.assertEqual((len(document["roots"]), len(document["branches"]),
                          len(document["leaves"])), (1, 3, 5))
        leaf = next(node for node in document["leaves"] if node["id"] == "leaf-requirements")
        # The extended preset DSL must not retro-fit goals or artifacts onto old presets.
        self.assertEqual(leaf["goal"], leaf["title"])
        self.assertEqual(list(leaf.get("expected_artifacts") or []), [])

    def test_industry_presets_are_present_with_expected_titles(self):
        expected = {"agile-web-delivery": "全栈 Web 敏捷开发实战",
                    "ai-agent-research": "AI Agent 多智能体协同研发",
                    "business-mvp": "商业策划与 MVP 闭环"}
        for identity, title in expected.items():
            with self.subTest(template=identity):
                self.assertIn(identity, self.templates)
                self.assertEqual(self.templates[identity]["title"], title)
                self.assertTrue(self.templates[identity]["description"])

    def test_every_preset_validates_and_previews_as_reusable_data(self):
        for item in builtin_templates():
            with self.subTest(template=item["id"]):
                document = item["document"]
                self.assertEqual(validate_template(copy.deepcopy(document))["title"],
                                 document["title"])
                preview = preview_template(document)
                self.assertEqual(preview["roots"], 1)
                self.assertGreaterEqual(preview["branches"], 3)
                self.assertGreaterEqual(preview["leaves"], 5)
                self.assertEqual(preview["node_count"],
                                 preview["roots"] + preview["branches"] + preview["leaves"])

    def test_industry_preset_leaves_declare_specific_goal_and_relative_artifact(self):
        for identity in self.NEW_IDS:
            for node in self.templates[identity]["document"]["leaves"]:
                with self.subTest(template=identity, leaf=node["id"]):
                    self.assertTrue(node.get("goal"))
                    self.assertNotEqual(node["goal"], node["title"])
                    self.assertTrue(node["expected_artifacts"])
                    for path in node["expected_artifacts"]:
                        self.assertFalse(Path(path).is_absolute())
                        self.assertNotIn("\\", path)
                        self.assertNotIn(":", path)
                        self.assertNotIn("..", path.split("/"))

    def test_industry_preset_dependencies_resolve_to_sibling_leaves(self):
        for identity in self.NEW_IDS:
            document = self.templates[identity]["document"]
            leaf_ids = {node["id"] for node in document["leaves"]}
            known = {node["id"] for kind in ("roots", "branches", "leaves")
                     for node in document[kind]}
            self.assertTrue(any(node.get("dependencies") for node in document["leaves"]))
            for node in document["leaves"]:
                for dependency in node.get("dependencies", []):
                    with self.subTest(template=identity, leaf=node["id"]):
                        self.assertIn(dependency, known)
                        self.assertIn(dependency, leaf_ids)

    def test_industry_presets_carry_no_execution_state(self):
        for identity in self.NEW_IDS:
            document = self.templates[identity]["document"]
            for kind in ("roots", "branches", "leaves"):
                for node in document[kind]:
                    with self.subTest(template=identity, node=node["id"]):
                        for forbidden in ("owner", "task_id", "job_id", "status",
                                          "result_id", "artifacts", "heartbeat", "claim_id"):
                            self.assertNotIn(forbidden, node)

    def test_importing_each_industry_preset_appends_unbound_nodes(self):
        for identity in self.NEW_IDS:
            with self.subTest(template=identity):
                forest = self.fresh_forest()
                snapshot = import_template(forest, self.templates[identity]["document"])
                self.assertEqual((len(snapshot["roots"]), len(snapshot["branches"]),
                                  len(snapshot["leaves"])), (1, 5, 10))
                self.assertTrue(all(not node.get("task_id") for node in snapshot["leaves"]))
                self.assertTrue(all(not node.get("job_id") for node in snapshot["leaves"]))
                sections = [node["section_no"] for node in snapshot["branches"] + snapshot["leaves"]]
                self.assertEqual(len(sections), len(set(sections)))
                again = import_template(forest, self.templates[identity]["document"])
                self.assertEqual(len(again["leaves"]), 20)
                replaced = import_template(forest, self.templates[identity]["document"], replace=True)
                self.assertEqual(len(replaced["leaves"]), 10)

    def test_import_with_replace_clears_previous_forest(self):
        forest = self.fresh_forest()
        snap1 = import_template(forest, self.templates["agile-web-delivery"]["document"])
        self.assertEqual(len(snap1["leaves"]), 10)
        snap2 = import_template(forest, self.templates["ai-agent-research"]["document"], replace=True)
        self.assertEqual(len(snap2["leaves"]), 10)
        self.assertEqual(len(snap2["roots"]), 1)
        # 确保旧树中的节点已被清空，只保留新树
        view = forest._structure()
        self.assertEqual(len(view["leaves"]), 10)

    # ---- 2. handover digest --------------------------------------------

    def handover_sample(self):
        return {"forest": {
            "roots": [{"id": "r", "title": "目标", "section_no": "0", "state": "claimed"}],
            "branches": [{"id": "b", "title": "分支", "section_no": "1", "state": "queued"}],
            "leaves": [
                {"id": "l1", "title": "已交付任务", "section_no": "1.2", "state": "delivered",
                 "owner": "alpha", "result_id": "m-result",
                 "artifacts": [{"path": "成果/a.md", "bytes": 12, "sha256": "a" * 64}]},
                {"id": "l2", "title": "待认领任务", "section_no": "1.10", "state": "queued"},
                {"id": "l3", "title": "受阻任务", "section_no": "1.3", "state": "blocked",
                 "results": "等待前序节点的正式成果回执。"}]},
            "stopped": False}

    def test_handover_markdown_has_all_sections_and_counts_only_verified_delivery(self):
        from agentbus.handover import build_handover_markdown
        text = build_handover_markdown(self.handover_sample(), project_name="示例项目",
                                       workspace="C:/work", output_root="C:/out",
                                       generated_at="2026-10-11T00:00:00+08:00", stopped=False)
        for heading in ("# 项目进度与交付归档", "## 一、交付进度总览",
                        "## 二、交付成果与摘要（SHA256）", "## 三、待推进与需关注项",
                        "## 四、边界说明"):
            self.assertIn(heading, text)
        self.assertIn("已交付 1／3", text)
        self.assertIn("成果/a.md", text)
        self.assertIn("a" * 64, text)
        self.assertIn("正常运行", text)

    def test_handover_orders_chapters_numerically(self):
        from agentbus.handover import build_handover_markdown
        text = build_handover_markdown(self.handover_sample(), project_name="P",
                                       generated_at="t")
        positions = [text.index("已交付任务"), text.index("受阻任务"), text.index("待认领任务")]
        self.assertEqual(positions, sorted(positions))

    def test_handover_escapes_table_separators_and_newlines(self):
        from agentbus.handover import build_handover_markdown
        snapshot = {"forest": {
            "roots": [{"id": "r", "title": "目标 | 带竖线", "section_no": "0", "state": "queued"}],
            "branches": [], "leaves": [
                {"id": "l", "title": "标题\n换行", "section_no": "1", "state": "queued",
                 "results": "结论 | 竖线"}]}, "stopped": True}
        text = build_handover_markdown(snapshot, project_name="P", generated_at="t", stopped=True)
        self.assertIn("目标 \\| 带竖线", text)
        self.assertIn("标题 换行", text)
        self.assertNotIn("| 标题\n换行", text)
        self.assertIn("已暂停新派发", text)

    def test_handover_without_a_tree_reports_honestly(self):
        from agentbus.handover import build_handover_markdown
        text = build_handover_markdown({}, project_name="空项目", generated_at="t")
        self.assertIn("尚未创建任务树", text)
        self.assertIn("尚无已核验产物记录", text)
        self.assertIn("已交付 0／0", text)

    def test_export_handover_action_is_read_only_until_saving(self):
        from agentbus.web_bridge import WebBridge
        bridge = WebBridge(str(self.cfg.workspace))
        result = bridge.call("export_handover", {})
        self.assertTrue(result["ok"], result.get("error"))
        data = result["data"]
        self.assertEqual(data["filename"], "项目进度与交付归档.md")
        self.assertEqual(data["bytes"], len(data["markdown"].encode("utf-8")))
        self.assertEqual(data["sha256"],
                         hashlib.sha256(data["markdown"].encode("utf-8")).hexdigest())
        self.assertIsNone(data["path"])
        self.assertFalse((self.cfg.workspace / "项目进度与交付归档.md").exists())

    def test_export_handover_saves_markdown_and_digest_matches_disk(self):
        from agentbus.web_bridge import WebBridge
        bridge = WebBridge(str(self.cfg.workspace))
        target = self.cfg.workspace / "项目进度与交付归档.md"
        result = bridge.call("export_handover", {"save": True, "path": str(target)})
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["path"], str(target))
        raw = target.read_bytes()
        self.assertEqual(raw, result["data"]["markdown"].encode("utf-8"))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result["data"]["sha256"])

    def test_export_handover_refuses_private_directory_and_non_markdown(self):
        from agentbus.web_bridge import WebBridge
        bridge = WebBridge(str(self.cfg.workspace))
        for bad in (self.cfg.workspace / ".agentbus" / "x.md",
                    self.cfg.workspace / "notes.txt"):
            with self.subTest(target=str(bad)):
                self.assertFalse(bridge.call("export_handover",
                                             {"save": True, "path": str(bad)})["ok"])
        self.assertFalse((self.cfg.workspace / "notes.txt").exists())

    def test_export_handover_rejects_non_boolean_save_flag(self):
        from agentbus.web_bridge import WebBridge
        bridge = WebBridge(str(self.cfg.workspace))
        self.assertFalse(bridge.call("export_handover", {"save": "yes"})["ok"])

    def test_web_surface_exposes_the_handover_export_without_auto_running_it(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "agentbus" / "web_assets" / "index.html").read_text(encoding="utf-8")
        script = (root / "agentbus" / "web_assets" / "app.js").read_text(encoding="utf-8")
        bridge = (root / "agentbus" / "web_bridge.py").read_text(encoding="utf-8")
        self.assertIn("exportHandoverButton", html)
        self.assertIn("导出项目交接总结", html)
        self.assertIn("exportHandoverButton", script)
        self.assertIn("export_handover", script)
        self.assertIn("'export_handover': self._export_handover", bridge)


if __name__ == "__main__":
    unittest.main()
