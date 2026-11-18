"""Codex integration E8/E9 regression evidence, 2026-10-08.

Real tempfile bus and CLI/MCP entrypoints; no model or vendor calls.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agentbus.bus import Bus, BusError
from agentbus import claim_fencing as F, heartbeat as H
from agentbus.cli import main
from agentbus.config import load, save
from agentbus.mcp_server import AgentBusMcp
from agentbus.director import Director, Summary
from agentbus.adapters import Result
from agentbus import jobs


class FencingEntrypoints(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.cfg = load(self.workspace, create=True)
        self.cfg.data['gates']['max_retries'] = 3
        save(self.cfg)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()
        self.task = self.bus.make('codex', 'workbuddy', 'task', body='work', result_to='codex')
        self.bus.post(self.task)

    def claim(self):
        self.claimed = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        self.ticket = F.normalize({'task_id': self.claimed.id, 'agent': 'workbuddy',
                                  'claim_epoch': self.claimed.claim_epoch,
                                  'claim_id': self.claimed.claim_id})
        self.bus.heartbeat('workbuddy', self.claimed, self.ticket, note='test executor is alive')
        return self.claimed

    def cli(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(args)
        return code, output.getvalue()

    def test_direct_result_without_ticket_cannot_enter_ready(self):
        self.claim()
        reply = self.bus.make('workbuddy', 'codex', 'result', reply_to=self.task.id, body='missing')
        with self.assertRaises(BusError):
            self.bus.post(reply)
        self.assertEqual(self.bus.find_results(self.task.id), [])

    def test_revoked_record_rejects_ticketless_finish(self):
        self.claim()
        self.bus.recover_claim('workbuddy', self.task.id, owner_confirmed_inactive=True)
        recovered = self.bus.read(self.bus.pending('workbuddy')[0])
        with self.assertRaises(BusError):
            self.bus.finish('workbuddy', recovered)
        self.assertEqual(recovered.claim_id, '')
        self.assertEqual(recovered.claim_epoch, 1)

    def test_recovery_clears_ticket_and_old_heartbeat(self):
        self.claim()
        H.record(self.bus.root, self.task.id, 'workbuddy', self.ticket,
                 task_record=self.claimed.to_dict())
        recovered = self.bus.recover_claim('workbuddy', self.task.id, owner_confirmed_inactive=True)
        self.assertEqual(recovered.claim_epoch, 1)
        self.assertEqual(recovered.claim_id, '')
        self.assertTrue(H.state(self.bus.root, self.task.id, 'workbuddy',
                               task_record=recovered.to_dict())['stale'])
        with self.assertRaises(BusError):
            self.bus.commit_result('workbuddy', self.claimed, self.ticket, body='late')

    def test_cli_claim_returns_original_ticket_for_followup(self):
        code, output = self.cli(['claim', '--workspace', str(self.workspace), '--agent', 'workbuddy',
                                 '--id', self.task.id])
        self.assertEqual(code, 0, output)
        line = next((line for line in output.splitlines() if line.startswith('{')), '{}')
        ticket = json.loads(line)
        self.assertEqual(ticket.get('task_id'), self.task.id)
        self.assertEqual(ticket.get('claim_epoch'), 1)
        self.assertRegex(ticket.get('claim_id', ''), r'^[0-9a-f]{32}$')

    def test_cli_answer_without_ticket_rejects_before_post(self):
        self.claim()
        self.assertEqual([p.stem for p in self.bus.processing('workbuddy')], [self.task.id])
        self.assertEqual(self.bus.pending('workbuddy'), [])
        code, output = self.cli(['answer', '--workspace', str(self.workspace), '--agent', 'workbuddy',
                                 '--id', self.task.id, '--body', 'missing'])
        self.assertNotEqual(code, 0, output)
        self.assertEqual(self.bus.find_results(self.task.id), [])

    def test_cli_answer_with_ticket_commits_result_then_terminal(self):
        self.claim()
        code, output = self.cli(['answer', '--workspace', str(self.workspace), '--agent', 'workbuddy',
                                 '--id', self.task.id, '--body', 'answer',
                                 '--claim-epoch', '1', '--claim-id', self.ticket.claim_id])
        self.assertEqual(code, 0, output)
        result = self.bus.find_results(self.task.id)[0]
        self.assertEqual(result.source_claim_id, self.ticket.claim_id)
        self.assertTrue((self.bus.dirs('workbuddy')['done'] / (self.task.id + '.json')).is_file())

    def test_mcp_claimed_session_reports_with_retained_ticket(self):
        app = AgentBusMcp(self.workspace)
        app.next_task('workbuddy')
        original = self.bus.read(self.bus.processing('workbuddy')[0])
        app.report('workbuddy', self.task.id, 'answer')
        self.assertEqual(self.bus.find_results(self.task.id)[0].source_claim_id, original.claim_id)

    def test_mcp_old_session_cannot_refresh_ticket_from_disk(self):
        app = AgentBusMcp(self.workspace)
        app.next_task('workbuddy')
        self.bus.recover_claim('workbuddy', self.task.id, owner_confirmed_inactive=True)
        newer = AgentBusMcp(self.workspace)
        newer.next_task('workbuddy')
        before = self.bus.processing('workbuddy')[0].read_bytes()
        with self.assertRaises((BusError, ValueError)):
            app.report('workbuddy', self.task.id, 'late')
        self.assertEqual(self.bus.processing('workbuddy')[0].read_bytes(), before)
        self.assertEqual(self.bus.find_results(self.task.id), [])
        quarantined = F.list_quarantined(self.bus.root, self.task.id)
        self.assertTrue(quarantined)
        self.assertEqual(quarantined[0]['evidence']['body'], 'late')

    def test_mcp_send_result_without_ticket_has_no_write(self):
        self.claim()
        app = AgentBusMcp(self.workspace, identity='workbuddy', session_tools=True)
        with self.assertRaises((BusError, ValueError)):
            app.session_send(to='codex', body='missing', type='result', reply_to=self.task.id)
        self.assertEqual(self.bus.find_results(self.task.id), [])

    def test_result_persisted_before_finish_is_reconciled_without_requeue(self):
        self.claim()
        with patch.object(self.bus, 'finish', side_effect=OSError('crash after receipt')):
            with self.assertRaises(OSError):
                self.bus.commit_result('workbuddy', self.claimed, self.ticket, body='answer', exit_code=0)
        self.assertEqual(len(self.bus.find_results(self.task.id)), 1)
        recovered = Bus(self.bus.root).recover_claim('workbuddy', self.task.id, owner_confirmed_inactive=True)
        self.assertEqual(recovered.status, 'done')
        self.assertEqual(recovered.attempt, 0)
        self.assertEqual(self.bus.pending('workbuddy'), [])

    def test_result_route_override_survives_crash_before_finish(self):
        self.claim()
        with patch.object(self.bus, 'finish', side_effect=OSError('crash after route override')):
            with self.assertRaises(OSError):
                self.bus.commit_result('workbuddy', self.claimed, self.ticket,
                                       body='answer', result_to='trae', exit_code=0)
        recovered = Bus(self.bus.root).recover_claim('workbuddy', self.task.id, owner_confirmed_inactive=True)
        self.assertEqual(recovered.status, 'done')
        self.assertEqual(recovered.result_to, 'trae')
        self.assertEqual(recovered.attempt, 0)

    def test_finish_if_ready_cannot_move_a_revoked_claim(self):
        self.claim()
        recovered = self.bus.recover_claim('workbuddy', self.task.id, owner_confirmed_inactive=True)
        ready = self.bus.pending('workbuddy')[0]
        before = ready.read_bytes()
        self.assertIsNone(self.bus.finish_if_ready('workbuddy', recovered))
        self.assertEqual(ready.read_bytes(), before)

    def test_claim_failure_restores_original_ready_bytes(self):
        path = self.bus.pending('workbuddy')[0]
        original = path.read_bytes()
        with patch.object(self.bus, '_write', side_effect=OSError('claim write interrupted')):
            with self.assertRaises(OSError):
                self.bus.claim('workbuddy', path)
        self.assertEqual(path.read_bytes(), original)

    def test_single_letter_member_uses_formal_id_contract(self):
        task = self.bus.make('codex', 'b', 'task', result_to='codex', body='one-letter member')
        self.bus.post(task)
        original = self.bus.claim('b', self.bus.pending('b')[0])
        ticket = F.captured_ticket(original)
        self.bus.heartbeat('b', original, ticket, note='test executor is alive')
        result = self.bus.commit_result('b', original, ticket, body='answer', exit_code=0)
        self.assertEqual(result.sender, 'b')

    def test_audit_failure_keeps_fully_signed_processing_claim(self):
        audit = self.bus.audit
        def failed_audit(**event):
            if event.get('event') == 'claim':
                raise OSError('claim audit interrupted')
            audit(**event)
        with patch.object(self.bus, 'audit', side_effect=failed_audit):
            with self.assertRaises(OSError):
                self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        self.assertEqual(self.bus.pending('workbuddy'), [])
        current = self.bus.read(self.bus.processing('workbuddy')[0])
        self.assertEqual(current.status, 'claimed')
        self.assertEqual(current.claim_epoch, 1)
        self.assertRegex(current.claim_id, r'^[0-9a-f]{32}$')

    def test_same_claim_double_submit_creates_one_receipt(self):
        self.claim()
        runtime = str(Path(__file__).resolve().parents[1])
        script = r'''
import json, sys, time
from pathlib import Path
from agentbus.bus import Bus
bus = Bus(Path(sys.argv[1]) / '.agentbus')
ticket = json.loads(sys.argv[3])
gate = Path(sys.argv[1]) / 'gate'
while not gate.exists(): time.sleep(.01)
try:
    msg = bus.read(bus.dirs('workbuddy')['processing'] / (sys.argv[2] + '.json'))
    result = bus.commit_result('workbuddy', msg, ticket, body='same', exit_code=0)
    print('ok:' + result.id)
except Exception as exc:
    print('rejected:' + type(exc).__name__)
'''
        env = dict(os.environ, PYTHONPATH=runtime)
        procs = [subprocess.Popen([sys.executable, '-B', '-X', 'utf8', '-c', script,
                                  str(self.workspace), self.task.id, json.dumps(self.ticket.to_dict())],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(4)]
        (self.workspace / 'gate').touch()
        outputs = [proc.communicate(timeout=20) for proc in procs]
        self.assertTrue(all(proc.returncode == 0 for proc in procs), outputs)
        self.assertEqual(len(self.bus.find_results(self.task.id)), 1, outputs)
        self.assertTrue((self.bus.dirs('workbuddy')['done'] / (self.task.id + '.json')).is_file())

    def test_revoked_receipt_cannot_complete_or_open_reviews(self):
        self.claim()
        reply = self.bus.make('workbuddy', 'codex', 'result', reply_to=self.task.id, body='late')
        reply.source_claim_epoch, reply.source_claim_id = 1, self.ticket.claim_id
        recovered = self.bus.recover_claim('workbuddy', self.task.id, owner_confirmed_inactive=True)
        self.assertTrue(jobs.receipt_binding_error(recovered, reply))

    def test_failed_commit_preserves_failure_terminal(self):
        self.claim()
        self.bus.commit_result('workbuddy', self.claimed, self.ticket, body='failed honestly', exit_code=7)
        terminal = self.bus.read(self.bus.dirs('workbuddy')['done'] / (self.task.id + '.json'))
        self.assertEqual(terminal.status, 'failed')
        self.assertEqual(terminal.exit_code, 7)

    def test_mcp_explicit_original_ticket_survives_new_connection(self):
        self.claim()
        app = AgentBusMcp(self.workspace, identity='workbuddy', session_tools=True)
        app.session_send(to='codex', body='answer', type='result', reply_to=self.task.id,
                         ticket=self.ticket.to_dict())
        self.assertEqual(self.bus.find_results(self.task.id)[0].source_claim_id, self.ticket.claim_id)

    def test_cli_direct_post_requires_original_ticket(self):
        self.claim()
        code, output = self.cli(['post', '--workspace', str(self.workspace), '--from', 'workbuddy',
                                 '--to', 'codex', '--type', 'result', '--reply-to', self.task.id,
                                 '--intent', 'answer', '--claim-epoch', '1', '--claim-id', self.ticket.claim_id])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.bus.find_results(self.task.id)[0].source_claim_id, self.ticket.claim_id)

    def test_director_keeps_original_ticket_through_execution(self):
        self.claim()
        director = Director(self.cfg, bus=self.bus)
        with patch.object(director, 'execute', return_value=Result(0, 'execution result', .01)):
            director._handle('workbuddy', self.claimed, Summary(), review=False)
        self.assertEqual(self.bus.find_results(self.task.id)[0].source_claim_id, self.ticket.claim_id)

    def test_director_execution_records_a_real_ticketed_heartbeat(self):
        self.claim()
        class OfflineAdapter:
            def run(inner, prompt, **kwargs):
                snapshot = H.state(self.bus.root, self.task.id, 'workbuddy', task_record=self.claimed.to_dict())
                self.assertEqual(snapshot['source'], 'heartbeat-record')
                return Result(0, 'offline adapter', .01)
        director = Director(self.cfg, bus=self.bus, adapter_factory=lambda *args: OfflineAdapter())
        result = director.execute('workbuddy', self.claimed)
        self.assertEqual(result.output, 'offline adapter')

    def test_director_refuses_late_execution_after_recovery(self):
        self.claim()
        director = Director(self.cfg, bus=self.bus)
        def finish_late(*args):
            self.bus.recover_claim('workbuddy', self.task.id, owner_confirmed_inactive=True)
            self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
            return Result(0, 'late execution result', .01)
        with patch.object(director, 'execute', side_effect=finish_late):
            with self.assertRaises(BusError):
                director._handle('workbuddy', self.claimed, Summary(), review=False)
        self.assertEqual(self.bus.find_results(self.task.id), [])
        self.assertEqual(self.bus.read(self.bus.processing('workbuddy')[0]).claim_epoch, 2)

    def test_fenced_processing_uses_explicit_heartbeat_not_touch(self):
        job = jobs.start_job(self.cfg, 'codex', 'goal', request_id='heartbeat-job')
        job = jobs.dispatch_job(self.cfg, 'codex', job['id'],
                                [{'worker': 'workbuddy', 'title': 'work', 'body': 'work', 'result_to': 'codex'}])
        task_id = job['tasks'][0]['id']
        task_path = self.bus.dirs('workbuddy')['ready'] / (task_id + '.json')
        task = self.bus.claim('workbuddy', task_path)
        source = self.bus.dirs('workbuddy')['processing'] / task_path.name
        os.utime(source, None)
        detail = jobs.status_job(self.cfg, job['id'])['tasks'][0]
        self.assertTrue(detail['heartbeat_stale'])
        self.assertEqual(detail['heartbeat_source'], 'none')
        ticket = F.captured_ticket(task)
        self.bus.heartbeat('workbuddy', task, ticket, note='executor is alive')
        detail = jobs.status_job(self.cfg, job['id'])['tasks'][0]
        self.assertFalse(detail['heartbeat_stale'])
        self.assertEqual(detail['heartbeat_source'], 'heartbeat-record')

    def test_os_lock_outlives_expired_lease(self):
        self.claim()
        from agentbus.lease import LeaseManager
        manager = LeaseManager(self.bus.root)
        lease = manager.acquire('agentbus/report/' + self.task.id, 'expired-holder', ttl_s=1)
        runtime = str(Path(__file__).resolve().parents[1])
        script = r'''
import sys, time
from pathlib import Path
from agentbus.operation_lock import task_lock
root = Path(sys.argv[1])
with task_lock(root / '.agentbus', 'workbuddy', sys.argv[2]):
    (root / 'locked').touch()
    while not (root / 'release').exists(): time.sleep(.01)
'''
        proc = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.workspace), self.task.id],
                                env=dict(os.environ, PYTHONPATH=runtime), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        deadline = time.monotonic() + 5
        while not (self.workspace / 'locked').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.workspace / 'locked').exists())
        time.sleep(1.05)
        manager.sweep()
        self.assertIsNone(manager.holder('agentbus/report/' + self.task.id))
        from agentbus.operation_lock import task_lock
        with self.assertRaises(TimeoutError):
            with task_lock(self.bus.root, 'workbuddy', self.task.id, timeout_s=.05):
                self.fail('expired cooperative lease released a live kernel lock')
        (self.workspace / 'release').touch()
        proc.communicate(timeout=5)
        with task_lock(self.bus.root, 'workbuddy', self.task.id, timeout_s=.5):
            result = self.bus.commit_result('workbuddy', self.claimed, self.ticket, body='answer', exit_code=0)
        self.assertEqual(result.source_claim_id, self.ticket.claim_id)

    def test_heartbeat_nonfinite_and_future_time_are_never_fresh(self):
        self.claim()
        path = H.heartbeat_path(self.bus.root, self.task.id, 'workbuddy')
        payload = self.bus.heartbeat('workbuddy', self.claimed, self.ticket)
        for invalid in (float('nan'), float('inf'), time.time() + 3600):
            with self.subTest(stamp=invalid):
                path.write_text(json.dumps({**payload, 'at_epoch_s': invalid}), encoding='utf-8')
                snapshot = H.state(self.bus.root, self.task.id, 'workbuddy', task_record=self.claimed.to_dict())
                self.assertTrue(snapshot['stale'])
                self.assertNotEqual(snapshot['source'], 'heartbeat-record')

    def test_legacy_record_does_not_upgrade_a_heartbeat_to_verified(self):
        self.claim()
        self.bus.heartbeat('workbuddy', self.claimed, self.ticket)
        snapshot = H.state(self.bus.root, self.task.id, 'workbuddy', task_record=self.task.to_dict())
        self.assertTrue(snapshot['stale'])

    def test_cli_heartbeat_requires_and_uses_original_ticket(self):
        self.claim()
        code, output = self.cli(['heartbeat', '--workspace', str(self.workspace), '--agent', 'workbuddy',
                                 '--id', self.task.id, '--claim-epoch', '1', '--claim-id', self.ticket.claim_id])
        self.assertEqual(code, 0, output)
        snapshot = H.state(self.bus.root, self.task.id, 'workbuddy', task_record=self.claimed.to_dict())
        self.assertEqual(snapshot['source'], 'heartbeat-record')

    def test_cli_evidence_reads_actual_project_and_writes_no_files(self):
        before = {p.relative_to(self.workspace).as_posix(): p.read_bytes()
                  for p in self.workspace.rglob('*') if p.is_file()}
        for command, subject in [('verify', ['--task-id', self.task.id]),
                                 ('timeline', []), ('attest', ['--task-id', self.task.id])]:
            code, output = self.cli([command, '--workspace', str(self.workspace), *subject])
            self.assertEqual(code, 0, output)
            data = json.loads(output)
            self.assertIsInstance(data, dict)
        after = {p.relative_to(self.workspace).as_posix(): p.read_bytes()
                 for p in self.workspace.rglob('*') if p.is_file()}
        self.assertEqual(after, before)

    def test_mcp_evidence_endpoints_read_without_claiming(self):
        app = AgentBusMcp(self.workspace)
        before = self.bus.pending('workbuddy')[0].read_bytes()
        report = json.loads(app.verify(task_id=self.task.id))
        self.assertNotEqual(report['verdict'], 'pass')
        self.assertIsInstance(json.loads(app.timeline()), dict)
        self.assertIsInstance(json.loads(app.attest(self.task.id)), dict)
        self.assertEqual(self.bus.pending('workbuddy')[0].read_bytes(), before)

    def test_claimticket_instance_rejects_boolean_epoch(self):
        self.claim()
        forged = F.ClaimTicket(self.task.id, 'workbuddy', True, self.ticket.claim_id, '')
        with self.assertRaises(BusError):
            self.bus.commit_result('workbuddy', self.claimed, forged, body='boolean ticket')
        self.assertEqual(self.bus.find_results(self.task.id), [])

    def test_boolean_ticket_cannot_finish_without_a_valid_epoch(self):
        self.claim()
        forged = F.ClaimTicket(self.task.id, 'workbuddy', True, self.ticket.claim_id, '')
        with self.assertRaises(BusError):
            self.bus.finish('workbuddy', self.claimed, ticket=forged)
        self.assertTrue(self.bus.processing('workbuddy'))

    def test_commit_and_recovery_processes_have_one_legal_winner(self):
        self.claim()
        runtime = str(Path(__file__).resolve().parents[1])
        script = r'''
import json, sys, time
from pathlib import Path
from agentbus.bus import Bus
root = Path(sys.argv[1])
bus = Bus(root / '.agentbus')
ticket = json.loads(sys.argv[3])
while not (root / 'race-go').exists(): time.sleep(.01)
try:
    if sys.argv[4] == 'commit':
        msg = bus.read(bus.dirs('workbuddy')['processing'] / (sys.argv[2] + '.json'))
        bus.commit_result('workbuddy', msg, ticket, body='answer', exit_code=0)
    else:
        bus.recover_claim('workbuddy', sys.argv[2], owner_confirmed_inactive=True)
    print('accepted:' + sys.argv[4])
except Exception as exc:
    print('rejected:' + type(exc).__name__)
'''
        procs = [subprocess.Popen([sys.executable, '-B', '-X', 'utf8', '-c', script,
                                  str(self.workspace), self.task.id, json.dumps(self.ticket.to_dict()), role],
                                 env=dict(os.environ, PYTHONPATH=runtime), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for role in ('commit', 'recover')]
        (self.workspace / 'race-go').touch()
        outputs = [p.communicate(timeout=20) for p in procs]
        self.assertTrue(all(p.returncode == 0 for p in procs), outputs)
        receipts = self.bus.find_results(self.task.id)
        source = self.bus.find_message(self.task.id)
        self.assertIn(source.status, ('pending', 'done'), outputs)
        if source.status == 'done':
            self.assertEqual(len(receipts), 1, outputs)
            self.assertEqual(source.attempt, 0)
        else:
            self.assertEqual(receipts, [], outputs)
            self.assertEqual(source.attempt, 1)
            self.assertEqual(source.claim_id, '')
        self.assertEqual(self.bus.processing('workbuddy'), [])

    def test_os_lock_released_when_owner_process_is_killed(self):
        self.claim()
        runtime = str(Path(__file__).resolve().parents[1])
        script = r'''
import sys, time
from pathlib import Path
from agentbus.operation_lock import task_lock
root = Path(sys.argv[1])
with task_lock(root / '.agentbus', 'workbuddy', sys.argv[2]):
    (root / 'owned').touch()
    time.sleep(30)
'''
        proc = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.workspace), self.task.id],
                                env=dict(os.environ, PYTHONPATH=runtime), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        until = time.monotonic() + 5
        while not (self.workspace / 'owned').exists() and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue((self.workspace / 'owned').exists())
        proc.kill()
        proc.communicate(timeout=5)
        result = self.bus.commit_result('workbuddy', self.claimed, self.ticket, body='after crash', exit_code=0)
        self.assertEqual(result.source_claim_epoch, 1)


if __name__ == '__main__':
    unittest.main()
