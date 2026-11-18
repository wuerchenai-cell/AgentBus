"""Candidate plan contracts. Source: Codex internal helper, 2026-10-04."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


from receipt_helpers import receipt_ticket
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agentbus.bus import Bus
from agentbus.config import load


class ProjectPlanTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('agentbus.project_plan'),
                             'persistent project plan API is absent')
        self.api = importlib.import_module('agentbus.project_plan')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load(Path(self.tmp.name), create=True)
        self.bus = Bus(self.cfg.workspace / '.agentbus')
        self.bus.init()
        self.root = self.cfg.workspace / '.agentbus' / 'project-plan'

    def proposal(self, request='one', content='# Goal\n验收：原始计划\n', base=None,
                 reviewers=None, caller='codex'):
        self.latest_proposal = self.api.propose_plan(self.cfg, caller, content, 'Actual user requirement',
                                                   reviewers or ['workbuddy'], base, request)
        return self.latest_proposal

    def receipt(self, member='workbuddy', body='Verdict: accept\n原意见全文。',
                recipient='codex', sender=None, terminal='done', exit_code=0,
                kind='review'):
        task = self.bus.make('codex', member, kind,
                             body='Read-only plan assessment; SHA256: ' + self.latest_proposal['content_sha256'],
                             result_to='codex')
        self.bus.post(task)
        task.exit_code = exit_code
        self.bus.finish(member, task, terminal, ticket=receipt_ticket(task))
        result = self.bus.make(sender or member, recipient, 'result', body=body,
                               reply_to=task.id)
        result.exit_code = exit_code
        self.bus.post(result)
        return task, result

    def reviewed(self, request='one', base=None, content='# Goal\n验收：原始计划\n'):
        proposal = self.proposal(request, content, base)
        task, result = self.receipt()
        review = self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')
        return proposal, task, result, review

    def adopt(self, proposal, base=None, dispositions=None):
        return self.api.adopt_plan(self.cfg, 'codex', proposal['id'], base,
                                   dispositions or {'workbuddy': 'Accepted with original evidence'})

    def test_absent_store_is_read_only_and_context_marks_unverified(self):
        self.assertIsNone(self.api.plan_snapshot(self.cfg))
        self.assertIsNone(self.api.plan_summary(self.cfg)['current'])
        self.assertIn('未', self.api.plan_context(self.cfg))
        self.assertFalse(self.root.exists())

    def test_raw_content_is_preserved_and_hashed_without_whitespace_normalization(self):
        content = '  # 计划\r\n验收：文件完整。\n\n'
        proposal = self.proposal(content=content)
        self.assertEqual(proposal['content_md'], content)
        self.assertEqual(proposal['content_sha256'], hashlib.sha256(content.encode('utf-8')).hexdigest())
        self.assertFalse(proposal['role_checks_are_authentication'])

    def test_request_retry_reuses_original_record_and_rejects_changed_parameters(self):
        first = self.proposal()
        self.assertEqual(first, self.proposal())
        with self.assertRaises(self.api.ProjectPlanError):
            self.proposal(content='different content')
        self.assertEqual(len(list((self.root / 'proposals').glob('*.json'))), 1)

    def test_concurrent_same_request_creates_one_proposal(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            records = list(pool.map(lambda _: self.proposal(), range(3)))
        self.assertEqual(len({record['id'] for record in records}), 1)
        self.assertEqual(len(list((self.root / 'proposals').glob('*.json'))), 1)

    def test_third_same_content_and_base_is_blocked_and_retry_does_not_increment(self):
        self.proposal('one')
        self.proposal('two')
        third = self.proposal('three')
        self.assertEqual(third['state'], 'blocked')
        self.assertEqual(third['duplicate_count'], 3)
        self.assertEqual(third, self.proposal('three'))
        with self.assertRaises(self.api.ProjectPlanError):
            self.adopt(third)

    def test_new_evidence_content_is_not_same_content_duplicate(self):
        for request in ('one', 'two', 'three'):
            self.proposal(request)
        changed = self.proposal('four', content='# Goal\nNew evidence: verified receipt.\n')
        self.assertEqual(changed['state'], 'proposed')

    def test_missing_formal_receipt_is_not_an_opinion(self):
        proposal = self.proposal()
        task = self.bus.make('codex', 'workbuddy', 'review', body='review', result_to='codex')
        self.bus.post(task)
        self.bus.finish('workbuddy', task, 'done', ticket=receipt_ticket(task))
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')

    def test_receipt_for_another_plan_cannot_be_imported(self):
        original = self.proposal()
        self.proposal('other', content='another frozen plan')
        task, _ = self.receipt()
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.record_review(self.cfg, 'codex', original['id'], task.id, 'accept')

    def test_original_negative_opinion_is_preserved_and_can_have_reasoned_disposition(self):
        proposal = self.proposal()
        task, result = self.receipt(body='结论：reject\n必须保留的异议。')
        review = self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'reject')
        self.assertEqual(review['body'], result.body)
        snapshot = self.adopt(proposal, dispositions={'workbuddy': 'Deferred with explicit verification task'})
        self.assertEqual(snapshot['reviews']['workbuddy']['body'], result.body)
        self.assertEqual(snapshot['reviews']['workbuddy']['verdict'], 'reject')

    def test_accept_with_changes_is_distinct_from_accept(self):
        proposal = self.proposal()
        task, _ = self.receipt(body='Verdict: accept with changes\nKeep dissent.')
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')
        review = self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept with changes')
        self.assertEqual(review['verdict'], 'accept with changes')

    def test_verdict_mention_in_negation_is_not_acceptance(self):
        proposal = self.proposal()
        task, _ = self.receipt(body='I do not accept this plan. Expected verdict: accept.')
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')

    def test_conflicting_explicit_verdicts_are_rejected(self):
        proposal = self.proposal()
        task, _ = self.receipt(body='Verdict: accept\nVerdict: reject')
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')

    def test_duplicate_results_wrong_sender_and_wrong_route_are_rejected(self):
        for case in ('duplicate', 'sender', 'route'):
            with self.subTest(case=case):
                proposal = self.proposal(case, content='# ' + case)
                task, result = self.receipt(sender='trae' if case == 'sender' else None,
                                            recipient='trae' if case == 'route' else 'codex')
                if case == 'duplicate':
                    self.bus.post(self.bus.make('workbuddy', 'codex', 'result',
                                                body=result.body, reply_to=task.id))
                with self.assertRaises(self.api.ProjectPlanError):
                    self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')

    def test_failed_or_nonterminal_task_is_not_successful_review(self):
        for state in ('failed', 'blocked', 'ready'):
            with self.subTest(state=state):
                proposal = self.proposal(state, content='# ' + state)
                if state == 'ready':
                    task = self.bus.make('codex', 'workbuddy', 'review', body='review', result_to='codex')
                    self.bus.post(task)
                    self.bus.post(self.bus.make('workbuddy', 'codex', 'result', body='Verdict: accept', reply_to=task.id))
                else:
                    task, _ = self.receipt(terminal=state, exit_code=1)
                with self.assertRaises(self.api.ProjectPlanError):
                    self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')

    def test_unlisted_member_cannot_supply_required_review(self):
        proposal = self.proposal()
        task, _ = self.receipt(member='trae')
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')

    def test_repeated_review_is_idempotent_and_replacement_is_forbidden(self):
        proposal, task, _, first = self.reviewed()
        self.assertEqual(first, self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept'))
        second, _ = self.receipt()
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.record_review(self.cfg, 'codex', proposal['id'], second.id, 'accept')

    def test_every_designated_member_must_have_real_review_and_nonempty_disposition(self):
        proposal = self.proposal(reviewers=['workbuddy', 'trae'])
        task, _ = self.receipt()
        self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')
        with self.assertRaises(self.api.ProjectPlanError):
            self.adopt(proposal, dispositions={'workbuddy': 'accepted', 'trae': 'not received'})
        task, _ = self.receipt(member='trae')
        self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')
        for reasons in ({'workbuddy': 'accepted'}, {'workbuddy': 'accepted', 'trae': ' '},
                        {'workbuddy': 'accepted', 'trae': 'accepted', 'zcode': 'invented'}):
            with self.assertRaises(self.api.ProjectPlanError):
                self.adopt(proposal, dispositions=reasons)
        self.assertIsNone(self.api.plan_snapshot(self.cfg))

    def test_only_current_selected_coordinator_can_adopt(self):
        proposal, _, _, _ = self.reviewed()
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.adopt_plan(self.cfg, 'workbuddy', proposal['id'], None, {'workbuddy': 'accepted'})

    def test_snapshot_history_stays_fixed_while_current_moves_and_context_shows_both(self):
        first, _, _, _ = self.reviewed()
        old = self.adopt(first)
        base = self.api.plan_summary(self.cfg)['current_sha256']
        next_proposal, _, _, _ = self.reviewed('two', base, '# Changed goal\nNew evidence.')
        current = self.adopt(next_proposal, base)
        self.assertNotEqual(old['revision_id'], current['revision_id'])
        self.assertEqual(self.api.plan_snapshot(self.cfg, old['revision_id']), old)
        context = self.api.plan_context(self.cfg, old)
        self.assertIn(old['revision_id'], context)
        self.assertIn(current['revision_id'], context)
        self.assertIn('改变', context)

    def test_cas_prevents_stale_proposal_from_replacing_new_current(self):
        first, _, _, _ = self.reviewed()
        stale, _, _, _ = self.reviewed('stale', content='# Competing plan')
        adopted = self.adopt(first)
        with self.assertRaises(self.api.ProjectPlanError):
            self.adopt(stale)
        self.assertEqual(self.api.plan_snapshot(self.cfg)['revision_id'], adopted['revision_id'])

    def test_adoption_retry_is_idempotent_but_changed_dispositions_are_rejected(self):
        proposal, _, _, _ = self.reviewed()
        first = self.adopt(proposal)
        self.assertEqual(first, self.adopt(proposal))
        with self.assertRaises(self.api.ProjectPlanError):
            self.adopt(proposal, dispositions={'workbuddy': 'different reason'})

    def test_new_duplicate_or_changed_receipt_after_recording_prevents_adoption(self):
        proposal, task, result, _ = self.reviewed()
        self.bus.post(self.bus.make('workbuddy', 'codex', 'result', body=result.body, reply_to=task.id))
        with self.assertRaises(self.api.ProjectPlanError):
            self.adopt(proposal)
        self.assertIsNone(self.api.plan_snapshot(self.cfg))

    def test_stopped_mutations_preserve_prior_records_and_reading_remains_available(self):
        proposal, task, _, _ = self.reviewed()
        (self.cfg.workspace / '.agentbus' / 'STOP').write_text('stop', encoding='utf-8')
        for action in (lambda: self.proposal('two', content='new'),
                       lambda: self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept'),
                       lambda: self.adopt(proposal)):
            with self.assertRaises(self.api.ProjectPlanError):
                action()
        self.assertIsNone(self.api.plan_snapshot(self.cfg))
        self.assertEqual(len(self.api.plan_summary(self.cfg)['proposals']), 1)

    def test_size_is_limited_by_utf8_bytes_and_malformed_members_are_rejected(self):
        for caller, content, reviewers, request in (
            ('stranger', '# goal', ['workbuddy'], 'one'),
            ('codex', '界' * 22000, ['workbuddy'], 'one'),
            ('codex', '# goal', ['workbuddy', 'workbuddy'], 'one'),
            ('codex', '# goal', ['unknown'], 'one'),
            ('codex', '# goal', ['workbuddy'], '../escape')):
            with self.assertRaises(self.api.ProjectPlanError):
                self.api.propose_plan(self.cfg, caller, content, 'reason', reviewers, None, request)

    def test_unknown_and_path_escaping_revision_ids_are_rejected(self):
        for identity in ('../outside', 'r-does-not-exist'):
            with self.assertRaises(self.api.ProjectPlanError):
                self.api.plan_snapshot(self.cfg, identity)

    def test_symlink_or_junction_plan_directory_is_rejected_without_writing_target(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)
            try:
                self.root.symlink_to(target, target_is_directory=True)
            except OSError:
                if os.name != 'nt':
                    raise
                # cmd emits the active Windows code page, not Python's UTF-8 mode.
                # Only the exit code is required; retain raw bytes for diagnostics.
                result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.root), str(target)],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaises(self.api.ProjectPlanError):
                    self.proposal()
                with self.assertRaises(self.api.ProjectPlanError):
                    self.api.plan_summary(self.cfg)
                self.assertEqual(list(target.iterdir()), [])
            finally:
                if self.root.is_symlink():
                    self.root.unlink()
                else:
                    os.rmdir(self.root)

    def test_corrupt_current_pointer_fails_closed_and_preserves_bytes(self):
        self.root.mkdir(parents=True)
        current = self.root / 'current.json'
        current.write_bytes(b'{broken evidence')
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.plan_summary(self.cfg)
        with self.assertRaises(self.api.ProjectPlanError):
            self.proposal()
        self.assertEqual(current.read_bytes(), b'{broken evidence')

    def test_pointer_hash_detects_revision_content_tampering(self):
        proposal, _, _, _ = self.reviewed()
        snapshot = self.adopt(proposal)
        revision = self.root / 'revisions' / (snapshot['revision_id'] + '.json')
        data = json.loads(revision.read_text(encoding='utf-8'))
        data['content_md'] = 'tampered'
        revision.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.plan_snapshot(self.cfg)

    def test_crash_before_pointer_commit_does_not_adopt_and_exact_retry_recovers(self):
        proposal, _, _, _ = self.reviewed()
        replace = os.replace
        def fail_current(source, destination):
            if Path(destination).name == 'current.json':
                raise OSError('simulated commit interruption')
            return replace(source, destination)
        with patch('agentbus.project_plan.os.replace', side_effect=fail_current):
            with self.assertRaises(OSError):
                self.adopt(proposal)
        self.assertIsNone(self.api.plan_snapshot(self.cfg))
        self.assertEqual(self.adopt(proposal)['content_md'], proposal['content_md'])

    def test_ordinary_old_job_bytes_and_deadline_are_untouched(self):
        from agentbus.jobs import start_job
        job = start_job(self.cfg, 'workbuddy', 'existing job', request_id='old')
        path = self.cfg.workspace / '.agentbus' / 'jobs' / (job['id'] + '.json')
        original = path.read_bytes()
        proposal, _, _, _ = self.reviewed()
        self.adopt(proposal)
        self.assertEqual(path.read_bytes(), original)

    def test_overall_verdict_formats_take_priority_over_subitems(self):
        for index, overall in enumerate(('- 总评：accept with changes。方向、结构',
                                         '一、总体判定：accept with changes——按下方',
                                         '**结论：accept with changes**')):
            proposal = self.proposal('format-' + str(index), content='# Format ' + str(index))
            task, _ = self.receipt(body='accept\n分项意见\n' + overall + '\nreject\n旧分项风险')
            result = self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept with changes')
            self.assertEqual(result['verdict'], 'accept with changes')

    def test_summary_does_not_count_missing_or_changed_formal_evidence_as_received(self):
        proposal, _, result, _ = self.reviewed()
        path = self.bus.dirs('codex')['ready'] / (result.id + '.json')
        data = json.loads(path.read_text(encoding='utf-8'))
        data['body'] = 'Verdict: reject\nchanged after recording'
        path.write_text(json.dumps(data), encoding='utf-8')
        summary = self.api.plan_summary(self.cfg)['proposals'][0]
        self.assertEqual(summary['received'], [])
        self.assertIn('workbuddy', summary['invalid'])
        path.unlink()
        self.assertEqual(self.api.plan_summary(self.cfg)['proposals'][0]['received'], [])

    def test_stop_appearing_after_fsync_blocks_history_publication(self):
        fsync = os.fsync
        def stop_after_flush(fd):
            fsync(fd)
            (self.cfg.workspace / '.agentbus' / 'STOP').write_text('stop', encoding='utf-8')
        with patch('agentbus.project_plan.os.fsync', side_effect=stop_after_flush):
            with self.assertRaises(self.api.ProjectPlanError):
                self.proposal()
        self.assertFalse(list((self.root / 'proposals').glob('*.json')))

    def test_subworkspace_real_evidence_stays_in_original_bus_and_supports_frozen_file(self):
        proposal = self.proposal()
        child = load(self.cfg.workspace / 'review-workspace', create=True)
        bus = Bus(child.workspace / '.agentbus')
        bus.init()
        (child.workspace / 'PROJECT_PLAN.md').write_bytes(proposal['content_md'].encode('utf-8'))
        task = bus.make('codex', 'workbuddy', 'task', body='请先读本工作区 PROJECT_PLAN.md', result_to='codex')
        bus.post(task)
        bus.finish('workbuddy', task, 'done', ticket=receipt_ticket(task))
        result = bus.make('workbuddy', 'codex', 'result', body='结论：accept with changes\n明确异议', reply_to=task.id)
        bus.post(result)
        review = self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept with changes',
                                        reviewed_file='PROJECT_PLAN.md', evidence_workspace='review-workspace')
        self.assertEqual(review['evidence_workspace'], 'review-workspace')
        self.assertEqual(review['association'], 'frozen_file_observed')
        self.assertIn('未证明', review['association_limit'])
        self.assertIsNone(self.bus.find_message(result.id))
        self.assertEqual(self.adopt(proposal)['reviews']['workbuddy']['body'], result.body)

    def test_subworkspace_evidence_cannot_escape_or_use_other_coordinator(self):
        proposal = self.proposal()
        for scope in ('../outside', '.workbuddy/private'):
            with self.assertRaises(self.api.ProjectPlanError):
                self.api.record_review(self.cfg, 'codex', proposal['id'], 'm-no-task', 'accept', evidence_workspace=scope)
        child = load(self.cfg.workspace / 'wrong-director', create=True)
        child.data['director'] = 'trae'
        child.path.write_text(json.dumps(child.data), encoding='utf-8')
        with self.assertRaises(self.api.ProjectPlanError):
            self.api.record_review(self.cfg, 'codex', proposal['id'], 'm-no-task', 'accept', evidence_workspace='wrong-director')

    def test_late_formal_opinion_is_preserved_without_claiming_timely_completion(self):
        proposal = self.proposal()
        task = self.bus.make('codex', 'workbuddy', 'review',
                             body='SHA256: ' + proposal['content_sha256'],
                             result_to='codex', deadline_ts=1)
        self.bus.post(task)
        self.bus.finish('workbuddy', task, 'done', ticket=receipt_ticket(task))
        result = self.bus.make('workbuddy', 'codex', 'result', body='Verdict: accept', reply_to=task.id)
        self.bus.post(result)
        review = self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')
        self.assertTrue(review['late'])
        self.assertEqual(review['task_deadline_ts'], 1)

    def test_stop_appearing_during_pointer_flush_preserves_uncommitted_revision(self):
        proposal, _, _, _ = self.reviewed()
        fsync = os.fsync
        def stop_on_second_flush(fd):
            fsync(fd)
            if list((self.root / 'revisions').glob('*.json')):
                (self.cfg.workspace / '.agentbus' / 'STOP').write_text('stop', encoding='utf-8')
        with patch('agentbus.project_plan.os.fsync', side_effect=stop_on_second_flush):
            with self.assertRaises(self.api.ProjectPlanError):
                self.adopt(proposal)
        self.assertFalse((self.root / 'current.json').exists())
        self.assertEqual(len(list((self.root / 'revisions').glob('*.json'))), 1)

    def test_plan_summary_separates_pending_and_adopted_proposals(self):
        summary = self.api.plan_summary(self.cfg)
        self.assertIn('pending_proposals', summary)
        self.assertIn('adopted_proposals', summary)
        for p in summary['pending_proposals']:
            self.assertEqual(p['state'], 'proposed')
        for p in summary['adopted_proposals']:
            self.assertEqual(p['state'], 'adopted')

    def test_formal_bus_evidence_unreadable_includes_relative_path_and_cause(self):
        proposal = self.proposal()
        task = self.bus.make('codex', 'workbuddy', 'review',
                             body='SHA256: ' + proposal['content_sha256'],
                             result_to='codex')
        self.bus.post(task)
        self.bus.finish('workbuddy', task, 'done', ticket=receipt_ticket(task))
        # 故意注入未知字段污染 done 目录下的 task 文件
        task_file = self.bus.dirs('workbuddy')['done'] / f'{task.id}.json'
        dirty_data = json.loads(task_file.read_text(encoding='utf-8'))
        dirty_data['illegal_field_for_test'] = 'bad_value'
        task_file.write_text(json.dumps(dirty_data), encoding='utf-8')

        with self.assertRaises(self.api.ProjectPlanError) as ctx:
            self.api.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')
        err_msg = str(ctx.exception)
        self.assertIn('formal bus evidence is unreadable', err_msg)
        self.assertIn(task.id + '.json', err_msg)
        self.assertIn('illegal_field_for_test', err_msg)


if __name__ == '__main__':
    unittest.main()
