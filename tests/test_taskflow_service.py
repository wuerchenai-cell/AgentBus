"""Task flow uses real temporary files, tickets and receipts. Codex 2026-10-09."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.task_assignment import assignment_mode, recommend
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class TaskflowServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = load(self.root, create=True)
        for worker in self.cfg.data['workers'].values():
            worker['enabled'] = worker['executor_enabled'] = False
        self.cfg.data['workers'].update({agent: {'enabled': True, 'executor_enabled': True,
            'can_spawn': False, 'kind': 'gui', 'transport': 'file', 'communication_mode': 'file',
            'host_version': '1', 'model_id': 'model-1'} for agent in ('alpha', 'beta')})
        self.cfg.data['director'] = 'alpha'
        self.cfg = save(self.cfg)
        self.forest = TaskForest(self.cfg)
        self.bus = self.forest.bus
        self.bridge = WebBridge(self.root)
        self.structure = {'schema_version': 1,
            'roots': [{'id': 'project', 'parent_id': None, 'title': 'Project', 'goal': 'Ship a project'}],
            'branches': [{'id': 'feature', 'parent_id': 'project', 'title': 'Feature'}],
            'leaves': [{'id': 'first', 'parent_id': 'feature', 'title': 'Write feature',
                        'goal': 'Build first output', 'handoff': 'Reuse earlier analysis'}]}
        self.forest.replace_structure(self.structure)

    def data(self, action, payload=None):
        result = self.bridge.call(action, payload)
        self.assertTrue(result['ok'], result)
        return result['data']

    def leaf(self, identity='first'):
        return next(leaf for leaf in self.data('snapshot')['leaves'] if leaf['id'] == identity)

    def config(self, **extra):
        return self.data('configure', {'members': ['alpha', 'beta'], 'coordinator': 'alpha', **extra})

    def card(self, agent, source=None, receipt=None, family='file_collaboration', verified=True):
        now = datetime.now(timezone.utc).isoformat()
        return {'schema_version': 1, 'member_id': agent, 'display_name': agent,
                'host_version': '1', 'model_id': 'model-1', 'route': 'file',
                'tools': [], 'specialties': [family], 'limitations': [],
                'source': 'test public evidence', 'updated_at': now,
                'samples': [] if source is None else [{'project_id': str(self.root),
                    'task_id': source.id, 'result_id': receipt.id if receipt else 'm-nonexistent',
                    'capability_family': family, 'outcome': 'done', 'verified': verified,
                    'observed_at': now, 'version': '1', 'model': 'model-1',
                    'route': 'file', 'transport': 'program'}]}

    def store_card(self, card):
        folder = self.root / '.agentbus/member-cards'
        folder.mkdir(exist_ok=True)
        (folder / (card['member_id'] + '.json')).write_text(json.dumps(card), encoding='utf-8')

    def commit_work(self, agent, task, **kwargs):
        ticket = captured_ticket(task)
        self.bus.heartbeat(agent, task, ticket, note='test worker started')
        return self.bus.commit_result(agent, task, ticket, **kwargs)

    def test_mode_defaults_and_only_valid_values_are_accepted(self):
        self.assertEqual(assignment_mode({}), 'automatic')
        self.assertEqual(self.data('snapshot')['project']['assignment_mode'], 'automatic')
        self.config(assignment_mode='manual')
        self.assertEqual(load(self.root).data['assignment_mode'], 'manual')
        before = self.cfg.path.read_bytes()
        for mode in ('auto', None, True, {}, 'sdk'):
            result = self.bridge.call('configure', {'members': ['alpha', 'beta'],
                'coordinator': 'alpha', 'assignment_mode': mode})
            self.assertFalse(result['ok'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_snapshot_recommendation_is_unverified_without_cards(self):
        leaf = self.leaf()
        self.assertEqual(leaf['recommended_agent'], 'alpha')
        self.assertFalse(leaf['recommendation_verified'])
        self.assertIn('能力待确认', leaf['recommendation_reason'])

    def test_recommendation_uses_actual_workload_not_brand(self):
        msg = self.bus.make('alpha', 'alpha', 'task', body='Other task', result_to='alpha')
        self.bus.init()
        self.bus.post(msg)
        self.assertEqual(self.leaf()['recommended_agent'], 'beta')

    def test_strict_supervision_never_recommends_designated_auditor_for_work(self):
        cfg = load(self.root)
        cfg.data['supervision'] = {
            'strict_supervision': True,
            'designated_auditor': 'beta',
            'checks': [{'argv': ['python', '--version'], 'timeout_seconds': 5}],
        }
        cfg = save(cfg)
        records = {
            'alpha': {'ready': [types.SimpleNamespace(id='busy', status='ready', type='task')]},
            'beta': {'ready': []},
        }

        result = recommend(cfg, self.leaf(), records)

        self.assertEqual(result['recommended_agent'], 'alpha')

    def test_recommendation_cannot_trust_a_self_report_without_a_receipt(self):
        source = self.forest.claim_leaf('first', 'beta')
        self.store_card(self.card('beta', source))
        leaf = self.leaf()
        self.assertFalse(leaf['recommendation_verified'])
        self.assertIn('能力待确认', leaf['recommendation_reason'])

    def test_recommendation_verified_requires_current_native_completion(self):
        source = self.forest.claim_leaf('first', 'beta')
        receipt = self.commit_work('beta', source, body='Done', exit_code=0)
        self.store_card(self.card('beta', source, receipt))
        leaf = self.leaf()
        self.assertEqual(leaf['recommended_agent'], 'beta')
        self.assertTrue(leaf['recommendation_verified'])
        cfg = load(self.root)
        cfg.data['workers']['beta']['model_id'] = 'model-2'
        save(cfg)
        self.assertFalse(self.leaf()['recommendation_verified'])

    def test_capability_requirement_excludes_undeclared_members(self):
        cfg = load(self.root)
        cfg.data['workers']['beta']['capabilities'] = ['ui']
        self.cfg = save(cfg)
        self.structure['leaves'][0]['required_capabilities'] = ['ui']
        self.forest = TaskForest(self.cfg)
        self.forest.replace_structure(self.structure)
        self.assertEqual(self.leaf()['recommended_agent'], 'beta')

    def test_unknown_capability_reports_no_candidate_instead_of_guessing(self):
        self.structure['leaves'][0]['required_capabilities'] = ['not-declared']
        self.forest.replace_structure(self.structure)
        leaf = self.leaf()
        self.assertIsNone(leaf['recommended_agent'])
        self.assertFalse(leaf['recommendation_verified'])

    def test_corrupt_card_does_not_make_a_member_online_or_verified(self):
        self.store_card({'member_id': 'alpha', 'verified': True})
        self.assertFalse(self.leaf()['recommendation_verified'])
        self.assertIn('能力待确认', self.leaf()['recommendation_reason'])

    def test_prompt_preview_has_section_and_does_not_publish_or_claim(self):
        section = self.leaf()['section_no']
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        value = self.data('task_prompt', {'leaf_id': section, 'agent': 'beta'})
        self.assertEqual(value['section_no'], section)
        self.assertFalse(value['assigned'])
        self.assertIn('尚未派发', value['prompt'])
        self.assertIn('Build first output', value['prompt'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_dispatch_instruction_uses_real_task_and_original_claim_receipt_steps(self):
        self.config(assignment_mode='manual')
        result = self.data('dispatch_leaf', {'leaf_id': self.leaf()['section_no'], 'agent': 'beta'})
        prompt = result['prompt']
        self.assertIn(result['dispatched_task_id'], prompt)
        self.assertIn('claim_epoch / claim_id', prompt)
        self.assertIn('answer', prompt)
        self.assertIn(str(self.root), prompt)
        self.assertIn('结果收件人', prompt)
        self.assertFalse(list((self.bus.agents_dir / 'beta/processing').glob('*.json')))
        self.assertFalse(self.bridge.call('task_prompt', {'leaf_id': 'first', 'agent': 'alpha'})['ok'])

    def test_no_launcher_prompt_explains_actual_registration_requirement(self):
        result = self.data('dispatch_leaf', {'leaf_id': 'first', 'agent': 'beta'})
        self.assertIn('正规入口尚未登记', result['prompt'])
        self.assertNotIn('agentbus.exe', result['prompt'].lower())

    def test_automatic_batch_dispatches_only_ready_dependency_and_is_idempotent(self):
        self.structure['leaves'].append({'id': 'second', 'parent_id': 'feature', 'title': 'Second',
                                         'goal': 'Use first', 'dependencies': ['first']})
        self.forest.replace_structure(self.structure)
        first = self.data('dispatch_ready')
        self.assertEqual(first['dispatch_summary'], {'assigned': 1, 'waiting': 1, 'blocked': 0})
        task = first['assignments'][0]['task_id']
        second = self.data('dispatch_ready')
        self.assertEqual(second['dispatch_summary']['assigned'], 0)
        self.assertEqual(self.leaf()['task_id'], task)
        self.assertEqual(self.leaf('second')['task_id'], None)
        self.assertEqual(first['model_calls'], 0)
        self.assertFalse(first['automatic_execution'])

    def test_manual_mode_refuses_automatic_dispatch_and_changes_no_task(self):
        self.config(assignment_mode='manual')
        result = self.bridge.call('dispatch_ready')
        self.assertFalse(result['ok'])
        self.assertIsNone(self.leaf()['task_id'])

    def test_successful_receipt_makes_dependency_ready_for_the_next_call(self):
        self.structure['leaves'].append({'id': 'second', 'parent_id': 'feature', 'title': 'Second',
                                        'goal': 'Use actual first output', 'dependencies': ['first']})
        self.forest.replace_structure(self.structure)
        first = self.data('dispatch_ready')['assignments'][0]
        claimed = self.forest.claim_leaf('first', first['agent'])
        self.commit_work(first['agent'], claimed, body='First done', exit_code=0)
        result = self.data('dispatch_ready')
        self.assertEqual(result['dispatch_summary']['assigned'], 1)
        self.assertEqual(result['assignments'][0]['leaf_id'], 'second')
        self.assertNotEqual(result['assignments'][0]['task_id'], claimed.id)

    def test_completed_task_prompt_cannot_suggest_reclaiming(self):
        self.config()
        claimed = self.forest.claim_leaf('first', 'beta')
        self.commit_work('beta', claimed, body='Done', exit_code=0)
        prompt = self.data('task_prompt', {'leaf_id': 'first', 'agent': 'beta'})['prompt']
        self.assertIn('任务已完成', prompt)
        self.assertIn(str(self.root / '.agentbus/agents/beta/done' / (claimed.id + '.json')), prompt)
        self.assertNotIn('【认领】', prompt)

    def test_automatic_batch_has_a_hard_per_call_limit(self):
        self.structure['leaves'] = [{'id': 'leaf-' + str(n), 'parent_id': 'feature',
            'title': 'Leaf ' + str(n), 'goal': 'Bounded work ' + str(n)} for n in range(22)]
        self.forest.replace_structure(self.structure)
        result = self.data('dispatch_ready')
        self.assertEqual(result['dispatch_summary']['assigned'], 20)
        self.assertEqual(result['dispatch_summary']['waiting'], 2)

    def test_stop_blocks_new_publication_and_resume_does_not_execute(self):
        self.data('stop')
        self.assertFalse(self.bridge.call('dispatch_ready')['ok'])
        self.assertFalse(self.bridge.call('dispatch_leaf', {'leaf_id': 'first', 'agent': 'alpha'})['ok'])
        self.data('resume')
        self.assertIsNone(self.leaf()['task_id'])
        self.assertEqual(self.data('dispatch_ready')['dispatch_summary']['assigned'], 1)

    def test_stop_waits_for_publication_lock_then_no_new_dispatch_after_return(self):
        from agentbus.operation_lock import task_lock
        entered = threading.Event()
        release = threading.Event()
        results = []
        def owner():
            with task_lock(self.bus.root, 'task-forest', 'structure'):
                entered.set()
                release.wait(5)
        held = threading.Thread(target=owner)
        held.start()
        self.assertTrue(entered.wait(3))
        stopping = threading.Thread(target=lambda: results.append(WebBridge(self.root).call('stop')))
        stopping.start()
        time.sleep(.06)
        self.assertTrue(stopping.is_alive())
        release.set()
        held.join(3)
        stopping.join(3)
        self.assertTrue(results[0]['ok'], results)
        self.assertFalse(self.bridge.call('dispatch_ready')['ok'])
        self.assertIsNone(self.leaf()['task_id'])

    def test_stop_competes_with_real_dispatch_on_another_bridge_without_late_publication(self):
        publishing, continue_publication = threading.Event(), threading.Event()
        dispatch_result, stop_result = [], []
        actual_allocate = TaskForest._allocate
        def held_publication(forest, tree, leaf, agent, previous=None):
            publishing.set()
            continue_publication.wait(5)
            return actual_allocate(forest, tree, leaf, agent, previous)
        with patch.object(TaskForest, '_allocate', held_publication):
            dispatcher = threading.Thread(target=lambda: dispatch_result.append(WebBridge(self.root).call(
                'dispatch_leaf', {'leaf_id': 'first', 'agent': 'beta'})))
            dispatcher.start()
            self.assertTrue(publishing.wait(3))
            stopper = threading.Thread(target=lambda: stop_result.append(WebBridge(self.root).call('stop')))
            stopper.start()
            time.sleep(.06)
            self.assertTrue(stopper.is_alive(), 'STOP must serialize with in-progress publication')
            continue_publication.set()
            dispatcher.join(5)
            stopper.join(5)
        self.assertFalse(dispatcher.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertTrue(dispatch_result[0]['ok'], dispatch_result)
        self.assertTrue(stop_result[0]['ok'], stop_result)
        task_id = dispatch_result[0]['data']['dispatched_task_id']
        self.assertTrue((self.bus.agents_dir / 'beta/ready' / (task_id + '.json')).is_file())
        events = self.bus.events()
        posted = next(index for index, e in enumerate(events) if e.get('event') == 'task_tree_dispatched')
        stopped = next(index for index, e in enumerate(events) if e.get('event') == 'stop')
        self.assertLess(posted, stopped, 'the real task is published before STOP is acknowledged')
        count = len(events)
        self.assertFalse(WebBridge(self.root).call('dispatch_ready')['ok'])
        self.assertFalse(WebBridge(self.root).call('dispatch_leaf', {'leaf_id': 'first', 'agent': 'beta'})['ok'])
        self.assertEqual(count, len(self.bus.events()), 'no publication occurs after STOP returns')

    def test_ready_task_owner_cannot_be_disabled_or_removed(self):
        self.data('dispatch_leaf', {'leaf_id': 'first', 'agent': 'beta'})
        before = self.cfg.path.read_bytes()
        result = self.bridge.call('configure', {'members': ['alpha'], 'coordinator': 'alpha'})
        self.assertFalse(result['ok'])
        self.assertIn('beta', result['error'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_processing_owner_cannot_be_disabled_or_removed(self):
        self.forest.claim_leaf('first', 'beta')
        before = self.cfg.path.read_bytes()
        result = self.bridge.call('configure', {'members': ['alpha'], 'coordinator': 'alpha'})
        self.assertFalse(result['ok'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_undecomposed_goal_must_not_be_orphaned_by_changing_coordinator(self):
        self.data('start_goal', {'goal': 'A goal still requiring a plan', 'request_id': 'pending-goal'})
        before = self.cfg.path.read_bytes()
        self.assertFalse(self.bridge.call('configure', {'members': ['beta'], 'coordinator': 'beta'})['ok'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_concurrent_configuration_cannot_dispatch_using_stale_membership(self):
        validated, proceed = threading.Event(), threading.Event()
        actual = self.bridge._leaf_context
        calls = []
        def paused_context(payload, allowed):
            value = actual(payload, allowed)
            calls.append(1)
            if len(calls) == 1:
                validated.set()
                proceed.wait(4)
            return value
        result = []
        with patch.object(self.bridge, '_leaf_context', side_effect=paused_context):
            worker = threading.Thread(target=lambda: result.append(self.bridge.call('dispatch_leaf',
                {'leaf_id': 'first', 'agent': 'beta'})))
            worker.start()
            self.assertTrue(validated.wait(3))
            configured = WebBridge(self.root).call('configure', {'members': ['alpha'], 'coordinator': 'alpha'})
            self.assertTrue(configured['ok'], configured)
            proceed.set()
            worker.join(5)
        self.assertFalse(result[0]['ok'])
        self.assertIsNone(self.leaf()['task_id'])

    def test_completed_member_can_be_removed_after_official_receipt(self):
        source = self.forest.claim_leaf('first', 'beta')
        self.commit_work('beta', source, body='Done', exit_code=0)
        self.data('configure', {'members': ['alpha'], 'coordinator': 'alpha'})
        self.assertFalse(load(self.root).worker('beta')['enabled'])

    def test_program_route_can_roundtrip_file_without_enabling_unknown_adapter(self):
        cfg = load(self.root)
        cfg.data['workers']['opencode'].update(enabled=True, executor_enabled=True,
            can_spawn=True, kind='acp', transport='cli', communication_mode='cli')
        save(cfg)
        self.data('configure', {'members': ['alpha', {'id': 'opencode', 'mode': 'file'}], 'coordinator': 'alpha'})
        self.assertEqual(load(self.root).worker('opencode')['transport'], 'file')
        self.data('configure', {'members': ['alpha', {'id': 'opencode', 'mode': 'cli'}], 'coordinator': 'alpha'})
        self.assertEqual(load(self.root).worker('opencode')['transport'], 'cli')
        self.assertFalse(self.bridge.call('configure', {'members': ['alpha', {'id': 'beta', 'mode': 'sdk'}],
            'coordinator': 'alpha'})['ok'])

    def test_template_export_and_import_preview_are_nonexecuting(self):
        target = self.root / 'reusable.tree.json'
        exported = self.data('export_tree', {'path': str(target)})
        self.assertEqual(exported['node_count'], 3)
        before = self.forest.path.read_bytes()
        preview = self.data('import_tree', {'path': str(target)})
        self.assertEqual(preview['path'], str(target))
        self.assertEqual(preview['preview']['leaf_count'], 1)
        self.assertEqual(before, self.forest.path.read_bytes())
        imported = self.data('import_tree', {'path': str(target), 'confirm': True,
                                             'preview_id': preview['preview_id']})
        self.assertTrue(imported['imported'])
        self.assertEqual(len(imported['leaves']), 2)
        self.assertTrue(all(leaf['task_id'] is None for leaf in imported['leaves']))

    def test_file_preview_reloads_modified_document_at_confirm_time(self):
        target = self.root / 'changed.tree.json'
        self.data('export_tree', {'path': str(target)})
        preview = self.data('import_tree', {'path': str(target)})
        target.write_text('{"schema_version":1,"command":"delete"}', encoding='utf-8')
        before = self.forest.path.read_bytes()
        self.assertFalse(self.bridge.call('import_tree', {'path': str(target), 'confirm': True,
                                                          'preview_id': preview['preview_id']})['ok'])
        self.assertEqual(before, self.forest.path.read_bytes())

    def test_private_or_non_template_paths_are_refused(self):
        for target in (self.root / '.agentbus/config.json', self.root / '.workbuddy/private.tree.json',
                       self.root / 'AGENTS.md', self.root / 'other.json'):
            self.assertFalse(self.bridge.call('export_tree', {'path': str(target)})['ok'])
            self.assertFalse(self.bridge.call('import_tree', {'path': str(target)})['ok'])

    def test_template_dialog_cancellation_and_preview_return_selected_path(self):
        target = self.root / 'picked.tree.json'
        self.data('export_tree', {'path': str(target)})
        class Picker:
            selection = None
            def create_file_dialog(self, kind, **kwargs):
                self.kind = kind
                return self.selection
        picker = Picker()
        self.bridge.bind_window(picker)
        webview = types.SimpleNamespace(FileDialog=types.SimpleNamespace(OPEN=1, SAVE=2))
        with patch.dict('sys.modules', {'webview': webview}):
            self.assertTrue(self.data('import_tree')['cancelled'])
            self.assertTrue(self.data('export_tree')['cancelled'])
            picker.selection = [str(target)]
            self.assertEqual(self.data('import_tree')['path'], str(target))
            self.assertEqual(picker.kind, 1)

    def test_duplicate_template_keys_and_untrusted_web_document_are_rejected(self):
        target = self.root / 'duplicate.tree.json'
        target.write_text('{"schema_version":1,"schema_version":2}', encoding='utf-8')
        self.assertFalse(self.bridge.call('import_tree', {'path': str(target)})['ok'])
        self.assertFalse(self.bridge.call('import_tree', {'document': {}})['ok'])

    def test_builtin_templates_are_previewable_and_confirmable_by_id(self):
        templates = self.data('tree_templates')['templates']
        self.assertTrue(templates)
        preview = self.data('import_tree', {'template_id': templates[0]['id']})
        self.assertGreater(preview['preview']['node_count'], 0)
        self.assertFalse(self.bridge.call('import_tree', {'template_id': 'missing'})['ok'])
        applied = self.data('import_tree', {'template_id': templates[0]['id'], 'confirm': True,
                                            'preview_id': preview['preview_id']})
        self.assertTrue(applied['imported'])
        self.assertFalse(any(leaf['task_id'] for leaf in applied['leaves']))

    def test_import_outline_discloses_truncated_dependency_and_artifact_counts(self):
        target = self.root / 'large-outline.tree.json'
        dependencies = [f'dep-{index}' for index in range(14)]
        document = {'schema_version': 1, 'format': 'generic-tree', 'title': '大依赖预览',
            'roots': [{'id': 'project', 'title': '项目目标', 'goal': '核对大任务'}],
            'branches': [{'id': 'work', 'parent_id': 'project', 'title': '工作分支'}],
            'leaves': [
                {'id': identity, 'parent_id': 'work', 'title': f'前序 {index}',
                 'goal': '完成一项前序工作'}
                for index, identity in enumerate(dependencies)
            ] + [{'id': 'main', 'parent_id': 'work', 'title': '汇总任务', 'goal': '依赖全部前序',
                  'dependencies': dependencies,
                  'expected_artifacts': [f'out/{index}.txt' for index in range(10)]}]}
        target.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        preview = self.data('import_tree', {'path': str(target)})
        row = next(item for item in preview['outline']['items'] if item['title'] == '汇总任务')
        self.assertEqual(row['dependency_count'], 14)
        self.assertEqual(row['dependencies_truncated'], 2)
        self.assertEqual(row['artifact_count'], 10)
        self.assertEqual(row['artifacts_truncated'], 2)


    def test_seed_leaf_avoids_cyclic_parent_and_supports_imported_roots(self):
        # Counterexample R2: root id is not 'project', leaf has parent_id='project'
        alt_forest = TaskForest(self.cfg)
        alt_forest.replace_structure({'roots': [{'id': 'projx', 'parent_id': None}], 'branches': [], 'leaves': []})
        res = alt_forest.seed_leaf({'id': 's1', 'parent_id': 'project', 'title': 'S', 'goal': 'g'})
        self.assertIn('s1', [l['id'] for l in res['leaves']])
        # Check no cyclic branch was created
        for b in res['branches']:
            self.assertNotEqual(b['id'], b['parent_id'])

        # Counterexample R1: fresh project with single imported template root
        fresh_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        fresh_cfg = load(fresh_root, create=True)
        fresh_forest = TaskForest(fresh_cfg)
        from agentbus.tree_templates import import_template, builtin_templates
        tmpl = next(t for t in builtin_templates() if t['id'] == 'software-project')
        import_template(fresh_forest, tmpl['document'])
        res2 = fresh_forest.seed_leaf({'id': 's2', 'title': 'S2', 'goal': 'g2'})
        self.assertIn('s2', [l['id'] for l in res2['leaves']])

    def test_claim_leaf_by_section_no_and_nonempty_error_on_missing(self):
        leaf = self.leaf('first')
        section_no = leaf['section_no']
        self.assertTrue(section_no)
        # Claim via section number
        claimed = self.bridge.call('claim_leaf', {'leaf_id': section_no, 'agent': 'alpha'})
        self.assertTrue(claimed['ok'], claimed)
        self.assertEqual(claimed['data']['leaf']['id'], 'first')
        self.assertTrue(claimed['data']['task_id'])
        self.assertNotIn('ticket', claimed['data'])
        self.assertTrue(self.bridge._tickets[('first', 'alpha')])

        # Checkpoint via section number using cached ticket
        cp = self.bridge.call('checkpoint_leaf', {'leaf_id': section_no, 'agent': 'alpha', 'note': 'in-progress'})
        self.assertTrue(cp['ok'], cp)

        # Missing leaf returns non-empty error message and is not StopIteration
        err_res = self.bridge.call('claim_leaf', {'leaf_id': 'non_existent_999', 'agent': 'alpha'})
        self.assertFalse(err_res['ok'])
        self.assertTrue(bool(err_res['error']))
        self.assertNotEqual(err_res.get('code'), 'StopIteration')

    def test_dispatch_leaf_fails_before_dispatching_on_ambiguous_reference(self):
        # Directly construct an ambiguous reference in file to simulate raw/external collision (R5)
        raw_tree = self.forest._structure()
        raw_tree['branches'][0]['section_no'] = '1'
        raw_tree['leaves'].append({'id': '1', 'parent_id': 'feature', 'title': 'Ambiguous leaf',
                                   'goal': 'Test ambiguity', 'handoff': 'None', 'dependencies': []})
        # Write directly to disk bypasses replace_structure's validation
        self.forest.path.write_text(json.dumps(raw_tree, ensure_ascii=False), encoding='utf-8')

        # Attempt to dispatch using reference '1'
        res = self.bridge.call('dispatch_leaf', {'leaf_id': '1', 'agent': 'alpha'})
        self.assertFalse(res['ok'])
        self.assertTrue('ambiguous' in res['error'] or 'conflicts' in res['error'])
        # Verify NO message was dispatched into alpha's ready directory
        ready_files = list(self.bus.dirs('alpha')['ready'].glob('*.json'))
        self.assertEqual(len(ready_files), 0, f"Leaked dispatched task in ready: {ready_files}")

    def test_recommendation_prioritizes_low_load_before_unverified_fit(self):
        # Alpha has load=1 with unverified declared fit; beta has load=0 without declared fit
        leaf = self.leaf('first')
        records = {
            'alpha': {'ready': [types.SimpleNamespace(id='t1', status='ready', type='task')]},
            'beta': {'ready': []}
        }
        cards = {
            'alpha': {'specialties': ['file_collaboration']},
            'beta': {'specialties': []}
        }
        rec = recommend(self.cfg, leaf, records, cards=cards)
        self.assertEqual(rec['recommended_agent'], 'beta')


if __name__ == '__main__':
    unittest.main()

