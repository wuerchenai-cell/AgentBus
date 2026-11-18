"""2026-11-04 remediation: real receipts for dependent revisions of one file."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus import jobs
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.task_tree import TaskForest, TaskTreeError


class ArtifactVersionRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="artifact-revisions-")
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name)
        self.cfg = load(self.ws, create=True)
        self.cfg.data["workers"].update({
            "alpha": {"enabled": True, "executor_enabled": True, "can_spawn": False, "kind": "gui"},
            "beta": {"enabled": True, "executor_enabled": True, "can_spawn": False, "kind": "gui"},
        })
        self.cfg = save(self.cfg)
        self.forest = TaskForest(self.cfg)
        self.bus = self.forest.bus
        self.bus.init()
        self.notify = patch("agentbus.trigger.service.TriggerDeliveryService.trigger_for_coordinator",
                            return_value={"suppressed": "local test"})
        self.notify.start()
        self.addCleanup(self.notify.stop)

    def tree(self, *, dependent=True, distinct=False):
        self.forest.replace_structure({
            "schema_version": 1,
            "roots": [{"id": "project", "parent_id": None, "title": "Versions", "goal": "Real work"}],
            "branches": [{"id": "source", "parent_id": "project", "title": "Source"},
                         {"id": "other", "parent_id": "project", "title": "Other"}],
            "leaves": [
                {"id": "create", "parent_id": "source", "title": "Create module", "goal": "Write v1",
                 "expected_artifacts": ["module.py"]},
                {"id": "improve", "parent_id": "source" if dependent else "other", "title": "Improve module",
                 "goal": "Write v2", "dependencies": ["create"] if dependent else [],
                 "expected_artifacts": ["second.py" if distinct else "module.py"]},
                {"id": "accept", "parent_id": "source", "title": "Accept", "goal": "Check both",
                 "dependencies": ["create", "improve"]},
            ],
        })

    def finish(self, leaf, version=None, path="module.py"):
        source = self.forest.claim_leaf(leaf, "alpha")
        ticket = captured_ticket(source)
        self.bus.heartbeat("alpha", source, ticket, note="Actual local change")
        artifacts = []
        if version is not None:
            file = self.cfg.output_root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            raw = f"VERSION = {version}\n".encode()
            file.write_bytes(raw)
            artifacts = [{"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}]
        result = self.bus.commit_result("alpha", source, ticket, body=f"Executed {leaf}", artifacts=artifacts, exit_code=0)
        return source, result

    def leaves(self):
        return {n["id"]: n for n in self.forest.snapshot()["leaves"]}

    def revisions(self):
        self.tree()
        a, ra = self.finish("create", 1)
        original = (self.bus.dirs(ra.to)["ready"] / (ra.id + ".json")).read_bytes()
        b, rb = self.finish("improve", 2)
        return a, ra, b, rb, original

    def test_dependent_file_revisions_unlock_final_claim_and_preserve_old_receipt(self):
        a, ra, b, rb, original = self.revisions()
        leaves = self.leaves()
        self.assertEqual(leaves["create"]["artifact_state"], "superseded")
        self.assertEqual(leaves["improve"]["artifact_state"], "verified")
        self.assertTrue(leaves["accept"]["claimable"])
        self.assertEqual((self.bus.dirs(ra.to)["ready"] / (ra.id + ".json")).read_bytes(), original)
        self.assertEqual(self.forest.claim_next("alpha")["leaf"]["id"], "accept")
        self.assertFalse(jobs._artifact_error(self.cfg, {"id": a.id, "expected_artifacts": ["module.py"]}, ra))

    def test_latest_tampering_blocks_instead_of_being_laundered_by_history(self):
        self.revisions()
        (self.ws / "module.py").write_text("UNAUTHORIZED = True\n", encoding="utf-8")
        leaves = self.leaves()
        self.assertFalse(leaves["accept"]["claimable"])
        self.assertEqual(leaves["improve"]["artifact_state"], "mismatch")
        self.assertEqual(leaves["create"]["artifact_state"], "mismatch")
        with self.assertRaises(TaskTreeError):
            self.forest.claim_leaf("accept", "alpha")
        roots = self.forest.diagnose_deadlock()["root_blockers"]
        self.assertIn("create", [n["id"] for n in roots])
        self.assertNotIn("accept", [n["id"] for n in roots])

    def test_swapping_back_to_v1_cannot_verify_both_historical_hashes(self):
        self.revisions()
        (self.ws / "module.py").write_bytes(b"VERSION = 1\n")
        leaves = self.leaves()
        self.assertFalse(leaves["accept"]["claimable"])
        self.assertEqual(leaves["improve"]["artifact_state"], "mismatch")
        self.assertFalse(leaves["create"]["dependency_ready"])

    def test_independent_other_branch_receipt_cannot_replace_history(self):
        self.tree(dependent=False)
        self.finish("create", 1)
        self.finish("improve", 2)
        leaves = self.leaves()
        self.assertEqual(leaves["create"]["artifact_state"], "mismatch")
        self.assertFalse(leaves["accept"]["claimable"])

    def test_changed_old_receipt_is_rejected_even_if_rewritten_to_latest_hash(self):
        a, ra, b, rb, _ = self.revisions()
        path = self.bus.dirs(ra.to)["ready"] / (ra.id + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["artifacts"] = rb.artifacts
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(self.leaves()["accept"]["claimable"])
        self.assertEqual(self.leaves()["create"]["artifact_state"], "mismatch")

    def test_bad_successor_ticket_does_not_replace_history(self):
        a, ra, b, rb, _ = self.revisions()
        path = self.bus.dirs(rb.to)["ready"] / (rb.id + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source_claim_id"] = "forged"
        path.write_text(json.dumps(data), encoding="utf-8")
        leaves = self.leaves()
        self.assertEqual(leaves["create"]["artifact_state"], "mismatch")
        self.assertFalse(leaves["accept"]["claimable"])

    def test_current_bytes_alone_do_not_prove_missing_successor_receipt(self):
        a, ra, b, rb, _ = self.revisions()
        (self.bus.dirs(rb.to)["ready"] / (rb.id + ".json")).unlink()
        leaves = self.leaves()
        self.assertEqual(leaves["create"]["artifact_state"], "mismatch")
        self.assertFalse(leaves["accept"]["claimable"])

    def test_delivered_leaf_has_no_missing_worker_block_reason(self):
        self.tree()
        self.finish("create", 1)
        self.assertIsNone(self.leaves()["create"]["blocked_reason"])

    def test_empty_artifacts_are_not_required_but_can_satisfy_dependencies(self):
        self.tree(distinct=True)
        self.finish("create", 1)
        self.finish("improve", 2, path="second.py")
        self.finish("accept")
        leaf = self.leaves()["accept"]
        self.assertEqual(leaf["artifact_state"], "not_required")
        self.assertTrue(leaf["dependency_ready"])

    def test_distinct_files_keep_positive_two_leaf_delivery(self):
        self.tree(distinct=True)
        self.finish("create", 1)
        self.finish("improve", 2, path="second.py")
        leaves = self.leaves()
        self.assertEqual(leaves["create"]["artifact_state"], "verified")
        self.assertEqual(leaves["improve"]["artifact_state"], "verified")
        self.assertTrue(leaves["accept"]["claimable"])

    def test_three_revisions_follow_the_latest_committed_version(self):
        self.revisions()
        self.forest.seed_leaf({"id": "polish", "parent_id": "source", "title": "Polish", "goal": "Write v3",
                               "dependencies": ["improve"], "expected_artifacts": ["module.py"]})
        self.finish("polish", 3)
        leaves = self.leaves()
        self.assertEqual(leaves["create"]["artifact_state"], "superseded")
        self.assertEqual(leaves["improve"]["artifact_state"], "superseded")
        self.assertEqual(leaves["polish"]["artifact_state"], "verified")
        self.assertTrue(leaves["accept"]["claimable"])

    def test_parallel_successors_of_one_file_are_an_unresolved_conflict(self):
        self.tree()
        self.finish("create", 1)
        # Both tasks are legitimately allocated from v1, without a dependency
        # between them. Neither is authorized to overwrite the other's result.
        b = self.forest.claim_leaf("improve", "alpha")
        self.forest.seed_leaf({"id": "sibling", "parent_id": "other", "title": "Sibling", "goal": "Other edit",
                               "dependencies": ["create"], "expected_artifacts": ["module.py"]})
        sibling = self.forest.claim_leaf("sibling", "beta")
        for source, version in [(b, 2), (sibling, 3)]:
            ticket = captured_ticket(source)
            self.bus.heartbeat(source.to, source, ticket, note="Actual local edit")
            raw = f"VERSION = {version}\n".encode()
            (self.ws / "module.py").write_bytes(raw)
            self.bus.commit_result(source.to, source, ticket, body="Local edit", artifacts=[
                {"path": "module.py", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}], exit_code=0)
        leaves = self.leaves()
        self.assertEqual(leaves["create"]["artifact_state"], "mismatch")
        self.assertFalse(leaves["accept"]["claimable"])

    def test_foreign_workspace_version_record_cannot_authorize_replacement(self):
        a, ra, b, rb, _ = self.revisions()
        path = self.ws / ".agentbus" / "artifact-versions" / (b.id + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["workspace"] = str(self.ws / "foreign")
        path.write_text(json.dumps(data), encoding="utf-8")
        leaves = self.leaves()
        self.assertEqual(leaves["create"]["artifact_state"], "mismatch")
        self.assertFalse(leaves["accept"]["claimable"])

    def test_changing_successor_dependency_refs_after_allocation_is_rejected(self):
        a, ra, b, rb, _ = self.revisions()
        path = self.bus.dirs(b.to)["done"] / (b.id + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["refs"] = []
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(self.leaves()["accept"]["claimable"])

    def test_version_record_retry_is_idempotent_and_conflict_preserves_history(self):
        from agentbus.artifact_versions import record_delivery
        from agentbus.bus import BusError
        self.tree()
        source, result = self.finish("create", 1)
        source = self.bus.find_message(source.id)
        path = self.ws / ".agentbus" / "artifact-versions" / (source.id + ".json")
        original = path.read_bytes()
        record_delivery(self.bus, source, result)
        self.assertEqual(path.read_bytes(), original)
        changed = json.loads(original)
        changed["receipt_sha256"] = "0" * 64
        path.write_text(json.dumps(changed), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(BusError):
            record_delivery(self.bus, source, result)
        self.assertEqual(path.read_bytes(), before)

    def test_receipt_mailbox_lifecycle_does_not_invalidate_version_binding(self):
        a, ra, b, rb, _ = self.revisions()
        receipt = self.bus.claim(ra.to, self.bus.dirs(ra.to)["ready"] / (ra.id + ".json"))
        self.bus.finish(ra.to, receipt, "done", ticket=captured_ticket(receipt))
        self.assertTrue(self.leaves()["accept"]["claimable"])

    def test_bare_legacy_bus_without_project_config_still_delivers_artifact(self):
        from agentbus.bus import Bus
        raw = b"legacy artifact"
        other = self.ws / "bare"
        other.mkdir()
        (other / "file.txt").write_bytes(raw)
        bus = Bus(other)
        bus.init()
        source = bus.make("codex", "alpha", "task", title="Legacy task", result_to="codex")
        bus.post(source)
        source = bus.claim("alpha", bus.dirs("alpha")["ready"] / (source.id + ".json"))
        ticket = captured_ticket(source)
        bus.heartbeat("alpha", source, ticket, note="Legacy local worker")
        receipt = bus.commit_result("alpha", source, ticket, body="Delivered", artifact_root=other,
                                    artifacts=[{"path": "file.txt", "bytes": len(raw),
                                                "sha256": hashlib.sha256(raw).hexdigest()}])
        self.assertEqual(bus.find_message(source.id).status, "done")
        self.assertEqual(receipt.reply_to, source.id)

    def test_missing_config_cannot_downgrade_a_version_bound_delivery(self):
        from agentbus.artifact_versions import record_delivery
        from agentbus.bus import BusError
        a, ra, b, rb, _ = self.revisions()
        (self.ws / ".agentbus" / "config.json").unlink()
        with self.assertRaises(BusError):
            record_delivery(self.bus, b, rb)

    def test_version_record_write_failure_leaves_retriable_formal_receipt_without_done(self):
        from agentbus.bus import BusError
        self.tree()
        source = self.forest.claim_leaf("create", "alpha")
        ticket = captured_ticket(source)
        self.bus.heartbeat("alpha", source, ticket, note="Actual local edit")
        raw = b"VERSION = 1\n"
        (self.ws / "module.py").write_bytes(raw)
        artifacts = [{"path": "module.py", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}]
        with patch("agentbus.artifact_versions.os.link", side_effect=OSError("injected version write failure")):
            with self.assertRaises(BusError):
                self.bus.commit_result("alpha", source, ticket, body="Actual edit", artifacts=artifacts)
        self.assertTrue((self.bus.dirs("alpha")["processing"] / (source.id + ".json")).is_file())
        self.assertFalse((self.bus.dirs("alpha")["done"] / (source.id + ".json")).exists())
        pending = self.bus.find_results(source.id)
        self.assertEqual(len(pending), 1)
        retried = self.bus.commit_result("alpha", source, ticket, body="Actual edit", artifacts=artifacts)
        self.assertEqual(retried.id, pending[0].id)
        self.assertEqual(self.bus.find_message(source.id).status, "done")
        self.assertTrue(self.leaves()["improve"]["claimable"])

    def test_branch_inherited_dependency_authorizes_only_its_real_revision(self):
        self.tree()
        structure = json.loads(self.forest.path.read_text(encoding="utf-8"))
        structure["branches"][1]["dependencies"] = ["create"]
        structure["leaves"][1]["parent_id"] = "other"
        structure["leaves"][1]["dependencies"] = []
        self.forest.replace_structure(structure)
        self.finish("create", 1)
        self.finish("improve", 2)
        self.assertEqual(self.leaves()["create"]["artifact_state"], "superseded")
        self.assertTrue(self.leaves()["accept"]["claimable"])

    def test_cyclic_topology_is_rejected_before_it_can_create_version_edges(self):
        self.tree()
        structure = json.loads(self.forest.path.read_text(encoding="utf-8"))
        structure["leaves"][0]["dependencies"] = ["improve"]
        before = self.forest.path.read_bytes()
        with self.assertRaises(TaskTreeError):
            self.forest.replace_structure(structure)
        self.assertEqual(self.forest.path.read_bytes(), before)

    def test_worker_loop_accepts_real_text_delivery_without_claiming_artifact_verification(self):
        from agentbus.worker_loop import WorkerLoop
        self.revisions()

        def handler(leaf):
            source = self.bus.find_message(leaf["task_id"])
            self.bus.commit_result("alpha", source, leaf["claim_ticket"], body="Actual final review",
                                   artifacts=[], exit_code=0)
            return {"status": "ok"}

        loop = WorkerLoop(self.cfg, "alpha", handler=handler)
        record = loop.execute_task(self.leaves()["accept"])
        self.assertTrue(record["success"], record)
        self.assertEqual(self.leaves()["accept"]["artifact_state"], "not_required")

    def test_headless_monitor_accepts_real_text_receipt_and_local_process_exit(self):
        from agentbus.headless_spawn import HeadlessSpawnEngine, SpawnResult
        import subprocess
        import threading
        import time
        self.revisions()
        source, receipt = self.finish("accept")
        ticket = captured_ticket(source)
        engine = HeadlessSpawnEngine(self.cfg)
        process = subprocess.Popen([sys.executable, "-c", "pass"],
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        info = {"proc": process, "pid": process.pid, "agent": "alpha", "leaf_id": "accept", "ticket": ticket,
                "cancel": threading.Event(), "expires": time.monotonic() + 5, "start_monotonic": time.monotonic()}
        result = SpawnResult(agent="alpha", leaf_id="accept", task_id=source.id, cmd=[sys.executable, "-c", "pass"])
        engine._monitor_process(source.id, info, result)
        self.assertTrue(result.process_success, result)
        self.assertTrue(result.success, result)
        self.assertEqual(self.leaves()["accept"]["artifact_state"], "not_required")

    def test_headless_receipt_verifier_accepts_superseded_but_rejects_latest_drift(self):
        from agentbus.headless_spawn import HeadlessSpawnEngine
        a, ra, b, rb, _ = self.revisions()
        engine = HeadlessSpawnEngine(self.cfg)
        ticket = captured_ticket(a)
        self.assertTrue(engine._delivery_verified("create", a.id, ticket))
        (self.ws / "module.py").write_bytes(b"UNAUTHORIZED = True\n")
        self.assertFalse(engine._delivery_verified("create", a.id, ticket))


if __name__ == "__main__":
    unittest.main()
