"""Runtime closeout regressions, Owner-authorized review, 2026-10-14.

All subprocesses are harmless Python fixtures; no vendor agents or desktop UI.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.config import load
from agentbus.claim_fencing import captured_ticket
from agentbus.headless_spawn import HeadlessSpawnEngine
from agentbus.heartbeat import heartbeat_path
from agentbus.task_tree import TaskForest
from agentbus.worker_loop import HeartbeatKeeper, WorkerLoop


class RuntimeCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentbus-runtime-closeout-")
        self.root = Path(self.temp.name)
        self.bus_root = self.root / ".agentbus"
        self.bus_root.mkdir()
        (self.bus_root / "config.json").write_text(json.dumps({
            "director": "codex", "output_root": str(self.root),
            "workers": {"codex": {"enabled": True, "executor_enabled": True,
                "can_spawn": True, "communication_mode": "cli",
                "capabilities": ["python"],
                "command": [sys.executable, "-c", "import time; time.sleep(.5)"]}},
            "gates": {"heartbeat_timeout_s": 600, "max_retries": 2},
        }), encoding="utf-8")
        (self.bus_root / "task-tree.json").write_text(json.dumps({
            "schema_version": 1, "section_registry": ["0", "1", "1.1"],
            "roots": [{"id": "root", "title": "Runtime fixture"}],
            "branches": [{"id": "branch", "parent_id": "root", "section_no": "1", "title": "Tests"}],
            "leaves": [{"id": "RUNTIME-ONE", "parent_id": "branch", "section_no": "1.1",
                "title": "Harmless fixture", "goal": "Exercise local runtime", "dependencies": [],
                "required_capabilities": ["python"], "expected_artifacts": [], "handoff": "Fixture"}],
        }), encoding="utf-8")
        self.cfg = load(self.root)
        self.forest = TaskForest(self.cfg)
        self.engine = HeadlessSpawnEngine(self.cfg)

    def tearDown(self):
        self.engine.terminate_all_active()
        self.temp.cleanup()

    def leaf(self):
        return self.forest._leaf(self.forest.snapshot(), "RUNTIME-ONE")

    def keeper_once(self, keeper):
        def done(_delay):
            keeper._stop_event.set()
            return True
        keeper._stop_event.wait = done
        keeper._run()

    def test_no_handler_leaves_task_unclaimed_and_attempts_once(self):
        loop = WorkerLoop(self.cfg, "codex", max_tasks=3, timeout_s=.7, poll_interval=.2)
        report = loop.run(max_idle_loops=1)
        self.assertEqual(report["total_processed"], 1)
        self.assertEqual(self.leaf()["state"], "queued")
        self.assertEqual(report["succeeded"], 0)

    def test_handler_does_not_repeat_same_claim_when_no_receipt_written(self):
        executions = []
        loop = WorkerLoop(self.cfg, "codex", max_tasks=3, timeout_s=.7, poll_interval=.2,
                          handler=lambda leaf: executions.append(leaf["id"]))
        report = loop.run(max_idle_loops=1)
        self.assertEqual(executions, ["RUNTIME-ONE"])
        self.assertEqual(report["total_processed"], 1)
        self.assertEqual(report["succeeded"], 0)
        self.assertEqual(report["awaiting_delivery"], 1)

    def test_same_member_claim_from_another_session_does_not_run_handler(self):
        self.forest.claim_leaf("RUNTIME-ONE", "codex")
        executions = []
        loop = WorkerLoop(self.cfg, "codex", handler=lambda leaf: executions.append(leaf["id"]))
        result = loop.execute_task(self.leaf())
        self.assertFalse(result["success"])
        self.assertEqual(executions, [])
        self.assertIn("TICKET", result["error"])

    def test_handler_declared_failure_is_reported_as_failure(self):
        result = WorkerLoop(self.cfg, "codex", handler=lambda leaf: {"success": False}).execute_task(self.leaf())
        self.assertFalse(result["success"])

    def test_handler_receives_original_ticket_and_initial_explicit_heartbeat(self):
        seen = []
        def handler(leaf):
            seen.append(leaf.get("claim_ticket"))
            hb = json.loads(heartbeat_path(self.bus_root, leaf["task_id"], "codex").read_text(encoding="utf-8"))
            self.assertEqual(hb["claim_id"], leaf["claim_ticket"]["claim_id"])
        result = WorkerLoop(self.cfg, "codex", handler=handler).execute_task(self.leaf())
        self.assertTrue(result["handled"], result["error"])
        self.assertFalse(result["success"])
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], dict)

    def test_original_ticket_handler_delivers_one_verified_receipt(self):
        receipts = []
        def handler(leaf):
            ticket = leaf["claim_ticket"]
            current = self.forest.bus._active_record("codex", leaf["task_id"])
            self.forest.bus.heartbeat("codex", current, ticket)
            artifact = self.root / "runtime-result.txt"
            artifact.write_text("verified harmless result", encoding="utf-8")
            manifest = [{"path": "runtime-result.txt", "bytes": artifact.stat().st_size,
                         "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]
            receipts.append(self.forest.bus.commit_result("codex", current, ticket,
                body="Harmless fixture delivered", artifacts=manifest, exit_code=0))
        report = WorkerLoop(self.cfg, "codex", handler=handler, poll_interval=.2).run(max_idle_loops=1)
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(report["total_processed"], 1)
        self.assertEqual(len(self.forest.bus.find_results(receipts[0].reply_to)), 1)
        self.assertEqual(self.leaf()["state"], "delivered")

    def test_stop_requested_during_handler_does_not_count_as_success(self):
        def handler(leaf):
            (self.bus_root / "STOP").write_text("stop", encoding="utf-8")
        result = WorkerLoop(self.cfg, "codex", handler=handler).execute_task(self.leaf())
        self.assertFalse(result["success"])
        self.assertIn("STOP_REQUESTED", result["error"])

    def test_keeper_does_not_refresh_heartbeat_missing_claim_id(self):
        msg = self.forest.claim_leaf("RUNTIME-ONE", "codex")
        ticket = captured_ticket(msg)
        self.forest.bus.heartbeat("codex", msg, ticket)
        path = heartbeat_path(self.bus_root, msg.id, "codex")
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("claim_id")
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.keeper_once(HeartbeatKeeper(self.bus_root, msg.id, "codex", ticket=ticket.to_dict()))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_keeper_does_not_touch_corrupt_heartbeat(self):
        msg = self.forest.claim_leaf("RUNTIME-ONE", "codex")
        ticket = captured_ticket(msg)
        path = heartbeat_path(self.bus_root, msg.id, "codex")
        path.parent.mkdir(parents=True)
        path.write_text("{corrupt", encoding="utf-8")
        before = path.read_bytes(), path.stat().st_mtime_ns
        time.sleep(.02)
        self.keeper_once(HeartbeatKeeper(self.bus_root, msg.id, "codex", ticket=ticket.to_dict()))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_spawn_without_explicit_adapter_does_not_claim_or_start(self):
        self.cfg.data["workers"]["codex"].pop("command")
        result = self.engine.spawn_leaf(self.leaf(), "codex", wait=False)
        self.assertFalse(result.started)
        self.assertIn("ADAPTER", result.error)
        self.assertEqual(self.leaf()["state"], "queued")

    def test_spawn_cannot_adopt_another_session_claim_for_same_member(self):
        self.forest.claim_leaf("RUNTIME-ONE", "codex")
        result = self.engine.spawn_leaf(self.leaf(), "codex", wait=False)
        self.assertFalse(result.started)
        self.assertIn("TICKET", result.error)

    def test_process_exit_zero_without_receipt_does_not_declare_delivery(self):
        self.cfg.data["workers"]["codex"]["command"] = [sys.executable, "-c", "pass"]
        result = self.engine.spawn_leaf(self.leaf(), "codex")
        self.assertTrue(result.started)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.success)
        self.assertIn("DELIVERY_UNVERIFIED", result.error)

    def test_async_watchdog_reaps_timeout_without_explicit_cleanup(self):
        result = self.engine.spawn_leaf(self.leaf(), "codex", timeout_s=.1, wait=False)
        self.assertTrue(result.started)
        proc = self.engine.active_processes[result.task_id]["proc"]
        limit = time.monotonic() + 2
        while result.task_id in self.engine.active_processes and time.monotonic() < limit:
            time.sleep(.02)
        self.assertIsNotNone(proc.poll())
        self.assertNotIn(result.task_id, self.engine.active_processes)

    def test_async_watchdog_reaps_stop_request(self):
        result = self.engine.spawn_leaf(self.leaf(), "codex", timeout_s=5, wait=False)
        self.assertTrue(result.started)
        proc = self.engine.active_processes[result.task_id]["proc"]
        (self.bus_root / "STOP").write_text("stop", encoding="utf-8")
        limit = time.monotonic() + 2
        while result.task_id in self.engine.active_processes and time.monotonic() < limit:
            time.sleep(.02)
        self.assertIsNotNone(proc.poll())
        self.assertNotIn(result.task_id, self.engine.active_processes)

    def test_zero_spawn_limit_does_not_claim_or_start_any_task(self):
        self.assertEqual(self.engine.auto_spawn_ready(max_spawn=0), [])
        self.assertEqual(self.leaf()["state"], "queued")

    def test_original_spawn_ticket_is_handed_to_adapter_for_verified_delivery(self):
        source_root = str(Path(__file__).resolve().parents[1])
        script = (
            f"import sys; sys.path.insert(0, {source_root!r}); "
            "import os,json,hashlib; from pathlib import Path; from agentbus.bus import Bus; "
            "ticket=json.loads(os.environ['AGENTBUS_CLAIM_TICKET']); "
            "root=Path(os.environ['AGENTBUS_WORKSPACE']); bus=Bus(root/'.agentbus'); "
            "msg=bus._active_record(ticket['agent'],ticket['task_id']); "
            "bus.heartbeat(ticket['agent'],msg,ticket); p=root/'spawn-result.txt'; p.write_text('verified'); "
            "manifest=[{'path':'spawn-result.txt','bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}]; "
            "bus.commit_result(ticket['agent'],msg,ticket,body='Verified synthetic adapter',artifacts=manifest,exit_code=0)"
        )
        self.cfg.data["workers"]["codex"]["command"] = [sys.executable, "-c", script]
        result = self.engine.spawn_leaf(self.leaf(), "codex")
        self.assertTrue(result.success, result.error + "\n" + Path(result.log_path).read_text(encoding="utf-8"))
        self.assertTrue(result.process_success)
        self.assertEqual(self.leaf()["state"], "delivered")

    def test_process_tree_children_are_reaped_when_parent_exits_normally(self):
        marker = self.root / "orphan-marker.txt"
        child = f"import time; from pathlib import Path; time.sleep(1.5); Path({str(marker)!r}).write_text('orphan')"
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}], stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(.2)"
        self.cfg.data["workers"]["codex"]["command"] = [sys.executable, "-c", parent]
        result = self.engine.spawn_leaf(self.leaf(), "codex")
        self.assertEqual(result.exit_code, 0)
        time.sleep(1.7)
        self.assertFalse(marker.exists(), "A detached child survived its supervised parent")

    def test_concurrent_spawns_reserve_before_popen(self):
        entered = threading.Event()
        release = threading.Event()
        actual_popen = subprocess.Popen
        spawned = []
        results = []
        def delayed_popen(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                release.wait(2)
            process = actual_popen(*args, **kwargs)
            spawned.append(process)
            return process
        def first_spawn():
            results.append(self.engine.spawn_leaf(self.leaf(), "codex", wait=False))
        with patch("agentbus.headless_spawn.subprocess.Popen", side_effect=delayed_popen):
            thread = threading.Thread(target=first_spawn)
            thread.start()
            self.assertTrue(entered.wait(2))
            results.append(self.engine.spawn_leaf(self.leaf(), "codex", wait=False))
            release.set()
            thread.join(3)
        self.engine.terminate_all_active()
        for process in spawned:
            process.wait(timeout=2)
        self.assertEqual(sum(result.started for result in results), 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows job assignment boundary")
    def test_process_cannot_execute_before_job_assignment_succeeds(self):
        marker = self.root / "premature-process.txt"
        self.cfg.data["workers"]["codex"]["command"] = [sys.executable, "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('premature')"]
        roots = []
        class RefusedJob:
            def attach_and_resume(self, proc):
                roots.append(proc)
                time.sleep(.3)
                raise PermissionError("synthetic job assignment refusal")
            def close(self):
                pass
        with patch.object(self.engine, "_windows_job", return_value=RefusedJob()):
            result = self.engine.spawn_leaf(self.leaf(), "codex")
        self.assertFalse(result.success)
        self.assertFalse(result.started)
        self.assertIsNotNone(roots[0].poll(), "Suspended process must be reaped before returning")
        self.assertFalse(marker.exists(), "Process ran before supervision was established")

    def test_zero_worker_time_budget_does_not_invoke_handler(self):
        executions = []
        report = WorkerLoop(self.cfg, "codex", timeout_s=0, max_tasks=1,
                            handler=lambda leaf: executions.append(leaf["id"])).run()
        self.assertEqual(executions, [])
        self.assertEqual(report["total_processed"], 0)


if __name__ == "__main__":
    unittest.main()
