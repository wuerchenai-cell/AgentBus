"""Recovery must honor STOP, deadline, and retry gates before changing a claim."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


from receipt_helpers import receipt_ticket
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.bus import Bus, BusError
from agentbus.config import load, save


class RecoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name), create=True)
        self.bus = Bus(self.cfg.workspace / ".agentbus")
        self.bus.init()

    def make_claim(self, *, attempt=0, deadline_ts=0.0, **fields):
        msg = self.bus.make(
            "codex", "workbuddy", "task", body="recover me",
            result_to="codex", attempt=attempt, deadline_ts=deadline_ts, **fields,
        )
        self.bus.post(msg)
        return self.bus.claim("workbuddy", self.bus.pending("workbuddy")[0])

    def test_expired_claim_cannot_be_recovered(self):
        msg = self.make_claim(deadline_ts=time.time() - 1)
        with self.assertRaisesRegex(BusError, "期限"):
            self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(len(self.bus.processing("workbuddy")), 1)
        self.assertEqual(self.bus.read(self.bus.processing("workbuddy")[0]).attempt, 0)

    def test_retry_limit_cannot_be_exceeded(self):
        msg = self.make_claim(attempt=2)
        with self.assertRaisesRegex(BusError, "重试"):
            self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(len(self.bus.processing("workbuddy")), 1)

    def test_last_allowed_retry_is_recoverable_and_idempotent(self):
        msg = self.make_claim(attempt=1)
        first = self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        again = self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual((first.attempt, again.attempt), (2, 2))
        self.assertEqual(len([e for e in self.bus.events() if e["event"] == "recovered"]), 1)

    def test_past_authorization_cannot_bypass_next_recovery_limit(self):
        msg = self.make_claim(attempt=1)
        self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.bus.claim("workbuddy", self.bus.pending("workbuddy")[0])
        source = self.bus.processing("workbuddy")[0]
        original = source.read_bytes()
        with self.assertRaisesRegex(BusError, "重试"):
            self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(source.read_bytes(), original)

    def test_stop_appearing_while_acquiring_lease_blocks_recovery(self):
        from agentbus.lease import LeaseManager
        msg = self.make_claim()
        source = self.bus.processing("workbuddy")[0]
        original = source.read_bytes()
        acquire = LeaseManager.acquire
        def stopped(manager, *args, **kwargs):
            lease = acquire(manager, *args, **kwargs)
            (self.bus.root / "STOP").write_text("stop during lock", encoding="utf-8")
            return lease
        with patch.object(LeaseManager, "acquire", stopped):
            with self.assertRaisesRegex(BusError, "STOP"):
                self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(source.read_bytes(), original)

    def test_stop_appearing_after_intent_blocks_publication(self):
        msg = self.make_claim()
        source = self.bus.processing("workbuddy")[0]
        original = source.read_bytes()
        audit = self.bus.audit
        def stopped(**event):
            audit(**event)
            if event.get("event") == "recovery_authorized":
                (self.bus.root / "STOP").write_text("stop after intent", encoding="utf-8")
        with patch.object(self.bus, "audit", stopped):
            with self.assertRaisesRegex(BusError, "STOP"):
                self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(source.read_bytes(), original)

    def test_malformed_config_fails_closed(self):
        msg = self.make_claim()
        config_path = self.bus.root / "config.json"
        original = config_path.read_bytes()
        for value in (b"[]", b"null", b"{broken", b'{"gates":{"max_retries":true}}'):
            with self.subTest(config=value):
                config_path.write_bytes(value)
                with self.assertRaises(BusError):
                    self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
                self.assertEqual(len(self.bus.processing("workbuddy")), 1)
        config_path.write_bytes(original)

    def test_private_root_is_rejected_before_reading_config(self):
        private_bus = Bus(Path(self.temp.name) / ".workbuddy" / "not-created")
        def no_read(*args, **kwargs):
            self.fail("private path was read before validation")
        with patch.object(Path, "read_text", no_read):
            with self.assertRaises(BusError):
                private_bus.recover_claim("workbuddy", "m-safe", owner_confirmed_inactive=True)

    def test_nonfinite_or_invalid_task_deadline_fails_closed(self):
        msg = self.make_claim()
        source = self.bus.processing("workbuddy")[0]
        original = source.read_bytes()
        for invalid in (float("nan"), float("inf"), -1, "tomorrow", True):
            with self.subTest(deadline=invalid):
                data = msg.to_dict()
                data["deadline_ts"] = invalid
                source.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(BusError):
                    self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
                self.assertEqual(json.loads(source.read_text(encoding="utf-8"))["attempt"], 0)
        source.write_bytes(original)

    def test_expired_bound_job_blocks_task_with_future_deadline(self):
        from agentbus import jobs
        job = jobs.start_job(self.cfg, "codex", "bounded recovery", request_id="recovery-test")
        dispatched = jobs.dispatch_job(self.cfg, "codex", job["id"], [
            {"worker": "workbuddy", "title": "recover", "body": "bounded task", "result_to": "codex"}])
        msg = self.bus.claim("workbuddy", self.bus.pending("workbuddy")[0])
        job_path = self.bus.root / "jobs" / (dispatched["id"] + ".json")
        data = json.loads(job_path.read_text(encoding="utf-8"))
        data["deadline_ts"] = time.time() - 1
        job_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(BusError, "作业"):
            self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(len(self.bus.processing("workbuddy")), 1)

    def test_cli_refuses_exhausted_retry_without_changing_claim(self):
        msg = self.make_claim(attempt=2)
        source = self.bus.processing("workbuddy")[0]
        original = source.read_bytes()
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "agentbus", "recover",
            "--workspace", str(self.cfg.workspace), "--agent", "workbuddy", "--id", msg.id,
            "--owner-confirmed-inactive"], capture_output=True, timeout=15,
            env={**os.environ, "PYTHONPATH": str(_ROOT_DIR)})
        self.assertEqual(result.returncode, 4, result.stderr.decode("utf-8"))
        self.assertEqual(source.read_bytes(), original)

    def bound_claim(self):
        from agentbus import jobs
        job = jobs.start_job(self.cfg, "codex", "bounded recovery", request_id="bound-test")
        jobs.dispatch_job(self.cfg, "codex", job["id"], [
            {"worker": "workbuddy", "title": "recover", "body": "bounded task", "result_to": "codex"}])
        return job, self.bus.claim("workbuddy", self.bus.pending("workbuddy")[0])

    def test_stale_file_is_recoverable_after_explicit_owner_confirmation(self):
        job, msg = self.bound_claim()
        source = self.bus.processing("workbuddy")[0]
        old = time.time() - 3600
        os.utime(source, (old, old))
        result = self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual((result.id, result.attempt), (msg.id, 1))

    def test_stop_during_claim_write_prevents_ready_publication(self):
        msg = self.make_claim()
        source = self.bus.processing("workbuddy")[0]
        write = self.bus._write
        def stopped(path, message):
            write(path, message)
            if path == source:
                (self.bus.root / "STOP").write_text("stop during write", encoding="utf-8")
        with patch.object(self.bus, "_write", stopped):
            with self.assertRaisesRegex(BusError, "STOP"):
                self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertTrue(source.is_file())
        self.assertFalse((self.bus.dirs("workbuddy")["ready"] / source.name).exists())
        self.assertEqual(self.bus.read(source).attempt, 1)
        (self.bus.root / "STOP").unlink()
        result = self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(result.attempt, 1)

    def test_bound_job_check_does_not_scan_unrelated_member_mailboxes(self):
        job, msg = self.bound_claim()
        unrelated = self.bus.make("codex", "trae", "task", body="not related", result_to="codex")
        self.bus.post(unrelated)
        read = Path.read_text
        def bounded(path, *args, **kwargs):
            if "trae" in path.parts:
                self.fail("unrelated member mailbox was read for recovery")
            return read(path, *args, **kwargs)
        with patch.object(Path, "read_text", bounded):
            result = self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(result.attempt, 1)

    def test_bound_job_failed_goal_prevents_recovery(self):
        job, msg = self.bound_claim()
        goal = self.bus.find_message(job["goal_id"])
        self.bus.finish(goal.to, goal, status="failed", ticket=receipt_ticket(goal))
        with self.assertRaisesRegex(BusError, "作业"):
            self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)

    def test_already_published_recovery_can_close_under_stop(self):
        msg = self.make_claim(attempt=1)
        first = self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        (self.bus.root / "STOP").write_text("stop", encoding="utf-8")
        again = self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual((first.attempt, again.attempt), (2, 2))

    def test_matching_intent_with_wrong_attempt_cannot_bypass_gate(self):
        msg = self.make_claim(attempt=1)
        source = self.bus.processing("workbuddy")[0]
        original = source.read_bytes()
        with patch.object(self.bus, "_write", side_effect=OSError("failed before publication")):
            with self.assertRaises(OSError):
                self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        lines = self.bus.events_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            row = json.loads(line)
            if row.get("event") == "recovery_authorized":
                row["attempt_after"] = 0
                lines[lines.index(line)] = json.dumps(row)
        self.bus.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(BusError, "代次"):
            self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(source.read_bytes(), original)

    def test_deadline_expiring_during_write_blocks_publication(self):
        now = time.time()
        msg = self.make_claim(deadline_ts=now + 600)
        source = self.bus.processing("workbuddy")[0]
        clock = patch("agentbus.bus.time.time", return_value=now + 601)
        write = self.bus._write
        def expire(path, message):
            write(path, message)
            if path == source:
                clock.start()
        try:
            with patch.object(self.bus, "_write", expire):
                with self.assertRaisesRegex(BusError, "期限"):
                    self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        finally:
            clock.stop()
        self.assertTrue(source.is_file())
        self.assertEqual(len(self.bus.pending("workbuddy")), 0)

    def test_corrupt_config_during_write_blocks_publication(self):
        msg = self.make_claim()
        source = self.bus.processing("workbuddy")[0]
        write = self.bus._write
        def corrupt(path, message):
            write(path, message)
            if path == source:
                (self.bus.root / "config.json").write_text("{broken", encoding="utf-8")
        with patch.object(self.bus, "_write", corrupt):
            with self.assertRaisesRegex(BusError, "配置"):
                self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertTrue(source.is_file())
        self.assertEqual(len(self.bus.pending("workbuddy")), 0)

    def test_missing_bound_job_is_not_treated_as_unbound_legacy_work(self):
        job, msg = self.bound_claim()
        (self.bus.root / "jobs" / (job["id"] + ".json")).unlink()
        with self.assertRaisesRegex(BusError, "作业"):
            self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)

    def test_disabled_member_cannot_recover_new_work(self):
        msg = self.make_claim()
        self.cfg.data["workers"]["workbuddy"]["enabled"] = False
        save(self.cfg)
        with self.assertRaisesRegex(BusError, "成员"):
            self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)

    def test_lease_lock_link_is_refused_before_opening_target(self):
        from agentbus.lease import LeaseManager
        msg = self.make_claim()
        lock = LeaseManager(self.bus.root).root / ".mutation.lock"
        if lock.exists():
            lock.unlink()  # This test's isolated fixture only.
        target = Path(self.temp.name) / "outside-bus-sentinel.txt"
        target.write_text("preserve", encoding="utf-8")
        try:
            lock.symlink_to(target)
        except OSError:
            self.skipTest("Windows account cannot create isolated file symlinks")
        os_open = os.open
        def forbidden(path, *args, **kwargs):
            if Path(path) == lock:
                self.fail("unsafe lease lock was opened")
            return os_open(path, *args, **kwargs)
        with patch("agentbus.lease.os.open", forbidden):
            with self.assertRaises(BusError):
                self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve")

    def test_lease_record_link_is_refused_before_reading_target(self):
        from agentbus.lease import LeaseManager
        msg = self.make_claim()
        manager = LeaseManager(self.bus.root)
        record = manager._file("agentbus/report/" + msg.id)
        target = Path(self.temp.name) / "outside-bus-sentinel.json"
        target.write_text("{}", encoding="utf-8")
        try:
            record.symlink_to(target)
        except OSError:
            self.skipTest("Windows account cannot create isolated file symlinks")
        read = Path.read_text
        def forbidden(path, *args, **kwargs):
            if path == record:
                self.fail("unsafe lease record was read")
            return read(path, *args, **kwargs)
        with patch.object(Path, "read_text", forbidden):
            with self.assertRaises(BusError):
                self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_resolved_internal_lease_escape_is_refused_before_acquire(self):
        from agentbus.lease import LeaseManager
        msg = self.make_claim()
        manager = LeaseManager(self.bus.root)
        lease_paths = (manager.root / ".mutation.lock", manager._file("agentbus/report/" + msg.id))
        resolve = Path.resolve
        outside = Path(self.temp.name) / "outside-bus-sentinel"
        for lease_path in lease_paths:
            with self.subTest(path=lease_path.name):
                def escaped(path, *args, **kwargs):
                    if path == lease_path:
                        return outside
                    return resolve(path, *args, **kwargs)
                def no_acquire(*args, **kwargs):
                    self.fail("recovery acquired an unsafe internal lease path")
                with patch.object(Path, "resolve", escaped), patch.object(LeaseManager, "acquire", no_acquire):
                    with self.assertRaises(BusError):
                        self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)

    def test_stop_blocks_recovery_without_touching_claim(self):
        msg = self.make_claim()
        stop = self.bus.root / "STOP"
        stop.write_text("operator stop", encoding="utf-8")
        with self.assertRaisesRegex(BusError, "STOP"):
            self.bus.recover_claim("workbuddy", msg.id, owner_confirmed_inactive=True)
        self.assertEqual(len(self.bus.processing("workbuddy")), 1)


if __name__ == "__main__":
    unittest.main()
