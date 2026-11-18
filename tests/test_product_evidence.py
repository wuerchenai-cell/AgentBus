"""Integration regressions against the real file mailbox contract (Codex, 2026-10-08)."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from agentbus import delivery_verify as dv
from agentbus import timeline
from agentbus import evidence_service

TASK = 'm-1791457584502-eeeeee'
RESULT = 'm-1791457584503-ffffff'
REVIEW = 'm-1791457584504-aaaaaa'
JOB = 'j-b7ed91331128d934d22268834e306bd4'


class ProductEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'control'
        self.bus = self.root / '.agentbus'
        self.bus.mkdir(parents=True)
        self.output = Path(self.temp.name) / 'business'
        self.output.mkdir()
        data = b'actual artifact'
        (self.root / 'report.txt').write_bytes(data)
        (self.output / 'report.txt').write_bytes(data)
        self.art = dict(path='report.txt', bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.task = dict(id=TASK, type='task', sender='codex', to='alpha', result_to='codex',
                         status='done', expected_artifacts=['report.txt'])
        self.result = dict(id=RESULT, type='result', sender='alpha', to='codex', reply_to=TASK,
                           status='done', transport='program', artifacts=[self.art], exit_code=0,
                           ts='2026-10-08T19:10:00+08:00')

    def put(self, state, agent, record):
        path = self.bus / 'agents' / agent / state / (record['id'] + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding='utf-8')

    def closed_review(self, *, reviewer='bravo', receipt=True):
        self.put('done', reviewer, dict(id=REVIEW, type='review', sender='codex', to=reviewer,
                 result_to='codex', reply_to=TASK, status='done', refs=[TASK]))
        if receipt:
            self.put('ready', 'codex', dict(id='m-1791457584505-bbbbbb', type='result',
                     sender=reviewer, to='codex', reply_to=REVIEW, status='pending', exit_code=0))

    def inspect(self):
        return dv.verify_task(self.bus, self.root, TASK, task_record=self.task)

    def test_find_actual_terminal_task_and_ready_receipt(self):
        self.put('done', 'alpha', self.task)
        self.put('ready', 'codex', self.result)
        self.closed_review()
        out = dv.verify_task(self.bus, self.root, TASK)
        self.assertEqual(out['verdict'], 'pass', out)

    def test_review_without_formal_result_is_unverified(self):
        self.put('done', 'codex', self.result)
        self.closed_review(receipt=False)
        self.assertIn('review_closure', self.inspect()['unverified'])

    def test_self_review_does_not_close(self):
        self.put('done', 'codex', self.result)
        self.closed_review(reviewer='alpha')
        self.assertIn('review_closure', self.inspect()['unverified'])

    def test_closed_review_with_ready_result_closes(self):
        self.put('ready', 'codex', self.result)
        self.closed_review()
        self.assertEqual(self.inspect()['verdict'], 'pass')

    def test_info_reply_is_not_a_formal_result(self):
        self.result['type'] = 'info'
        self.put('done', 'codex', self.result)
        self.assertEqual(dv.collect_receipts(self.bus, TASK), [])

    def test_receipt_wrong_sender_fails(self):
        self.result['sender'] = 'impostor'
        self.put('done', 'codex', self.result)
        self.assertEqual(self.inspect()['verdict'], 'fail')

    def test_receipt_binding_to_other_plan_fails(self):
        self.task['plan_id'] = 'r-selected'
        self.result['plan_id'] = 'r-other'
        self.put('done', 'codex', self.result)
        self.assertEqual(self.inspect()['verdict'], 'fail')

    def test_manifest_subset_cannot_satisfy_expected_artifacts(self):
        self.task['expected_artifacts'].append('missing.txt')
        self.put('done', 'codex', self.result)
        self.assertEqual(self.inspect()['verdict'], 'fail')

    def test_failed_receipt_is_not_success_despite_matching_bytes(self):
        self.result['exit_code'] = 1
        self.put('done', 'codex', self.result)
        self.closed_review()
        self.assertEqual(self.inspect()['verdict'], 'fail')

    def test_inspecting_after_deadline_does_not_expire_timely_completion(self):
        from datetime import datetime
        ts = datetime.fromisoformat(self.result['ts']).timestamp()
        self.task['deadline_ts'] = ts + 10
        self.put('done', 'codex', self.result)
        self.closed_review()
        out = dv.verify_task(self.bus, self.root, TASK, task_record=self.task, now=ts+1000)
        self.assertEqual(out['verdict'], 'pass', out)

    def test_task_id_from_different_file_is_rejected(self):
        wrong = dict(self.task, id='m-1791457584508-cccccc')
        self.put('done', 'codex', self.result)
        out = dv.verify_task(self.bus, self.root, TASK, task_record=wrong)
        self.assertEqual(out['verdict'], 'fail')

    def test_job_partial_is_not_all_verified(self):
        path = self.bus / 'jobs' / (JOB+'.json')
        path.parent.mkdir()
        path.write_text(json.dumps(dict(id=JOB, tasks=[dict(id=TASK), dict(id=RESULT)],
              finalization=dict(summary='finished', committed=True))), encoding='utf-8')
        with patch.object(dv, 'verify_task', side_effect=[dict(verdict='pass',evidence='native_receipt'),
                                                        dict(verdict='partial',evidence='none')]):
            out = dv.verify_job(self.bus, self.root, JOB)
        self.assertNotEqual(out['verdict'], 'pass', out)
        check = next(c for c in out['checks'] if c['code'] == 'all_tasks_verified')
        self.assertNotEqual(check['verdict'], 'pass')

    def test_job_id_path_traversal_is_rejected(self):
        self.assertEqual(dv.verify_job(self.bus, self.root, '../outside')['verdict'], 'fail')

    def test_timeline_uses_configured_output_root(self):
        (self.root / 'report.txt').unlink()
        (self.bus / 'config.json').write_text(json.dumps(dict(output_root=str(self.output))),encoding='utf-8')
        self.put('ready', 'codex', self.result)
        out = timeline.build_timeline(self.bus)
        self.assertTrue(out['nodes'][0]['artifact_verified'], out)

    def test_broken_review_reference_in_body_does_not_close(self):
        self.put('done', 'codex', self.result)
        self.put('done', 'bravo', dict(id=REVIEW, type='review', body='old note '+TASK))
        self.assertIn('review_closure', self.inspect()['unverified'])

    def test_service_is_read_only_and_uses_business_root(self):
        self.put('done', 'alpha', self.task)
        self.put('ready', 'codex', self.result)
        self.closed_review()
        (self.root / 'report.txt').unlink()
        cfg = SimpleNamespace(workspace=self.root, output_root=self.output)
        before = {p.relative_to(self.root).as_posix():p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        data = json.loads(evidence_service.execute(cfg, 'verify', TASK))
        self.assertEqual(data['verdict'], 'pass', data)
        self.assertIn('交付时间线', evidence_service.execute(cfg, 'timeline', format='markdown'))
        self.assertNotIn('actual artifact', evidence_service.execute(cfg, 'attest', TASK))
        after = {p.relative_to(self.root).as_posix():p.read_bytes()
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_service_rejects_ambiguous_subject_and_missing_attestation(self):
        cfg = SimpleNamespace(workspace=self.root, output_root=self.output)
        with self.assertRaises(ValueError):
            evidence_service.verify(cfg, task_id=TASK, job_id=JOB)
        with self.assertRaises(ValueError):
            evidence_service.attest_task(cfg, TASK)

    def test_no_artifact_cannot_get_native_badge(self):
        from agentbus.attestation import attest
        result = attest(self.root, author='alpha', task_id=TASK, evidence='native_receipt')
        self.assertEqual(result['badge'], 'D')


if __name__ == '__main__':
    unittest.main()
