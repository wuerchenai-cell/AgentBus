"""Task forest contracts. Source: user forest decision / Codex, 2026-10-08."""
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
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agentbus.bus import Bus, BusError, LateResultRejected
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus import jobs
from agentbus.task_tree import TaskForest, TaskTreeError, snapshot, seed_leaf, checkpoint_leaf


class TaskForestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load(Path(self.tmp.name), create=True)
        self.cfg.data["workers"].update({
            "alpha": {"enabled": True, "executor_enabled": True, "can_spawn": False, "kind": "gui"},
            "beta": {"enabled": True, "executor_enabled": True, "can_spawn": False, "kind": "gui"},
            "disabled": {"enabled": False, "executor_enabled": True, "can_spawn": False, "kind": "gui"},
            "observer": {"enabled": True, "executor_enabled": False, "can_spawn": False, "kind": "gui"},
        })
        self.cfg = save(self.cfg)
        self.forest = TaskForest(self.cfg)
        self.bus = self.forest.bus
        self.bus.init()

    def tree(self, second=False):
        tree = {"schema_version": 1,
                "roots": [{"id": "project", "parent_id": None, "title": "Project", "goal": "Ship actual outputs"}],
                "branches": [{"id": "feature", "parent_id": "project", "title": "Feature"}],
                "leaves": [{"id": "first", "parent_id": "feature", "title": "Build first",
                            "goal": "Produce first output", "handoff": "Start at first output"}]}
        if second:
            tree["leaves"].append({"id": "second", "parent_id": "feature", "title": "Build second",
                                   "goal": "Use the first output", "dependencies": ["first"]})
        self.forest.replace_structure(tree)
        return tree

    def leaf(self, identity="first"):
        return next(node for node in self.forest.snapshot()["leaves"] if node["id"] == identity)

    def finish(self, message, body="Actual delivered work", artifacts=None):
        self.bus.heartbeat(message.to, message, captured_ticket(message), note="test executor is alive")
        return self.bus.commit_result(message.to, message, captured_ticket(message),
                                      body=body, artifacts=artifacts, exit_code=0)

    def test_snapshot_of_empty_project_does_not_write_structure_or_mailboxes(self):
        empty = Path(self.tmp.name) / "empty"
        cfg = load(empty, create=True)
        before = set(empty.rglob("*"))
        self.assertEqual(snapshot(cfg)["leaves"], [])
        self.assertEqual(before, set(empty.rglob("*")))

    def test_existing_jobs_are_projected_without_fixed_agent_brand(self):
        job = jobs.start_job(self.cfg, "alpha", "Existing project objective", request_id="existing")
        dispatched = jobs.dispatch_job(self.cfg, "codex", job["id"],
                                        [{"worker": "beta", "title": "existing task", "body": "real work",
                                          "result_to": "alpha"}])
        view = self.forest.snapshot()
        leaf = view["leaves"][0]
        self.assertEqual(leaf["task_id"], dispatched["tasks"][0]["id"])
        self.assertEqual(leaf["eligible_agents"], ["beta"])
        self.assertFalse(self.forest.path.exists())

    def test_unassigned_leaf_any_enabled_executor_can_claim_and_binding_has_no_state(self):
        self.tree()
        view = self.leaf()
        self.assertIn("alpha", view["eligible_agents"])
        self.assertIn("beta", view["eligible_agents"])
        self.assertNotIn("disabled", view["eligible_agents"])
        self.assertNotIn("observer", view["eligible_agents"])
        claimed = self.forest.claim_leaf("first", "beta")
        self.assertEqual(claimed.to, "beta")
        self.assertTrue(claimed.claim_id)
        self.assertEqual(self.leaf()["state"], "claimed")
        persisted = json.loads(self.forest.path.read_text(encoding="utf-8"))
        self.assertNotIn("state", persisted["leaves"][0])
        self.assertNotIn("owner", persisted["leaves"][0])
        self.assertNotIn("results", persisted["leaves"][0])

    def test_strict_supervision_excludes_auditor_from_manual_task_candidates(self):
        cfg = load(self.tmp.name)
        cfg.data["supervision"] = {
            "strict_supervision": True,
            "designated_auditor": "beta",
            "checks": [{"argv": ["python", "--version"], "timeout_seconds": 5}],
        }
        self.cfg = save(cfg)
        self.forest = TaskForest(self.cfg)
        self.bus = self.forest.bus
        self.tree()

        candidates = self.leaf()["eligible_agents"]
        self.assertIn("alpha", candidates)
        self.assertNotIn("beta", candidates)
        with self.assertRaises(TaskTreeError):
            self.forest.dispatch_leaf("first", "beta")

    def test_dispatch_leaf_publishes_to_selected_ready_inbox_without_claiming(self):
        self.tree()
        message = self.forest.dispatch_leaf("first", "beta")
        ready = self.bus.agents_dir / "beta" / "ready" / (message.id + ".json")
        processing = self.bus.agents_dir / "beta" / "processing" / (message.id + ".json")
        self.assertEqual(message.to, "beta")
        self.assertEqual(message.type, "task")
        self.assertEqual(message.result_to, self.cfg.director)
        self.assertTrue(ready.is_file())
        self.assertFalse(processing.exists())
        view = self.leaf()
        self.assertEqual(view["task_id"], message.id)
        self.assertEqual(view["owner"], "beta")
        self.assertEqual(view["state"], "queued")
        self.assertTrue(view["claimable"])
        self.assertEqual(view["eligible_agents"], ["beta"])

        repeated = self.forest.dispatch_leaf("first", "beta")
        self.assertEqual(repeated.id, message.id)
        self.assertEqual(len(list((self.bus.agents_dir / "beta" / "ready").glob("*.json"))), 1)
        with self.assertRaises(TaskTreeError):
            self.forest.dispatch_leaf("first", "alpha")

    def test_disabled_observer_and_unknown_executor_are_refused(self):
        self.tree()
        for agent in ("disabled", "observer", "missing"):
            with self.subTest(agent=agent), self.assertRaises(TaskTreeError):
                self.forest.claim_leaf("first", agent)
        self.assertIsNone(self.leaf()["task_id"])

    def test_dependencies_require_formal_results(self):
        self.tree(second=True)
        with self.assertRaises(TaskTreeError):
            self.forest.claim_leaf("second", "alpha")
        first = self.forest.claim_leaf("first", "alpha")
        self.bus.finish("alpha", first, "done", ticket=captured_ticket(first))
        self.assertEqual(self.leaf()["state"], "awaiting_receipt")
        self.assertFalse(self.leaf("second")["claimable"])
        self.assertEqual(self.forest.snapshot()["branches"][0]["state"], "blocked")

    def test_successful_result_unblocks_dependency_but_does_not_imply_publication(self):
        self.tree(second=True)
        first = self.forest.claim_leaf("first", "alpha")
        receipt = self.finish(first)
        self.assertEqual(self.leaf()["state"], "delivered")
        self.assertEqual(self.leaf()["result_id"], receipt.id)
        self.assertTrue(self.leaf("second")["claimable"])
        self.assertFalse(self.forest.snapshot()["delivery_is_publication"])
        self.assertIsNotNone(self.forest.claim_leaf("second", "beta"))

    def test_snapshot_scans_each_result_recipient_once_for_multiple_delivered_leaves(self):
        self.tree(second=True)
        first = self.forest.claim_leaf("first", "alpha")
        self.finish(first)
        second = self.forest.claim_leaf("second", "beta")
        self.finish(second)

        with patch.object(self.bus, "index_results", wraps=self.bus.index_results) as index_results:
            view = self.forest.snapshot()

        self.assertEqual([node["state"] for node in view["leaves"]], ["delivered", "delivered"])
        self.assertEqual(index_results.call_count, 1)

    def test_explicit_valid_heartbeat_is_required_for_running(self):
        self.tree()
        first = self.forest.claim_leaf("first", "alpha")
        self.assertEqual(self.leaf()["state"], "claimed")
        path = self.bus.agents_dir / "alpha" / "processing" / (first.id + ".json")
        os.utime(path, None)
        self.assertEqual(self.leaf()["state"], "claimed")
        self.bus.heartbeat("alpha", first, captured_ticket(first), note="actual checkpoint")
        self.assertEqual(self.leaf()["state"], "running")
        self.assertFalse(self.leaf()["claimable"])

    def test_stale_heartbeat_does_not_authorize_takeover(self):
        self.tree()
        first = self.forest.claim_leaf("first", "alpha")
        self.bus.heartbeat("alpha", first, captured_ticket(first))
        with patch("agentbus.heartbeat.time.time", return_value=time.time() + 1000):
            self.assertEqual(self.leaf()["state"], "claimed")
            with self.assertRaises(TaskTreeError):
                self.forest.claim_leaf("first", "beta")
            with self.assertRaises(TaskTreeError):
                self.forest.handoff_leaf("first", "beta", reason="stale", successor="beta")

    def test_stop_denies_claim_and_preserves_existing_checkpoint(self):
        self.tree()
        before = self.forest.path.read_bytes()
        (self.bus.root / "STOP").write_text("owner stopped", encoding="utf-8")
        with self.assertRaises(TaskTreeError):
            self.forest.claim_leaf("first", "alpha")
        self.assertEqual(before, self.forest.path.read_bytes())
        self.assertEqual(self.leaf()["state"], "stopped")

    def test_checkpoint_requires_original_ticket_and_keeps_three_part_record(self):
        self.tree()
        first = self.forest.claim_leaf("first", "alpha")
        with self.assertRaises((BusError, TaskTreeError)):
            self.forest.checkpoint_leaf("first", "alpha", "resume here")
        view = self.forest.checkpoint_leaf("first", "alpha", "Continue at output line 4", captured_ticket(first))
        self.assertEqual(view["handoff"], "Continue at output line 4")
        self.assertTrue(all(isinstance(view[key], str) for key in ("goal", "results", "handoff")))

    def test_checkpoint_persists_optional_progress_percentage(self):
        self.tree()
        first = self.forest.claim_leaf("first", "alpha")
        view = self.forest.checkpoint_leaf(
            "first", "alpha", "Implemented the parser; next add edge cases",
            captured_ticket(first), progress=45)
        self.assertEqual(view["handoff"], "Implemented the parser; next add edge cases")
        self.assertEqual(view["progress_percent"], 45)

    def test_checkpoint_helper_exposes_optional_progress_percentage(self):
        self.tree()
        checkpoint_leaf(self.cfg, "first", "alpha", "Saved checkpoint", progress=45)
        self.assertEqual(self.leaf()["progress_percent"], 45)

    def test_checkpoint_rejects_invalid_progress_without_mutating_tree(self):
        self.tree()
        first = self.forest.claim_leaf("first", "alpha")
        before = self.forest.path.read_bytes()
        with self.assertRaises(TaskTreeError):
            self.forest.checkpoint_leaf("first", "alpha", "invalid progress",
                                         captured_ticket(first), progress=101)
        self.assertEqual(before, self.forest.path.read_bytes())

    def test_forged_result_ticket_does_not_complete_done_record(self):
        self.tree()
        first = self.forest.claim_leaf("first", "alpha")
        self.bus.finish("alpha", first, "done", ticket=captured_ticket(first))
        result = self.bus.make("alpha", "codex", "result", reply_to=first.id, body="self report",
                               job_id=first.job_id, plan_id=first.plan_id, plan_sha256=first.plan_sha256)
        result.source_claim_epoch = first.claim_epoch
        result.source_claim_id = "0" * 32
        with self.assertRaises(LateResultRejected):
            self.bus.post(result)
        # A hostile file writer can bypass the API; projection must still
        # reject its claim binding instead of trusting the JSON's existence.
        forged_path = self.bus.dirs("codex")["ready"] / (result.id + ".json")
        forged_path.write_text(json.dumps(result.to_dict()), encoding="utf-8")
        self.assertEqual(self.leaf()["state"], "awaiting_receipt")
        self.assertFalse(self.leaf()["claimable"])

    def test_artifact_mutation_preserves_delivery_and_marks_current_artifact_drift(self):
        tree = self.tree()
        tree["leaves"][0]["expected_artifacts"] = ["output.txt"]
        self.forest.replace_structure(tree)
        output = self.cfg.output_root / "output.txt"
        output.write_bytes(b"actual artifact")
        first = self.forest.claim_leaf("first", "alpha")
        manifest = [{"path": "output.txt", "bytes": output.stat().st_size,
                     "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}]
        self.finish(first, artifacts=manifest)
        self.assertEqual(self.leaf()["state"], "delivered")
        output.write_bytes(b"modified")
        leaf = self.leaf()
        self.assertEqual(leaf["state"], "delivered")
        self.assertEqual(leaf["receipt_state"], "valid")
        self.assertEqual(leaf["artifact_state"], "mismatch")
        self.assertIn("CONTENT_MISMATCH", leaf["evidence_error"])

    def test_artifact_drift_keeps_historical_receipt_and_manifest_visible(self):
        tree = self.tree()
        tree["leaves"][0]["expected_artifacts"] = ["output.txt"]
        self.forest.replace_structure(tree)
        output = self.cfg.output_root / "output.txt"
        output.write_bytes(b"original artifact")
        first = self.forest.claim_leaf("first", "alpha")
        manifest = [{"path": "output.txt", "bytes": output.stat().st_size,
                     "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}]
        receipt = self.finish(first, artifacts=manifest)

        output.write_bytes(b"changed artifact")
        leaf = self.leaf()

        self.assertEqual(leaf["state"], "delivered")
        self.assertEqual(leaf["result_id"], receipt.id)
        self.assertEqual(leaf["artifacts"], manifest)
        self.assertEqual(leaf["receipt_state"], "valid")
        self.assertEqual(leaf["artifact_state"], "mismatch")
        self.assertIn("CONTENT_MISMATCH", leaf["evidence_error"])
        self.assertIn(receipt.id, leaf["results"])
        self.assertIn("当前产物", leaf["results"])

    def test_artifact_drift_blocks_dependent_task_without_erasing_delivery(self):
        tree = self.tree(second=True)
        tree["leaves"][0]["expected_artifacts"] = ["output.txt"]
        self.forest.replace_structure(tree)
        output = self.cfg.output_root / "output.txt"
        output.write_bytes(b"original artifact")
        first = self.forest.claim_leaf("first", "alpha")
        manifest = [{"path": "output.txt", "bytes": output.stat().st_size,
                     "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}]
        self.finish(first, artifacts=manifest)
        output.write_bytes(b"changed artifact")

        first_view = self.leaf("first")
        second_view = self.leaf("second")
        self.assertEqual(first_view["state"], "delivered")
        self.assertFalse(first_view["dependency_ready"])
        self.assertEqual(second_view["state"], "blocked")
        self.assertEqual(second_view["unmet_dependencies"], ["first"])
        self.assertFalse(second_view["claimable"])
        with self.assertRaises(TaskTreeError):
            self.forest.claim_leaf("second", "beta")

    def test_archived_or_renamed_output_preserves_receipt_but_blocks_dependency(self):
        tree = self.tree(second=True)
        tree["leaves"][0]["expected_artifacts"] = ["output.txt"]
        self.forest.replace_structure(tree)
        output = self.cfg.output_root / "output.txt"
        output.write_bytes(b"original artifact before archival")
        first = self.forest.claim_leaf("first", "alpha")
        manifest = [{"path": output.name, "bytes": output.stat().st_size,
                     "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}]
        receipt = self.finish(first, artifacts=manifest)

        archive = self.cfg.output_root / "archive"
        archive.mkdir()
        archived = archive / "output.deprecated_old_version.txt"
        output.rename(archived)
        self.assertEqual(hashlib.sha256(archived.read_bytes()).hexdigest(), manifest[0]["sha256"])

        delivered = self.leaf("first")
        dependent = self.leaf("second")
        self.assertEqual(delivered["state"], "delivered")
        self.assertEqual(delivered["result_id"], receipt.id)
        self.assertEqual(delivered["artifacts"], manifest)
        self.assertEqual(delivered["receipt_state"], "valid")
        self.assertEqual(delivered["artifact_state"], "mismatch")
        self.assertFalse(delivered["dependency_ready"])
        self.assertEqual(dependent["state"], "blocked")
        self.assertFalse(dependent["claimable"])
        self.assertIn("NOT_FOUND", delivered["evidence_error"])

    def test_branch_dependency_requires_currently_valid_descendant_artifacts(self):
        tree = {
            "schema_version": 1,
            "roots": [{"id": "project", "parent_id": None, "title": "Project"}],
            "branches": [
                {"id": "producer", "parent_id": "project", "title": "Producer"},
                {"id": "consumer", "parent_id": "project", "title": "Consumer",
                 "dependencies": ["producer"]},
            ],
            "leaves": [
                {"id": "source", "parent_id": "producer", "title": "Source",
                 "expected_artifacts": ["output.txt"]},
                {"id": "followup", "parent_id": "consumer", "title": "Follow-up"},
            ],
        }
        self.forest.replace_structure(tree)
        output = self.cfg.output_root / "output.txt"
        output.write_bytes(b"original artifact")
        source = self.forest.claim_leaf("source", "alpha")
        manifest = [{"path": "output.txt", "bytes": output.stat().st_size,
                     "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}]
        self.finish(source, artifacts=manifest)
        output.write_bytes(b"changed artifact")

        view = self.forest.snapshot()
        producer = next(node for node in view["branches"] if node["id"] == "producer")
        followup = next(node for node in view["leaves"] if node["id"] == "followup")
        self.assertEqual(producer["state"], "delivered")
        self.assertFalse(producer["dependency_ready"])
        self.assertEqual(followup["state"], "blocked")
        self.assertEqual(followup["unmet_dependencies"], ["producer"])

    def test_optional_artifact_manifest_mutation_keeps_delivery_and_flags_mismatch(self):
        self.tree()
        output = self.cfg.output_root / "optional-output.txt"
        output.write_bytes(b"optional but verifiable output")
        first = self.forest.claim_leaf("first", "alpha")
        manifest = [{"path": output.name, "bytes": output.stat().st_size,
                     "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}]
        receipt = self.finish(first, artifacts=manifest)
        self.assertEqual(self.leaf()["state"], "delivered")

        receipt_path = self.bus.dirs("codex")["ready"] / (receipt.id + ".json")
        saved = json.loads(receipt_path.read_text(encoding="utf-8"))
        saved["artifacts"][0]["sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(saved), encoding="utf-8")

        leaf = self.leaf()
        self.assertEqual(leaf["state"], "delivered")
        self.assertEqual(leaf["receipt_state"], "valid")
        self.assertEqual(leaf["artifact_state"], "mismatch")
        self.assertFalse(leaf["dependency_ready"])

    def test_structure_rejects_status_injection_and_binding_replacement(self):
        tree = self.tree()
        bad = copy.deepcopy(tree)
        bad["leaves"][0]["state"] = "published"
        with self.assertRaises(TaskTreeError):
            self.forest.replace_structure(bad)
        self.forest.claim_leaf("first", "alpha")
        with self.assertRaises(TaskTreeError):
            self.forest.replace_structure(tree)

    def test_dependency_cycle_and_unknown_parent_are_rejected(self):
        tree = self.tree(second=True)
        tree["leaves"][0]["dependencies"] = ["second"]
        with self.assertRaises(TaskTreeError):
            self.forest.replace_structure(tree)
        tree["leaves"][0]["dependencies"] = []
        tree["leaves"][1]["parent_id"] = "missing"
        with self.assertRaises(TaskTreeError):
            self.forest.replace_structure(tree)

    def test_claim_repairs_crash_between_binding_and_bus_post(self):
        self.tree()
        job = jobs.start_job(self.cfg, "codex", "Ship actual outputs", request_id="prepared")
        structure = json.loads(self.forest.path.read_text(encoding="utf-8"))
        structure["roots"][0]["job_id"] = job["id"]
        self.forest.replace_structure(structure)
        with patch.object(Bus, "post", side_effect=OSError("simulated publication failure")):
            with self.assertRaises(OSError):
                self.forest.claim_leaf("first", "alpha")
        allocated = self.leaf()
        self.assertTrue(allocated["claimable"])
        self.assertEqual(allocated["location"], "missing")
        claimed = self.forest.claim_leaf("first", "alpha")
        self.assertEqual(claimed.id, allocated["task_id"])

    def test_claim_repairs_crash_after_job_allocation_before_structure_write(self):
        self.tree()
        job = jobs.start_job(self.cfg, "codex", "Ship actual outputs", request_id="prepared")
        structure = json.loads(self.forest.path.read_text(encoding="utf-8"))
        structure["roots"][0]["job_id"] = job["id"]
        self.forest.replace_structure(structure)
        with patch.object(self.forest, "_write", side_effect=OSError("simulated binding write failure")):
            with self.assertRaises(OSError):
                self.forest.claim_leaf("first", "alpha")
        allocated = self.leaf()
        self.assertEqual(len(self.forest.snapshot()["leaves"]), 1)
        self.assertTrue(allocated["claimable"])
        claimed = self.forest.claim_leaf("first", "alpha")
        self.assertEqual(claimed.id, allocated["task_id"])
        self.assertEqual(len(jobs._load(self.cfg, job["id"])["tasks"]), 1)

    def test_new_formal_job_task_appears_as_leaf_without_read_writing(self):
        self.tree()
        first = self.forest.claim_leaf("first", "alpha")
        job = jobs._load(self.cfg, first.job_id)
        message = self.bus.make("codex", "beta", "task", title="New requirement", body="Implement a new request",
                                result_to="codex", reply_to=job["goal_id"], deadline_ts=first.deadline_ts,
                                **jobs.job_binding(job))
        jobs.register_job_tasks(self.cfg, "codex", job["id"], [message])
        self.bus.post(message)
        before = self.forest.path.read_bytes()
        leaves = self.forest.snapshot()["leaves"]
        self.assertEqual(len(leaves), 2)
        self.assertIn(message.id, [node["task_id"] for node in leaves])
        self.assertEqual(before, self.forest.path.read_bytes())

    @unittest.skipUnless(os.name == "nt", "explicit Bus release is supported on Windows")
    def test_original_owner_handoff_preserves_intent_and_rejects_old_ticket(self):
        self.tree()
        original = self.forest.claim_leaf("first", "alpha")
        ticket = captured_ticket(original)
        job_before = jobs._load(self.cfg, original.job_id)
        continuation = self.forest.handoff_leaf("first", "alpha", ticket=ticket,
                                                reason="quota exhausted; continue from output", successor="beta")
        self.assertNotEqual(continuation["task_id"], original.id)
        with self.assertRaises(TaskTreeError):
            self.forest.handoff_leaf("first", "alpha", ticket=ticket,
                                    reason="quota exhausted; continue from output", successor="beta")
        self.assertEqual(self.leaf()["task_id"], continuation["task_id"])
        old = self.bus.find_message(original.id)
        self.assertEqual(old.status, "blocked")
        self.assertFalse(old.claim_id)
        with self.assertRaises((LateResultRejected, BusError)):
            self.bus.commit_result("alpha", original, ticket, body="late old output")
        new = self.forest.claim_leaf("first", "beta")
        self.assertEqual(new.body, original.body)
        self.assertEqual(new.deadline_ts, original.deadline_ts)
        self.assertEqual(new.attempt, original.attempt + 1)
        self.assertEqual(jobs._load(self.cfg, original.job_id)["tasks"][0], job_before["tasks"][0])
        self.finish(new)
        self.assertEqual(self.leaf()["state"], "delivered")
        formal = jobs.status_job(self.cfg, original.job_id)
        old_row = next(item for item in formal["tasks"] if item["id"] == original.id)
        self.assertEqual(old_row["status"], "transferred")
        self.assertEqual(formal["state"], "awaiting_summary")

    @unittest.skipUnless(os.name == "nt", "explicit Bus release is supported on Windows")
    def test_released_leaf_is_claimable_by_any_executor_without_resetting_deadline(self):
        self.tree()
        original = self.forest.claim_leaf("first", "alpha")
        released = self.forest.handoff_leaf("first", "alpha", ticket=captured_ticket(original),
                                            reason="Saved checkpoint; pick any available executor")
        self.assertIsNone(released["task_id"])
        self.assertIn("beta", released["eligible_agents"])
        replacement = self.forest.claim_leaf("first", "beta")
        self.assertEqual(replacement.deadline_ts, original.deadline_ts)
        self.assertEqual(replacement.attempt, original.attempt + 1)

    @unittest.skipUnless(os.name == "nt", "explicit Bus release is supported on Windows")
    def test_released_leaf_cannot_reset_an_expired_predecessor_deadline(self):
        self.tree()
        original = self.forest.claim_leaf("first", "alpha")
        self.forest.handoff_leaf("first", "alpha", ticket=captured_ticket(original), reason="continue later")
        with patch("agentbus.task_tree.time.time", return_value=original.deadline_ts + 1):
            self.assertFalse(self.leaf()["claimable"])
            with self.assertRaises(TaskTreeError):
                self.forest.claim_leaf("first", "beta")

    @unittest.skipUnless(os.name == "nt", "explicit Bus release is supported on Windows")
    def test_successor_binding_audit_is_repaired_before_claim(self):
        self.tree()
        original = self.forest.claim_leaf("first", "alpha")
        audit = self.bus.audit
        def fail_binding(**kwargs):
            if kwargs.get("event") == "task_tree_successor_bound":
                raise OSError("simulated audit failure")
            return audit(**kwargs)
        with patch.object(self.bus, "audit", side_effect=fail_binding):
            with self.assertRaises(OSError):
                self.forest.handoff_leaf("first", "alpha", ticket=captured_ticket(original),
                                        reason="continue checkpoint", successor="beta")
        replacement = self.forest.claim_leaf("first", "beta")
        self.finish(replacement)
        old_row = next(item for item in jobs.status_job(self.cfg, original.job_id)["tasks"] if item["id"] == original.id)
        self.assertEqual(old_row["status"], "transferred")

    @unittest.skipUnless(os.name == "nt", "explicit Bus release is supported on Windows")
    def test_confirmed_inactive_handoff_is_explicit_and_new_ticket_is_required(self):
        self.tree()
        original = self.forest.claim_leaf("first", "alpha")
        self.forest.handoff_leaf("first", "beta", reason="owner explicitly confirmed stopped",
                                successor="beta", owner_confirmed_inactive=True, expected_task_id=original.id, expected_owner="alpha", expected_claim_epoch=original.claim_epoch)
        new = self.forest.claim_leaf("first", "beta")
        with self.assertRaises((LateResultRejected, BusError)):
            self.bus.commit_result("beta", new, captured_ticket(original), body="wrong claim")
        self.finish(new)
        self.assertEqual(self.leaf()["state"], "delivered")

    def test_required_capabilities_do_not_depend_on_brand(self):
        tree = self.tree()
        tree["leaves"][0]["required_capabilities"] = ["edit"]
        self.cfg.data["workers"]["beta"]["capabilities"] = ["edit"]
        self.forest.replace_structure(tree)
        self.assertEqual(self.leaf()["eligible_agents"], ["beta"])
        with self.assertRaises(TaskTreeError):
            self.forest.claim_leaf("first", "alpha")

    def test_seed_leaf_persists_a_project_owned_node(self):
        seed_leaf(self.cfg, {"id": "new-leaf", "title": "Any available member", "goal": "Do useful work"},
                  branch_id="new-feature", project_goal="Ship the project")
        view = self.forest.snapshot()
        self.assertEqual(view["leaves"][0]["parent_id"], "new-feature")
        self.assertEqual(view["leaves"][0]["owner"], "")

    def test_product_seed_imports_eight_bindings_five_branches_without_pretending_done(self):
        from agentbus import task_tree
        job = jobs.start_job(self.cfg, "alpha", "Converge the product", request_id="seed")
        plan = [{"worker": "alpha", "title": f"Delivery {index}", "body": f"Real work {index}",
                 "result_to": "codex"} for index in range(8)]
        dispatched = jobs.dispatch_job(self.cfg, "codex", job["id"], plan)
        mapping = tuple((entry[0], entry[1], entry[2], dispatched["tasks"][index]["id"])
                        for index, entry in enumerate(task_tree._PRODUCT_TASKS))
        with patch.object(task_tree, "_PRODUCT_JOB", job["id"]), patch.object(task_tree, "_PRODUCT_TASKS", mapping):
            view = self.forest.ensure_product_forest()
        self.assertEqual(len(view["branches"]), 5)
        self.assertEqual(len(view["leaves"]), 10)
        self.assertTrue(all(node["state"] != "delivered" for node in view["leaves"]))
        self.assertEqual(sum(node["task_id"] is not None for node in view["leaves"]), 8)
        self.assertEqual(self.leaf("CX-INTEGRATE")["owner"], "")
        self.assertIn("beta", self.leaf("CX-INTEGRATE")["eligible_agents"])

    def test_two_processes_have_exactly_one_claim_winner(self):
        self.tree()
        code = """
import json, sys
from agentbus.config import load
from agentbus.task_tree import claim_leaf
print('READY', flush=True)
sys.stdin.readline()
try:
    claimed = claim_leaf(load(sys.argv[1]), 'first', sys.argv[2])
    print(json.dumps({'winner': bool(claimed), 'agent': sys.argv[2]}), flush=True)
except Exception as exc:
    print(json.dumps({'winner': False, 'error': type(exc).__name__}), flush=True)
"""
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        processes = [subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-c", code,
                                       str(self.cfg.workspace), agent], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                      encoding="utf-8", env=env) for agent in ("alpha", "beta")]
        try:
            for process in processes:
                self.assertEqual(process.stdout.readline().strip(), "READY")
            for process in processes:
                process.stdin.write("go\n")
                process.stdin.flush()
                process.stdin.close()
                process.stdin = None
            outcomes = []
            for process in processes:
                out, err = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, err)
                outcomes.append(json.loads(out))
            self.assertEqual(sum(outcome["winner"] for outcome in outcomes), 1)
            self.assertEqual(len(jobs._load(self.cfg, self.bus.find_message(self.leaf()["task_id"]).job_id)["tasks"]), 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
