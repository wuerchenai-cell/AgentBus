from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentbus.bus import Bus, BusError, Message
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.lease import LeaseManager
from agentbus.task_tree import TaskForest
from agentbus.workspace_files import WorkspaceFileError, WorkspaceFiles


class StrictSupervisionFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentbus-supervision-flow-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.cfg = load(self.workspace, create=True)
        self.cfg.data["workers"].update({
            "worker": {"enabled": True, "executor_enabled": True,
                       "can_spawn": False, "kind": "gui"},
            "reviewer": {"enabled": True, "executor_enabled": True,
                         "can_spawn": False, "kind": "gui"},
        })
        self.cfg = save(self.cfg)
        self.bus = Bus(self.workspace / ".agentbus")
        self.bus.init()
        self.artifact_path = self.workspace / "output.txt"
        self.artifact_path.write_text("actual output\n", encoding="utf-8")
        raw = self.artifact_path.read_bytes()
        self.artifacts = [{"path": "output.txt", "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}]

    def enable_supervision(self, *, auditor="reviewer", checks=None):
        if checks is None:
            checks = [{"name": "workspace acceptance tests",
                       "argv": self._write_unittest_check(
                           "test_agentbus_acceptance",
                           "from pathlib import Path\n"
                           "class Acceptance(unittest.TestCase):\n"
                           "    def test_declared_output_is_present(self):\n"
                           "        self.assertTrue(Path('output.txt').read_text(encoding='utf-8').strip())\n"),
                       "timeout_seconds": 10}]
        data = dict(self.cfg.data)
        data["supervision"] = {
            "strict_supervision": True,
            "designated_auditor": auditor,
            "checks": checks,
        }
        self.cfg = save(self.cfg, data)
        return self.cfg

    def _write_unittest_check(self, module, test_class_body):
        path = self.workspace / f"{module}.py"
        path.write_text(
            "import unittest\n" + test_class_body,
            encoding="utf-8")
        return [sys.executable, "-m", "unittest", module]

    def task(self, *, worker="worker", kind="task"):
        message = self.bus.make(
            "coordinator", worker, kind, title="supervised work",
            body="Create output.txt", result_to="coordinator",
            expected_artifacts=["output.txt"],
        )
        self.bus.post(message)
        path = self.bus.agents_dir / worker / "ready" / f"{message.id}.json"
        claimed = self.bus.claim(worker, path)
        self.bus.heartbeat(worker, claimed, captured_ticket(claimed),
                           note="test worker started")
        return claimed

    def submit_work(self, task, *, body="Finished output", artifacts=None,
                    exit_code=0):
        return self.bus.commit_result(
            task.to, task, captured_ticket(task), body=body,
            artifacts=self.artifacts if artifacts is None else artifacts,
            exit_code=exit_code,
            artifact_root=self.cfg.output_root,
        )

    def audit_task(self, task):
        review_id = (getattr(task, "supervision", None) or {}).get("review_task_id")
        self.assertTrue(review_id, "pending audit must bind a formal review task")
        review_path = self.bus.agents_dir / "reviewer" / "ready" / f"{review_id}.json"
        return self.bus.read(review_path)

    def decide(self, review, decision="approve", *, agent="reviewer",
                reason="independent review", result_to=None):
        claimed = self.bus.claim(agent, self.bus.agents_dir / agent / "ready" / f"{review.id}.json")
        self.assertIsNotNone(claimed)
        self.bus.heartbeat(agent, claimed, captured_ticket(claimed),
                           note="test auditor started")
        link = json.loads(claimed.body)
        body = json.dumps({
            "schema_version": 1,
            "task_id": link["task_id"],
            "result_id": link["result_id"],
            "decision": decision,
            "artifacts_reviewed": True,
            "checks_reviewed": True,
            "text_scan_reviewed": True,
            "reason": reason,
        }, ensure_ascii=False)
        return self.bus.commit_result(
            agent, claimed, captured_ticket(claimed), body=body,
            result_to=result_to)

    def test_strict_submission_stays_awaiting_audit_until_independent_approval(self):
        self.enable_supervision()
        task = self.task()
        pending = self.submit_work(task)

        active = self.bus._active_record("worker", task.id)
        self.assertEqual(active.status, "awaiting_audit")
        self.assertEqual((getattr(active, "supervision", None) or {}).get("state"), "awaiting_audit")
        self.assertIsNone(self.bus.find_result(task.id, sender="worker", recipient="coordinator"))
        review = self.audit_task(active)
        self.assertEqual(review.type, "review")
        self.assertEqual(review.to, "reviewer")
        self.assertEqual(pending.id, (active.supervision or {})["result"]["id"])

        self.decide(review, "approve")
        terminal = self.bus.read(self.bus.agents_dir / "worker" / "done" / f"{task.id}.json")
        self.assertEqual(terminal.status, "done")
        self.assertEqual((terminal.supervision or {})["state"], "approved")
        self.assertIsNotNone(self.bus.find_result(task.id, sender="worker", recipient="coordinator"))

    def test_direct_post_cannot_bypass_strict_supervision(self):
        self.enable_supervision()
        task = self.task()
        forged = Message(
            id="m-forged-001", ts="2026-10-10T00:00:00+00:00", sender="worker",
            to="coordinator", type="result", title=task.title, body="pretend approved",
            reply_to=task.id, result_to="coordinator", artifacts=self.artifacts,
            source_claim_epoch=task.claim_epoch, source_claim_id=task.claim_id,
        )
        with self.assertRaisesRegex(BusError, "supervision|audit|审核"):
            self.bus.post(forged)
        self.assertIsNone(self.bus.find_message(forged.id))

    def test_direct_post_cannot_bypass_pending_audit_after_config_is_disabled(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        self.assertEqual(pending.status, "awaiting_audit")

        changed = dict(self.cfg.data)
        changed["supervision"] = dict(
            changed["supervision"], strict_supervision=False)
        self.cfg = save(self.cfg, changed)
        forged = Message(
            id="m-forged-after-disable", ts="2026-10-10T00:00:00+00:00",
            sender="worker", to="coordinator", type="result",
            title=task.title, body="direct post bypass",
            reply_to=task.id, result_to="coordinator", artifacts=self.artifacts,
            source_claim_epoch=task.claim_epoch, source_claim_id=task.claim_id,
        )

        with self.assertRaisesRegex(BusError, "supervision|audit|审核|监督"):
            self.bus.post(forged)
        self.assertIsNone(self.bus.find_message(forged.id))
        self.assertIsNone(self.bus.find_result(
            task.id, sender="worker", recipient="coordinator"))

    def test_wrong_audit_result_recipient_is_rejected_before_decision_is_recorded(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)

        with self.assertRaises(BusError):
            self.decide(review, "approve", result_to="worker")

        still_pending = self.bus._active_record("worker", task.id)
        self.assertEqual(still_pending.status, "awaiting_audit")
        self.assertEqual(still_pending.supervision["state"], "awaiting_audit")
        self.assertNotIn("decision", still_pending.supervision)
        self.assertIsNone(self.bus.find_result(
            review.id, sender="reviewer", recipient="worker"))
        self.assertTrue((self.bus.agents_dir / "reviewer" / "processing" / f"{review.id}.json").is_file())
        self.assertFalse((self.bus.agents_dir / "reviewer" / "done" / f"{review.id}.json").exists())

    def test_direct_post_cannot_replay_approved_receipt_without_artifact_recheck(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)
        original_post = self.bus._post_supervised_result

        def crash_before_result_post(message):
            if message.type == "result" and message.reply_to == task.id:
                raise BusError("simulated crash after approved state was persisted")
            return original_post(message)

        with patch.object(self.bus, "_post_supervised_result", side_effect=crash_before_result_post):
            with self.assertRaisesRegex(BusError, "simulated crash"):
                self.decide(review, "approve")
        approved = self.bus._active_record("worker", task.id)
        self.assertEqual(approved.supervision["state"], "approved")
        replay = Message.from_dict(approved.supervision["result"], strict=True)
        self.artifact_path.write_text("changed after approved state\n", encoding="utf-8")

        with self.assertRaisesRegex(BusError, "supervision|audit|审核|监督"):
            self.bus.post(replay)
        self.assertIsNone(self.bus.find_message(replay.id))

    def test_self_audit_is_rejected_before_any_pending_receipt_is_created(self):
        self.enable_supervision()
        task = self.task()
        changed = dict(self.cfg.data)
        changed["supervision"] = dict(changed["supervision"], designated_auditor="worker")
        self.cfg = save(self.cfg, changed)
        with self.assertRaises(BusError):
            self.submit_work(task)
        active = self.bus._active_record("worker", task.id)
        self.assertEqual(active.status, "claimed")
        self.assertFalse(list((self.bus.agents_dir / "worker" / "ready").glob("*.json")))

    def test_designated_auditor_cannot_receive_task_or_goal_dispatch(self):
        self.enable_supervision()
        for kind in ("task", "goal"):
            with self.subTest(kind=kind), self.assertRaisesRegex(BusError, "auditor|审核"):
                self.bus.post(self.bus.make(
                    "coordinator", "reviewer", kind, title="must remain independent",
                    body="do work", result_to="coordinator"))

    def test_auditor_rejection_keeps_task_rejected_and_out_of_done(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        active = self.bus._active_record("worker", task.id)
        review = self.audit_task(active)
        self.decide(review, "reject", reason="artifact does not meet acceptance criteria")

        rejected = self.bus.read(self.bus.agents_dir / "worker" / "blocked" / f"{task.id}.json")
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual((rejected.supervision or {})["state"], "rejected")
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())
        self.assertIsNone(self.bus.find_result(task.id, sender="worker", recipient="coordinator"))
        self.assertEqual(self.bus.reclaim_stale("worker", -1, owner_confirmed_inactive=True), [])

    def test_rejected_commit_recovers_crash_between_blocked_write_and_processing_cleanup(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        active = self.bus._active_record("worker", task.id)
        review = self.audit_task(active)
        processing = self.bus.agents_dir / "worker" / "processing" / f"{task.id}.json"
        blocked = self.bus.agents_dir / "worker" / "blocked" / f"{task.id}.json"
        original_unlink = Path.unlink

        def interrupt_before_cleanup(path, *args, **kwargs):
            if path == processing:
                raise OSError("simulated crash before processing cleanup")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=interrupt_before_cleanup):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.decide(review, "reject", reason="independent rejection")

        self.assertTrue(processing.is_file())
        self.assertTrue(blocked.is_file())
        recovered = self.bus.recover_claim(
            "worker", task.id, owner_confirmed_inactive=True)
        self.assertEqual(recovered.status, "rejected")
        self.assertEqual(recovered.supervision["state"], "rejected")
        self.assertFalse(processing.exists())
        terminal = self.bus.read(blocked)
        self.assertEqual(terminal.status, "rejected")
        self.assertEqual(terminal.supervision["case_id"], recovered.supervision["case_id"])
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())

    def test_failed_physical_evidence_cannot_be_approved(self):
        self.enable_supervision()
        task = self.task()
        bad_manifest = [dict(self.artifacts[0], bytes=999)]
        with self.assertRaises(BusError):
            self.submit_work(task, artifacts=bad_manifest)
        still_processing = self.bus._active_record("worker", task.id)
        self.assertEqual(still_processing.status, "claimed")
        self.assertTrue((self.bus.agents_dir / "worker" / "processing" / f"{task.id}.json").is_file())
        self.assertEqual(still_processing.supervision, {})
        self.assertIsNone(self.bus.find_result(task.id, sender="worker", recipient="coordinator"))

    def test_artifact_mutated_by_allowlisted_check_is_not_reviewable(self):
        self.enable_supervision(checks=[{
            "name": "mutate-artifact-during-check",
            "argv": self._write_unittest_check(
                "test_mutate_artifact",
                "from pathlib import Path\n"
                "class Mutation(unittest.TestCase):\n"
                "    def test_mutates_declared_output(self):\n"
                "        Path('output.txt').write_text('tampered during check\\n', encoding='utf-8')\n"),
            "timeout_seconds": 10,
        }])
        task = self.task()
        self.submit_work(task)

        active = self.bus._active_record("worker", task.id)
        self.assertEqual(active.status, "awaiting_audit")
        evidence = active.supervision["evidence"]
        self.assertFalse(evidence["reviewable"],
                         "post-check artifact mutation must invalidate review evidence")
        self.assertEqual(evidence["artifact_verification"]["verdict"], "fail")
        review = self.audit_task(active)
        with self.assertRaises(BusError):
            self.decide(review, "approve")
        self.assertIsNone(self.bus.find_result(task.id, sender="worker", recipient="coordinator"))

    def test_artifact_mutation_after_submission_blocks_approval(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        active = self.bus._active_record("worker", task.id)
        review = self.audit_task(active)

        self.artifact_path.write_text("tampered while awaiting audit\\n", encoding="utf-8")
        with self.assertRaisesRegex(BusError, "artifact|产物|证据|审核"):
            self.decide(review, "approve")

        still_waiting = self.bus._active_record("worker", task.id)
        self.assertEqual(still_waiting.status, "awaiting_audit")
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())
        self.assertIsNone(self.bus.find_result(task.id, sender="worker", recipient="coordinator"))

    def test_approval_holds_artifact_lease_through_terminal_commit(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)
        manager = LeaseManager(self.workspace / ".agentbus")
        writer = WorkspaceFiles(self.workspace)
        expected_sha = hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
        observed_competing_leases = []
        original_finish = self.bus._finish_receipt

        def inspect_lease_during_finish(agent, *args, **kwargs):
            if agent == "worker":
                competing = manager.acquire(
                    str(self.artifact_path.resolve()), "other-writer", ttl_s=30,
                    note="must be excluded by final audit commit")
                relative_competing = manager.acquire(
                    "output.txt", "relative-writer", ttl_s=30)
                try:
                    writer.write_text("output.txt", "competing rewrite",
                                      "files-write", expected_sha)
                except WorkspaceFileError as exc:
                    write_error = exc.code
                else:
                    write_error = None
                observed_competing_leases.append(
                    (competing, relative_competing, write_error))
                if competing is not None:
                    manager.release(competing)
                if relative_competing is not None:
                    manager.release(relative_competing)
            return original_finish(agent, *args, **kwargs)

        with patch.object(self.bus, "_finish_receipt",
                          side_effect=inspect_lease_during_finish):
            self.decide(review, "approve")

        self.assertEqual(observed_competing_leases, [(None, None, "LEASE_BUSY")],
                         "control CLI and files-write routes must be excluded through final commit")
        self.assertIsNone(manager.holder(str(self.artifact_path.resolve())),
                          "finalization must release the temporary artifact lease")
        self.assertIsNone(manager.holder("output.txt"),
                          "finalization must release the relative-path alias lease")
        self.assertTrue((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())

    def test_external_output_root_artifact_lease_uses_shared_control_namespace(self):
        output_root = self.workspace / "separate-output-root"
        output_root.mkdir()
        data = dict(self.cfg.data)
        data["output_root"] = str(output_root)
        self.cfg = save(self.cfg, data)
        self.artifact_path = output_root / "output.txt"
        self.artifact_path.write_text("external output root\n", encoding="utf-8")
        raw = self.artifact_path.read_bytes()
        self.artifacts = [{"path": "output.txt", "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}]

        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)
        manager = LeaseManager(self.workspace / ".agentbus")
        resource_key = str(self.artifact_path.resolve())
        observed_competing_leases = []
        original_finish = self.bus._finish_receipt

        def inspect_external_lease(agent, *args, **kwargs):
            if agent == "worker":
                competing = manager.acquire(
                    resource_key, "output-root-writer", ttl_s=30,
                    note="external output root must share control lease registry")
                observed_competing_leases.append(competing)
                if competing is not None:
                    manager.release(competing)
            return original_finish(agent, *args, **kwargs)

        with patch.object(self.bus, "_finish_receipt", side_effect=inspect_external_lease):
            self.decide(review, "approve")

        self.assertEqual(observed_competing_leases, [None])
        self.assertIsNone(manager.holder(resource_key))
        self.assertTrue((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())

    def test_approval_blocks_control_and_output_root_writers(self):
        output_root = self.workspace / "separate-output-root"
        output_root.mkdir()
        data = dict(self.cfg.data)
        data["output_root"] = str(output_root)
        self.cfg = save(self.cfg, data)
        self.artifact_path = output_root / "output.txt"
        self.artifact_path.write_text("external output\n", encoding="utf-8")
        raw = self.artifact_path.read_bytes()
        self.artifacts = [{"path": "output.txt", "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}]

        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)
        control_manager = LeaseManager(self.bus.root)
        output_manager = LeaseManager(output_root / ".agentbus")
        output_writer = WorkspaceFiles(output_root)
        expected_output_sha = hashlib.sha256(raw).hexdigest()
        competing = []
        original_finish = self.bus._finish_receipt

        def inspect_both_namespaces(agent, *args, **kwargs):
            if agent == "worker":
                control_lease = control_manager.acquire(
                    str(self.artifact_path.resolve()), "other-control-writer",
                    ttl_s=30)
                output_lease = output_manager.acquire(
                    "output.txt", "other-output-writer", ttl_s=30)
                try:
                    output_writer.write_text(
                        "output.txt", "competing rewrite", "other-output-writer",
                        expected_output_sha)
                except WorkspaceFileError as exc:
                    write_error = exc.code
                else:
                    write_error = None
                competing.append((control_lease, output_lease, write_error))
                if control_lease is not None:
                    control_manager.release(control_lease)
                if output_lease is not None:
                    output_manager.release(output_lease)
            return original_finish(agent, *args, **kwargs)

        with patch.object(self.bus, "_finish_receipt", side_effect=inspect_both_namespaces):
            self.decide(review, "approve")

        self.assertEqual(competing, [(None, None, "LEASE_BUSY")],
                         "approval must exclude control leases and the actual output-root files-write path")
        self.assertIsNone(control_manager.holder(str(self.artifact_path.resolve())))
        self.assertIsNone(output_manager.holder("output.txt"))

    def test_approved_commit_retry_rechecks_artifacts_after_result_post(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)
        original_finish = self.bus._finish_receipt

        def crash_before_source_terminal(agent, current, *args, **kwargs):
            if agent == "worker" and current.id == task.id:
                raise BusError("simulated crash after result post")
            return original_finish(agent, current, *args, **kwargs)

        with patch.object(self.bus, "_finish_receipt",
                          side_effect=crash_before_source_terminal):
            with self.assertRaisesRegex(BusError, "simulated crash"):
                self.decide(review, "approve")

        persisted = self.bus._active_record("worker", task.id)
        self.assertEqual(persisted.supervision["state"], "approved")
        self.artifact_path.write_text("changed after posted approval\n", encoding="utf-8")
        with self.assertRaisesRegex(BusError, "artifact|产物|证据|审核"):
            self.bus.commit_result(
                "worker", persisted, captured_ticket(persisted),
                body="Finished output", artifacts=self.artifacts, exit_code=0)
        self.assertEqual(self.bus._active_record("worker", task.id).status,
                         "awaiting_audit")
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())

    def test_approved_commit_retry_requires_posted_auditor_receipt(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)
        original_post = self.bus._post_supervised_result

        def crash_before_worker_result(message):
            if message.type == "result" and message.reply_to == task.id:
                raise BusError("simulated crash before worker result post")
            return original_post(message)

        with patch.object(self.bus, "_post_supervised_result",
                          side_effect=crash_before_worker_result):
            with self.assertRaisesRegex(BusError, "simulated crash"):
                self.decide(review, "approve")

        persisted = self.bus._active_record("worker", task.id)
        self.assertEqual(persisted.supervision["state"], "approved")
        auditor_receipt = self.bus.find_result(
            review.id, sender="reviewer", recipient="coordinator")
        self.assertIsNotNone(auditor_receipt)
        receipt_path = self.bus.agents_dir / "coordinator" / "ready" / f"{auditor_receipt.id}.json"
        self.assertTrue(receipt_path.is_file())
        receipt_path.unlink()
        self.assertIsNone(self.bus.find_result(
            review.id, sender="reviewer", recipient="coordinator"))

        with patch.object(
                self.bus, "_post_supervised_result",
                side_effect=AssertionError(
                    "worker result publication must not start without the posted audit receipt")):
            with self.assertRaisesRegex(BusError, "正式监督回执缺失"):
                self.bus.commit_result(
                    "worker", persisted, captured_ticket(persisted),
                    body="Finished output", artifacts=self.artifacts, exit_code=0)
        self.assertIsNone(self.bus.find_result(
            task.id, sender="worker", recipient="coordinator"))
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())

    def test_finish_cannot_bypass_approved_supervision_when_disabled(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)
        original_finish = self.bus._finish_receipt

        def crash_before_source_terminal(agent, current, *args, **kwargs):
            if agent == "worker" and current.id == task.id:
                raise BusError("simulated crash after result post")
            return original_finish(agent, current, *args, **kwargs)

        with patch.object(self.bus, "_finish_receipt",
                          side_effect=crash_before_source_terminal):
            with self.assertRaisesRegex(BusError, "simulated crash"):
                self.decide(review, "approve")

        persisted = self.bus._active_record("worker", task.id)
        changed = dict(self.cfg.data)
        changed["supervision"] = dict(
            changed["supervision"], strict_supervision=False)
        self.cfg = save(self.cfg, changed)
        with self.assertRaisesRegex(BusError, "supervision|audit|审核|监督"):
            self.bus.finish("worker", persisted, status="done",
                            ticket=captured_ticket(persisted))
        self.assertTrue((self.bus.agents_dir / "worker" / "processing" / f"{task.id}.json").exists())

    def test_strict_supervision_requires_review_flow_before_any_terminal_finish(self):
        for kind in ("task", "goal"):
            for terminal_status in ("done", "failed", "rejected", "blocked"):
                with self.subTest(kind=kind, status=terminal_status):
                    # Each blocked claim intentionally remains live. The
                    # single-work-slot policy requires independent fixtures.
                    self.setUp()
                    self.enable_supervision()
                    task = self.task(kind=kind)
                    with self.assertRaisesRegex(BusError, "supervision|audit|审核|监督"):
                        self.bus.finish(
                            "worker", task, status=terminal_status,
                            ticket=captured_ticket(task))

                    active = self.bus._active_record("worker", task.id)
                    self.assertEqual(active.status, "claimed")
                    self.assertEqual(active.claim_epoch, task.claim_epoch)
                    self.assertTrue((self.bus.agents_dir / "worker" / "processing" / f"{task.id}.json").is_file())
                    self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())
                    self.assertFalse((self.bus.agents_dir / "worker" / "blocked" / f"{task.id}.json").exists())

    def test_strict_supervision_finish_rejects_missing_active_record(self):
        self.enable_supervision()
        for kind in ("task", "goal"):
            with self.subTest(kind=kind):
                unposted = self.bus.make(
                    "coordinator", "worker", kind,
                    title="missing active record",
                    body="must not create a terminal copy",
                    result_to="coordinator")

                with self.assertRaisesRegex(BusError, "supervision|audit|审核|监督"):
                    self.bus.finish("worker", unposted, status="failed")

                self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{unposted.id}.json").exists())

    def test_non_done_terminal_status_cannot_bypass_pending_audit(self):
        for terminal_status in ("failed", "rejected", "blocked"):
            with self.subTest(status=terminal_status):
                self.setUp()
                self.enable_supervision()
                task = self.task()
                self.submit_work(task)
                pending = self.bus._active_record("worker", task.id)
                self.assertEqual(pending.status, "awaiting_audit")
                with self.assertRaisesRegex(BusError, "supervision|audit|审核|监督"):
                    self.bus.finish(
                        "worker", pending, status=terminal_status,
                        ticket=captured_ticket(task))

                still_pending = self.bus._active_record("worker", task.id)
                self.assertEqual(still_pending.status, "awaiting_audit")
                self.assertTrue((self.bus.agents_dir / "worker" / "processing" / f"{task.id}.json").is_file())
                self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())
                self.assertFalse((self.bus.agents_dir / "worker" / "blocked" / f"{task.id}.json").exists())

    def test_forged_review_payload_cannot_claim_a_different_evidence_digest(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        active = self.bus._active_record("worker", task.id)
        review_path = self.bus.agents_dir / "reviewer" / "ready" / f"{active.supervision['review_task_id']}.json"
        review = self.bus.read(review_path)
        payload = json.loads(review.body)
        payload["evidence_sha256"] = "0" * 64
        review.body = json.dumps(payload, ensure_ascii=False)
        self.bus._write(review_path, review)

        review = self.bus.read(review_path)
        with self.assertRaisesRegex(BusError, "evidence|证据|审核"):
            self.decide(review, "approve")

        still_waiting = self.bus._active_record("worker", task.id)
        self.assertEqual(still_waiting.status, "awaiting_audit")
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())

    def test_failed_submission_is_not_automatically_requeued_by_stale_reclaim(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task, exit_code=9)
        recovered = self.bus.reclaim_stale(
            "worker", -1, owner_confirmed_inactive=True)
        self.assertEqual(recovered, [])
        active = self.bus._active_record("worker", task.id)
        self.assertEqual(active.status, "awaiting_audit")
        self.assertTrue((self.bus.agents_dir / "worker" / "processing" / f"{task.id}.json").is_file())
        self.assertFalse((self.bus.agents_dir / "worker" / "ready" / f"{task.id}.json").exists())

    def test_failed_allowlisted_check_cannot_be_approved_or_requeued(self):
        self.enable_supervision(checks=[{
            "name": "failing-smoke",
            "argv": self._write_unittest_check(
                "test_failing_acceptance",
                "class FailingAcceptance(unittest.TestCase):\n"
                "    def test_fails(self):\n"
                "        self.fail('expected nonzero test result')\n"),
            "timeout_seconds": 10,
        }])
        task = self.task()
        self.submit_work(task)
        active = self.bus._active_record("worker", task.id)
        self.assertNotEqual(active.supervision["evidence"]["checks"][0]["exit_code"], 0)
        self.assertFalse(active.supervision["evidence"]["checks"][0]["audit_qualifying"])
        self.assertFalse(active.supervision["evidence"]["reviewable"])
        review = self.audit_task(active)
        with self.assertRaises(BusError):
            self.decide(review, "approve")
        self.assertEqual(self.bus.reclaim_stale(
            "worker", -1, owner_confirmed_inactive=True), [])
        current = self.bus._active_record("worker", task.id)
        self.assertEqual(current.status, "awaiting_audit")
        self.assertFalse((self.bus.agents_dir / "worker" / "ready" / f"{task.id}.json").exists())
        self.assertIsNone(self.bus.find_result(task.id, sender="worker", recipient="coordinator"))

    def test_host_secret_environment_is_not_exposed_or_persisted_in_audit_evidence(self):
        marker = "agentbus-flow-test-secret"
        self.enable_supervision(checks=[{
            "name": "env-isolation",
            "argv": self._write_unittest_check(
                "test_env_isolation",
                "import os\n"
                "class EnvironmentIsolation(unittest.TestCase):\n"
                "    def test_host_secret_is_not_inherited(self):\n"
                "        self.assertEqual(os.getenv('AGENTBUS_TEST_SECRET', ''), '')\n"),
            "timeout_seconds": 10,
        }])
        task = self.task()
        with patch.dict(os.environ, {"AGENTBUS_TEST_SECRET": marker}):
            self.submit_work(task)
        active = self.bus._active_record("worker", task.id)
        serialized = json.dumps(active.to_dict(), ensure_ascii=False)
        self.assertNotIn(marker, serialized)
        self.assertNotIn("stdout", active.supervision["evidence"]["checks"][0])
        self.assertNotIn("stderr", active.supervision["evidence"]["checks"][0])

    def test_recovery_reconciles_only_a_persisted_approved_decision(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        active = self.bus._active_record("worker", task.id)
        review = self.audit_task(active)
        claimed_review = self.bus.claim(
            "reviewer", self.bus.agents_dir / "reviewer" / "ready" / f"{review.id}.json")
        self.assertIsNotNone(claimed_review)
        self.bus.heartbeat("reviewer", claimed_review, captured_ticket(claimed_review),
                           note="test auditor started")
        link = json.loads(claimed_review.body)
        decision = json.dumps({
            "schema_version": 1, "task_id": link["task_id"],
            "result_id": link["result_id"], "decision": "approve",
            "artifacts_reviewed": True, "checks_reviewed": True,
            "text_scan_reviewed": True, "reason": "verified",
        })
        with patch.object(self.bus, "_complete_supervision_decision",
                          side_effect=BusError("simulated crash after review receipt")):
            with self.assertRaisesRegex(BusError, "simulated crash"):
                self.bus.commit_result(
                    "reviewer", claimed_review, captured_ticket(claimed_review),
                    body=decision)

        pending = self.bus._active_record("worker", task.id)
        self.assertEqual(pending.status, "awaiting_audit")
        self.assertEqual(pending.supervision["state"], "decision_recorded")
        self.assertIsNotNone(self.bus.find_result(review.id, sender="reviewer", recipient="coordinator"))
        recovered = self.bus.recover_claim(
            "worker", task.id, owner_confirmed_inactive=True)
        self.assertEqual(recovered.status, "done")
        self.assertIsNotNone(self.bus.find_result(task.id, sender="worker", recipient="coordinator"))

    def test_approved_recovery_rechecks_artifacts_before_posting_result(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)
        original_post = self.bus._post_supervised_result

        def crash_before_result_post(message):
            if message.type == "result" and message.reply_to == task.id:
                raise BusError("simulated crash after approved state was persisted")
            return original_post(message)

        with patch.object(self.bus, "_post_supervised_result", side_effect=crash_before_result_post):
            with self.assertRaisesRegex(BusError, "simulated crash"):
                self.decide(review, "approve")

        persisted = self.bus._active_record("worker", task.id)
        self.assertEqual(persisted.status, "awaiting_audit")
        self.assertEqual(persisted.supervision["state"], "approved")
        self.artifact_path.write_text("changed after approval crash\n", encoding="utf-8")

        with self.assertRaisesRegex(BusError, "artifact|产物|证据|审核"):
            self.bus.recover_claim("worker", task.id, owner_confirmed_inactive=True)
        still_pending = self.bus._active_record("worker", task.id)
        self.assertEqual(still_pending.status, "awaiting_audit")
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())
        self.assertIsNone(self.bus.find_result(
            task.id, sender="worker", recipient="coordinator"))

    def test_approved_recovery_rechecks_artifacts_when_result_was_already_posted(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        review = self.audit_task(pending)
        original_finish = self.bus._finish_receipt

        def crash_before_source_terminal(agent, current, *args, **kwargs):
            if agent == "worker" and current.id == task.id:
                raise BusError("simulated crash after result post")
            return original_finish(agent, current, *args, **kwargs)

        with patch.object(self.bus, "_finish_receipt",
                          side_effect=crash_before_source_terminal):
            with self.assertRaisesRegex(BusError, "simulated crash"):
                self.decide(review, "approve")

        persisted = self.bus._active_record("worker", task.id)
        self.assertEqual(persisted.supervision["state"], "approved")
        self.assertIsNotNone(self.bus.find_result(
            task.id, sender="worker", recipient="coordinator"))
        self.artifact_path.write_text("changed after posted approval\n", encoding="utf-8")

        with self.assertRaisesRegex(BusError, "artifact|产物|证据|审核"):
            self.bus.recover_claim("worker", task.id, owner_confirmed_inactive=True)
        still_pending = self.bus._active_record("worker", task.id)
        self.assertEqual(still_pending.status, "awaiting_audit")
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())

    def test_recovery_never_auto_delivers_recorded_rejection(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        active = self.bus._active_record("worker", task.id)
        review = self.audit_task(active)
        claimed_review = self.bus.claim(
            "reviewer", self.bus.agents_dir / "reviewer" / "ready" / f"{review.id}.json")
        self.bus.heartbeat("reviewer", claimed_review, captured_ticket(claimed_review),
                           note="test auditor started")
        link = json.loads(claimed_review.body)
        decision = json.dumps({
            "schema_version": 1, "task_id": link["task_id"],
            "result_id": link["result_id"], "decision": "reject",
            "artifacts_reviewed": True, "checks_reviewed": True,
            "text_scan_reviewed": True, "reason": "reject after inspection",
        })
        with patch.object(self.bus, "_complete_supervision_decision",
                          side_effect=BusError("simulated crash after review receipt")):
            with self.assertRaisesRegex(BusError, "simulated crash"):
                self.bus.commit_result(
                    "reviewer", claimed_review, captured_ticket(claimed_review),
                    body=decision)
        recovered = self.bus.recover_claim(
            "worker", task.id, owner_confirmed_inactive=True)
        self.assertEqual(recovered.status, "rejected")
        self.assertEqual(recovered.supervision["state"], "rejected")
        self.assertTrue((self.bus.agents_dir / "worker" / "blocked" / f"{task.id}.json").exists())
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())
        self.assertIsNone(self.bus.find_result(task.id, sender="worker", recipient="coordinator"))

    def test_disabled_supervision_keeps_existing_result_commit_behavior(self):
        task = self.task()
        result = self.submit_work(task)
        terminal = self.bus.read(self.bus.agents_dir / "worker" / "done" / f"{task.id}.json")
        self.assertEqual(terminal.status, "done")
        self.assertEqual(result.type, "result")
        self.assertEqual(result.to, "coordinator")

    def test_disabling_supervision_cannot_bypass_an_existing_pending_audit(self):
        self.enable_supervision()
        task = self.task()
        self.submit_work(task)
        pending = self.bus._active_record("worker", task.id)
        self.assertEqual(pending.status, "awaiting_audit")

        changed = dict(self.cfg.data)
        changed["supervision"] = dict(
            changed["supervision"], strict_supervision=False)
        self.cfg = save(self.cfg, changed)

        with self.assertRaisesRegex(BusError, "supervision|audit|审核|监督"):
            self.bus.commit_result(
                "worker", pending, captured_ticket(pending),
                body="bypass audit", artifacts=self.artifacts, exit_code=0)
        with self.assertRaisesRegex(BusError, "supervision|audit|审核|监督"):
            self.bus.finish(
                "worker", pending, status="done", ticket=captured_ticket(pending))

        still_pending = self.bus._active_record("worker", task.id)
        self.assertEqual(still_pending.status, "awaiting_audit")
        self.assertIsNone(self.bus.find_result(
            task.id, sender="worker", recipient="coordinator"))
        self.assertFalse((self.bus.agents_dir / "worker" / "done" / f"{task.id}.json").exists())

    def test_inline_noop_check_cannot_qualify_supervised_delivery(self):
        self.enable_supervision(checks=[{
            "name": "no-op",
            "argv": [sys.executable, "-c", "print('ok')"],
            "timeout_seconds": 10,
        }])
        task = self.task()
        self.submit_work(task)

        pending = self.bus._active_record("worker", task.id)
        evidence = pending.supervision["evidence"]
        self.assertEqual(pending.status, "awaiting_audit")
        self.assertFalse(evidence["checks"][0]["audit_qualifying"])
        self.assertFalse(evidence["reviewable"],
                         "a zero-exit inline no-op is not independent acceptance evidence")
        with self.assertRaises(BusError):
            self.decide(self.audit_task(pending), "approve")

    def test_all_skipped_test_suite_cannot_qualify_supervised_delivery(self):
        skipped_module = self._write_unittest_check(
            "test_only_skipped_acceptance",
            "@unittest.skip('the only test is skipped')\n"
            "class SkippedAcceptance(unittest.TestCase):\n"
            "    def test_acceptance(self):\n"
            "        self.fail('skipped tests must not execute')\n")
        self.enable_supervision(checks=[{
            "name": "all-skipped acceptance suite",
            "argv": skipped_module,
            "timeout_seconds": 10,
        }])
        task = self.task()
        self.submit_work(task)

        pending = self.bus._active_record("worker", task.id)
        evidence = pending.supervision["evidence"]
        self.assertEqual(pending.status, "awaiting_audit")
        self.assertEqual(evidence["checks"][0]["exit_code"], 0)
        self.assertFalse(evidence["checks"][0]["audit_qualifying"])
        self.assertFalse(evidence["reviewable"])
        with self.assertRaises(BusError):
            self.decide(self.audit_task(pending), "approve")

    def test_zero_test_unittest_run_cannot_qualify_supervised_delivery(self):
        self.enable_supervision(checks=[{
            "name": "empty-unittest-discovery",
            "argv": [sys.executable, "-m", "unittest", "discover"],
            "timeout_seconds": 10,
        }])
        task = self.task()
        self.submit_work(task)

        pending = self.bus._active_record("worker", task.id)
        check = pending.supervision["evidence"]["checks"][0]
        self.assertNotEqual(check["exit_code"], 0)
        self.assertFalse(check["audit_qualifying"])
        self.assertFalse(pending.supervision["evidence"]["reviewable"])

    def test_task_tree_exposes_awaiting_audit_and_rejected_as_distinct_states(self):
        self.enable_supervision()
        task = self.task()
        forest = TaskForest(self.cfg)
        forest.replace_structure({
            "schema_version": 1,
            "roots": [{"id": "project", "parent_id": None, "title": "Project", "goal": "Ship"}],
            "branches": [{"id": "branch", "parent_id": "project", "title": "Delivery"}],
            "leaves": [{"id": "leaf", "parent_id": "branch", "title": "Output",
                        "task_id": task.id, "goal": task.body}],
        })
        self.submit_work(task)
        self.assertEqual(forest.snapshot()["leaves"][0]["state"], "awaiting_audit")
        review = self.audit_task(self.bus._active_record("worker", task.id))
        self.decide(review, "reject", reason="not acceptable")
        leaf = forest.snapshot()["leaves"][0]
        self.assertEqual(leaf["state"], "rejected")
        self.assertFalse(leaf["claimable"])


if __name__ == "__main__":
    unittest.main()
