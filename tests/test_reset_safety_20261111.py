"""Reset must preserve live claims, deadlines, gates, and pending review."""
import json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from agentbus.config import load
from agentbus.task_tree import TaskForest, TaskTreeError
from agentbus.claim_fencing import captured_ticket

class ResetSafetyTests(unittest.TestCase):
    def active_fixture(self, ws):
        control = ws / ".agentbus"; control.mkdir()
        (control / "config.json").write_text(json.dumps({"version": 1, "workers": {
            "codex": {"enabled": True}, "worker": {"enabled": True}}, "director": "codex",
            "communication_mode": "file", "assignment_mode": "autonomous"}), encoding="utf-8")
        forest = TaskForest(load(ws))
        forest.replace_structure({"schema_version": 1, "roots": [{"id": "R", "title": "test"}],
            "branches": [{"id": "B", "parent_id": "R", "title": "branch"}],
            "leaves": [{"id": "A", "parent_id": "B", "title": "task", "goal": "test"}]})
        task = forest.claim_leaf("A", "worker")
        forest.bus.heartbeat("worker", task, captured_ticket(task), note="active")
        return control, forest, task

    def test_live_claim_without_confirmation_keeps_original_bytes(self):
        for assertion in (False, None, 1, "true"):
            with self.subTest(assertion=assertion), tempfile.TemporaryDirectory() as temp:
                control, forest, task = self.active_fixture(Path(temp))
                processing = forest.bus.dirs("worker")["processing"] / (task.id + ".json")
                tree = control / "task-tree.json"; before = (processing.read_bytes(), tree.read_bytes())
                with self.assertRaisesRegex(TaskTreeError, "明确确认"):
                    forest.reset_leaf("A", "no explicit confirmation", owner_confirmed_inactive=assertion)
                self.assertEqual((processing.read_bytes(), tree.read_bytes()), before)
                self.assertIsNotNone(forest.bus.heartbeat("worker", task, captured_ticket(task), note="still works"))

    def test_expired_job_never_extends_shared_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp); control = ws / ".agentbus"; control.mkdir()
            (control / "config.json").write_text(json.dumps({"version": 1,
                "workers": {"codex": {"enabled": True}}, "director": "codex"}), encoding="utf-8")
            forest = TaskForest(load(ws))
            forest.replace_structure({"schema_version": 1, "roots": [{"id": "R", "title": "test"}],
                "branches": [{"id": "B", "parent_id": "R", "title": "branch"}],
                "leaves": [{"id": "A", "parent_id": "B", "title": "task", "job_id": "job"}]})
            tree = control / "task-tree.json"; original = tree.read_bytes()
            with patch("agentbus.task_tree.jobs._load", return_value={"deadline_ts": time.time() - 60}), patch("agentbus.task_tree.jobs._save") as save:
                with self.assertRaisesRegex(TaskTreeError, "截止|期限"):
                    forest.reset_leaf("A", "expired job")
                save.assert_not_called()
            self.assertEqual(tree.read_bytes(), original)

    def test_confirmed_reset_does_not_bypass_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp))
            processing = forest.bus.dirs("worker")["processing"] / (task.id + ".json")
            tree = control / "task-tree.json"; before = (processing.read_bytes(), tree.read_bytes())
            (control / "STOP").write_text("stop", encoding="utf-8")
            with self.assertRaisesRegex(TaskTreeError, "释放|STOP"):
                forest.reset_leaf("A", "confirmed stopped", owner_confirmed_inactive=True, expected_task_id=task.id, expected_owner="worker", expected_claim_epoch=task.claim_epoch)
            self.assertEqual((processing.read_bytes(), tree.read_bytes()), before)

    def test_confirmed_inactive_reset_permits_formal_new_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp))
            forest.reset_leaf("A", "confirmed stopped", owner_confirmed_inactive=True, expected_task_id=task.id, expected_owner="worker", expected_claim_epoch=task.claim_epoch)
            self.assertTrue((forest.bus.dirs("worker")["blocked"] / (task.id + ".json")).is_file())
            successor = forest.claim_leaf("A", "codex")
            self.assertIsNotNone(successor)
            self.assertLessEqual(successor.deadline_ts, task.deadline_ts)
            self.assertLessEqual(successor.budget_s, task.budget_s)
            self.assertEqual(successor.attempt, task.attempt + 1)

    def test_pending_audit_is_preserved_even_if_stopped_asserted(self):
        from test_review_20261108_admin_pass import AdministratorPassRegression
        case = AdministratorPassRegression("test_pending_review_cannot_be_passed_or_projected_as_delivered"); case.setUp()
        try:
            bus, task, ticket = case._start_supervised_work(); case._submit_work(bus, task, ticket)
            task_path = bus.dirs("workbuddy")["processing"] / (task.id + ".json"); before = task_path.read_bytes()
            with self.assertRaisesRegex(TaskTreeError, "审核"):
                case.forest.reset_leaf("A", "try bypassing audit", owner_confirmed_inactive=True)
            self.assertEqual(task_path.read_bytes(), before)
            self.assertEqual(case.forest.snapshot()["leaves"][0]["state"], "awaiting_audit")
        finally: case.tearDown()

    def test_web_bridge_requires_literal_confirmation(self):
        from agentbus.web_bridge import WebBridge
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp)); bridge = WebBridge(Path(temp))
            rejected = bridge.call("reset_leaf", {"leaf_id":"A", "reason":"unconfirmed"})
            self.assertFalse(rejected["ok"])
            fake = bridge.call("reset_leaf", {"leaf_id":"A", "reason":"fake", "owner_confirmed_inactive":"true"})
            self.assertFalse(fake["ok"])
            accepted = bridge.call("reset_leaf", {"leaf_id":"A", "reason":"confirmed", "owner_confirmed_inactive":True, "expected_task_id":task.id, "expected_owner":"worker", "expected_claim_epoch":task.claim_epoch})
            self.assertTrue(accepted["ok"], accepted)

    def test_cli_requires_flag_to_assert_owner_stopped(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from agentbus.cli import main
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp)); args = ["tree", "reset", "--workspace", temp, "--leaf", "A", "--reason", "fixture"]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertNotEqual(main(args), 0)
                self.assertEqual(main(args + ["--owner-confirmed-inactive", "--id",task.id,"--expected-owner","worker","--claim-epoch",str(task.claim_epoch)]), 0)

    def test_mcp_requires_literal_stop_assertion(self):
        from agentbus.mcp_server import AgentBusMcp
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp)); mcp=AgentBusMcp(Path(temp))
            with self.assertRaisesRegex(TaskTreeError, "明确确认"):
                mcp.reset_leaf("A", "unconfirmed")
            with self.assertRaisesRegex(TaskTreeError, "明确确认"):
                mcp.reset_leaf("A", "fake", owner_confirmed_inactive="true")
            accepted=json.loads(mcp.reset_leaf("A", "confirmed", owner_confirmed_inactive=True, expected_task_id=task.id, expected_owner="worker", expected_claim_epoch=task.claim_epoch))
            self.assertTrue(accepted["ok"])

    def test_retry_after_retirement_before_tree_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp))
            with patch.object(forest, "_write", side_effect=OSError("simulated interruption")):
                with self.assertRaises(OSError):
                    forest.reset_leaf("A", "same reason", owner_confirmed_inactive=True, expected_task_id=task.id, expected_owner="worker", expected_claim_epoch=task.claim_epoch)
            self.assertTrue((forest.bus.dirs("worker")["blocked"] / (task.id + ".json")).is_file())
            forest.reset_leaf("A", "same reason", owner_confirmed_inactive=True, expected_task_id=task.id, expected_owner="worker", expected_claim_epoch=task.claim_epoch)
            self.assertIsNotNone(forest.claim_leaf("A", "codex"))

    def test_missing_original_does_not_clear_leaf_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp))
            processing = forest.bus.dirs("worker")["processing"] / (task.id + ".json")
            processing.unlink(); tree=control/"task-tree.json"; before=tree.read_bytes()
            with self.assertRaisesRegex(TaskTreeError, "缺失|读取"):
                forest.reset_leaf("A", "missing", owner_confirmed_inactive=True)
            self.assertEqual(tree.read_bytes(), before)

    def test_duplicate_task_id_in_other_mailbox_is_not_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp))
            processing=forest.bus.dirs("worker")["processing"]/(task.id+".json")
            duplicate=forest.bus.dirs("codex")["ready"]/(task.id+".json")
            duplicate.write_bytes(processing.read_bytes()); tree=control/"task-tree.json"
            before=(processing.read_bytes(),duplicate.read_bytes(),tree.read_bytes())
            with self.assertRaisesRegex(TaskTreeError, "多份|冲突"):
                forest.reset_leaf("A", "duplicate", owner_confirmed_inactive=True)
            self.assertEqual((processing.read_bytes(),duplicate.read_bytes(),tree.read_bytes()),before)

    def test_two_process_reset_and_result_cannot_both_win(self):
        import os, subprocess, sys
        worker_code = r"""
import json, pathlib, sys, time
from agentbus.config import load
from agentbus.task_tree import TaskForest
from agentbus.bus import Bus, Message
ws=pathlib.Path(sys.argv[1]); mode=sys.argv[2]; data=json.loads((ws/'fixture.json').read_text(encoding='utf8'))
(ws/(mode+'.ready')).write_text('ready',encoding='utf8')
end=time.monotonic()+15
while not (ws/'go').exists():
    if time.monotonic()>end: raise SystemExit('barrier timeout')
    time.sleep(.005)
try:
    if mode=='reset': TaskForest(load(ws)).reset_leaf('A','confirmed stopped',owner_confirmed_inactive=True,expected_task_id=data['task']['id'],expected_owner='worker',expected_claim_epoch=data['task']['claim_epoch'])
    else: Bus(ws/'.agentbus').commit_result('worker',Message.from_dict(data['task'],strict=True),data['ticket'],body='done')
    result={'ok':True}
except Exception as exc: result={'ok':False,'error':type(exc).__name__}
(ws/(mode+'.result')).write_text(json.dumps(result),encoding='utf8')
"""
        for _ in range(3):
            with tempfile.TemporaryDirectory() as temp:
                ws=Path(temp); control,forest,task=self.active_fixture(ws);ticket=captured_ticket(task)
                (ws/'fixture.json').write_text(json.dumps({'task':task.to_dict(),'ticket':ticket.to_dict()}),encoding='utf8')
                env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]),PYTHONUTF8='1')
                children=[subprocess.Popen([sys.executable,'-B','-X','utf8','-c',worker_code,temp,mode],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for mode in ('reset','result')]
                try:
                    end=time.monotonic()+15
                    while not all((ws/(mode+'.ready')).exists() for mode in ('reset','result')):
                        if time.monotonic()>end: self.fail('subprocess barrier timed out')
                        time.sleep(.01)
                    (ws/'go').write_text('go',encoding='utf8')
                    for child in children:
                        stdout,stderr=child.communicate(timeout=30)
                        self.assertEqual(child.returncode,0,(stdout+stderr).decode('utf8','replace'))
                    results=[json.loads((ws/(mode+'.result')).read_text(encoding='utf8')) for mode in ('reset','result')]
                    self.assertEqual(sum(item['ok'] for item in results),1,results)
                    message=forest.bus.find_message(task.id)
                    self.assertIsNotNone(message)
                    if results[0]['ok']:
                        self.assertEqual(message.status, 'blocked')
                        self.assertEqual(forest.resolve_leaf('A').get('history_task_ids'), [task.id])
                        self.assertIsNone(forest.bus.find_result(task.id, sender='worker', recipient='codex'))
                        with self.assertRaises(Exception):
                            forest.bus.check_claim('worker', message, ticket)
                    else:
                        self.assertEqual(message.status, 'done')
                        self.assertEqual(forest.resolve_leaf('A').get('task_id'), task.id)
                        self.assertIsNotNone(forest.bus.find_result(task.id, sender='worker', recipient='codex'))
                finally:
                    for child in children:
                        if child.poll() is None: child.kill();child.wait()

    def test_retry_after_blocked_write_before_processing_unlink(self):
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp))
            processing=forest.bus.dirs("worker")["processing"]/(task.id+".json")
            blocked=forest.bus.dirs("worker")["blocked"]/(task.id+".json")
            original_unlink=Path.unlink
            def interrupted(path, *args, **kwargs):
                if path == processing: raise OSError("simulated crash after blocked commit")
                return original_unlink(path,*args,**kwargs)
            with patch.object(Path,"unlink",interrupted):
                with self.assertRaises((OSError, TaskTreeError)):
                    forest.reset_leaf("A", "same reason", owner_confirmed_inactive=True, expected_task_id=task.id, expected_owner="worker", expected_claim_epoch=task.claim_epoch)
            self.assertTrue(processing.is_file());self.assertTrue(blocked.is_file())
            forest.reset_leaf("A", "same reason", owner_confirmed_inactive=True, expected_task_id=task.id, expected_owner="worker", expected_claim_epoch=task.claim_epoch)
            self.assertFalse(processing.exists());self.assertTrue(blocked.is_file())
            self.assertIsNotNone(forest.claim_leaf("A", "codex"))

    def test_full_handoff_history_does_not_retire_live_task(self):
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.active_fixture(Path(temp));tree=forest._structure();leaf=tree["leaves"][0]
            leaf["history_task_ids"]=[f"old-{i}" for i in range(100)]
            leaf["handoff_history"]=[{"task_id":f"old-{i}","agent":"worker","conversation_id":"",
                "timestamp":"2026-11-11T12:00:00+08:00","reason":"prior","owner_confirmed_inactive":True} for i in range(100)]
            forest._write(tree);tree_path=control/"task-tree.json";proc=forest.bus.dirs("worker")["processing"]/(task.id+".json");before=(tree_path.read_bytes(),proc.read_bytes())
            with self.assertRaisesRegex(TaskTreeError,"历史|上限"):
                forest.reset_leaf("A","confirmed stopped",owner_confirmed_inactive=True)
            self.assertEqual((tree_path.read_bytes(),proc.read_bytes()),before)

    def test_real_expired_shared_job_and_other_leaf_remain_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task = self.active_fixture(Path(temp)); tree=forest._structure()
            tree["leaves"].append({"id":"B2","parent_id":"B","title":"other","goal":"other"})
            forest._write(tree); jobs_dir=control/"jobs";job=jobs_dir/(task.job_id+".json")
            data=json.loads(job.read_text(encoding="utf8"));data["deadline_ts"]=time.time()-60
            job.write_text(json.dumps(data),encoding="utf8"); processing=forest.bus.dirs("worker")["processing"]/(task.id+".json");treepath=control/"task-tree.json"
            before=(job.read_bytes(),processing.read_bytes(),treepath.read_bytes())
            with self.assertRaisesRegex(TaskTreeError,"截止|期限"):
                forest.reset_leaf("A","expired",owner_confirmed_inactive=True)
            self.assertEqual((job.read_bytes(),processing.read_bytes(),treepath.read_bytes()),before)

    def test_old_confirmation_cannot_release_new_claim_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task=self.active_fixture(Path(temp))
            # Simulate confirmation for the preceding claim generation.
            with self.assertRaisesRegex(TaskTreeError,"变更|重新确认"):
                forest.reset_leaf("A","stale",owner_confirmed_inactive=True,
                    expected_task_id=task.id,expected_owner="worker",expected_claim_epoch=task.claim_epoch-1)
            self.assertTrue((forest.bus.dirs("worker")["processing"]/(task.id+".json")).is_file())

    def test_desktop_recovery_rejects_stale_epoch_without_mutation(self):
        from agentbus.web_bridge import WebBridge
        with tempfile.TemporaryDirectory() as temp:
            control, forest, task=self.active_fixture(Path(temp)); path=forest.bus.dirs("worker")["processing"]/(task.id+".json"); before=path.read_bytes()
            result=WebBridge(Path(temp)).call("recover_task", {"task_id":task.id,"agent":"worker", "owner_confirmed_inactive":True,"expected_claim_epoch":task.claim_epoch+1})
            self.assertFalse(result["ok"]);self.assertIn("变更",result["error"]);self.assertEqual(path.read_bytes(),before)

    def test_audit_failure_after_tree_write_reports_already_applied(self):
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.active_fixture(Path(temp)); original=forest.bus.audit
            def fail_final(**event):
                if event.get("event")=="leaf_reset_by_admin": raise OSError("audit unavailable")
                return original(**event)
            with patch.object(forest.bus,"audit",side_effect=fail_final):
                with self.assertRaisesRegex(TaskTreeError,"已生效"):
                    forest.reset_leaf("A","confirmed",owner_confirmed_inactive=True,
                        expected_task_id=task.id,expected_owner="worker",expected_claim_epoch=task.claim_epoch)
            leaf=forest.resolve_leaf("A")
            self.assertNotIn("task_id",leaf)
            self.assertIn(task.id,leaf["history_task_ids"])

    def test_repeated_reset_does_not_reset_retry_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            control,forest,first=self.active_fixture(Path(temp)); current=first
            for actor,next_actor in (("worker","codex"),("codex","worker")):
                forest.reset_leaf("A","confirmed",owner_confirmed_inactive=True,
                    expected_task_id=current.id,expected_owner=actor,expected_claim_epoch=current.claim_epoch)
                current=forest.claim_leaf("A",next_actor)
            self.assertEqual(current.attempt, 2)
            processing=forest.bus.dirs("worker")["processing"]/(current.id+".json"); before=processing.read_bytes()
            with self.assertRaisesRegex(TaskTreeError,"重试|上限"):
                forest.reset_leaf("A","third reset",owner_confirmed_inactive=True,
                    expected_task_id=current.id,expected_owner="worker",expected_claim_epoch=current.claim_epoch)
            self.assertEqual(processing.read_bytes(),before)

    def test_rejected_audit_remains_intact_for_future_formal_redo(self):
        from test_review_20261110_audit_deadlock import RejectedReasonTests
        case=RejectedReasonTests("test_rejection_explains_review_not_missing_executor");case.setUp()
        try:
            bridge,forest=case.pending()
            self.assertTrue(bridge.call("claim_audit", {"leaf_id":"work","agent":"reviewer"})["ok"])
            self.assertTrue(case.decision(bridge, decision="reject", reason="needs repair")["ok"])
            leaf=forest.snapshot()["leaves"][0];original=forest.bus.find_message(leaf["task_id"])
            blocked=forest.bus.dirs(original.to)["blocked"]/(original.id+".json");before=blocked.read_bytes()
            with self.assertRaisesRegex(TaskTreeError,"审核|驳回"):
                forest.reset_leaf("work","redo",owner_confirmed_inactive=True,
                    expected_task_id=original.id,expected_owner=original.to,expected_claim_epoch=original.claim_epoch)
            self.assertEqual(blocked.read_bytes(),before)
            self.assertEqual(forest.snapshot()["leaves"][0]["state"],"rejected")
        finally:case.tearDown()

    def test_confirmation_for_another_owner_or_task_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.active_fixture(Path(temp));path=forest.bus.dirs("worker")["processing"]/(task.id+".json");before=path.read_bytes()
            for identity, owner in ((task.id,"codex"),("m-other","worker")):
                with self.subTest(identity=identity,owner=owner),self.assertRaisesRegex(TaskTreeError,"变更|重新确认"):
                    forest.reset_leaf("A","stale",owner_confirmed_inactive=True,expected_task_id=identity,
                        expected_owner=owner,expected_claim_epoch=task.claim_epoch)
                self.assertEqual(path.read_bytes(),before)

if __name__ == "__main__": unittest.main()
