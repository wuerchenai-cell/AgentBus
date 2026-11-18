"""Real temporary-workspace WebView contracts. Source: Codex, 2026-10-09."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.task_tree import TaskForest


class WebBridgeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('agentbus.web_bridge'),
                             'the real WebView business bridge is not implemented')
        from agentbus.web_bridge import WebBridge
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.cfg = load(self.workspace, create=True)
        self.cfg.data['workers'].update({name: {'enabled': True,
            'executor_enabled': True, 'can_spawn': False, 'kind': 'gui',
            'transport': 'file', 'display_name': name.title()} for name in ('alpha', 'beta')})
        self.cfg = save(self.cfg)
        self.bridge = WebBridge(self.workspace)
        self.forest = TaskForest(self.cfg)

    def data(self, action, payload=None):
        result = self.bridge.call(action, payload)
        self.assertTrue(result['ok'], result)
        return result['data']

    def establish_live_execution(self, task_id, leaf_id, agent):
        """Model the real member process after claim: physical lock + ticketed heartbeat."""
        from agentbus.exclusive_lock import acquire_task_lock
        from agentbus.heartbeat import record as write_heartbeat
        bus = Bus(self.workspace / '.agentbus')
        task = bus.find_message(task_id)
        self.assertIsNotNone(task)
        ticket = self.bridge._tickets[(leaf_id, agent)]
        acquire_task_lock(bus.root, task_id, agent, ticket, note='web bridge delivery fixture')
        write_heartbeat(bus.root, task_id, agent, ticket,
                        task_record=task.to_dict(), note='test execution is active')

    def tree(self, dependency=False):
        tree = {'schema_version': 1,
                'roots': [{'id': 'project', 'parent_id': None, 'title': 'Example project', 'goal': 'Build outputs'}],
                'branches': [{'id': 'build', 'parent_id': 'project', 'title': 'Build'}],
                'leaves': [{'id': 'first', 'parent_id': 'build', 'title': 'First', 'goal': 'Build first'}]}
        if dependency:
            tree['leaves'].append({'id': 'second', 'parent_id': 'build', 'title': 'Second',
                                   'goal': 'Build second', 'dependencies': ['first']})
        self.forest.replace_structure(tree)

    def test_snapshot_is_readonly_and_unknown_member_is_not_online(self):
        before = {str(p): p.read_bytes() for p in self.workspace.rglob('*') if p.is_file()}
        view = self.data('snapshot')
        after = {str(p): p.read_bytes() for p in self.workspace.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(view['project']['workspace'], str(self.workspace.resolve()))
        self.assertEqual(view['project']['coordinator'], 'codex')
        self.assertEqual(view['forest']['leaves'], [])
        self.assertEqual(next(m for m in view['members'] if m['id'] == 'alpha')['status'], '待接入')
        self.assertEqual(view['leaves'], view['forest']['leaves'])

    def test_snapshot_uses_offline_member_marks_without_exposing_executable_paths(self):
        view = self.data('snapshot')
        member = next(item for item in view['members'] if item['id'] == 'alpha')
        self.assertTrue(member['identity_icon_data'].startswith('data:image/svg+xml;base64,'))
        self.assertNotIn('desktop_icon_path', member)
        self.assertNotIn('desktop_icon_data', member)
        self.assertNotIn('desktop_icon_status', member)
        from base64 import b64decode
        svg = b64decode(member['identity_icon_data'].split(',', 1)[1]).decode('utf-8')
        self.assertIn('Alpha', svg)
        self.assertNotIn('https://', svg)
        self.assertFalse(self.bridge.call('choose_member_icon', {})['ok'])

    def test_onboarding_capsule_is_member_specific_and_self_contained(self):
        self.tree()
        self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        result = self.data('onboarding_capsule', {'agent': 'alpha'})
        self.assertEqual(result['agent'], 'alpha')
        self.assertEqual(result['model_calls'], 0)
        self.assertIn('alpha', result['prompt'])
        self.assertIn(str(self.workspace), result['prompt'])
        self.assertIn('First', result['prompt'])
        self.assertNotIn('beta', result['prompt'])
        self.assertIn('彼此平级', result['prompt'])

    def test_ensure_quickstart_creates_only_the_project_root_guide(self):
        self.tree()
        result = self.data('ensure_quickstart')
        path = self.workspace / '00-系统自我坦白与快速上手.md'
        self.assertEqual(result['status'], 'created')
        self.assertTrue(path.is_file())
        text = path.read_text(encoding='utf-8')
        self.assertIn('Example project', text)
        self.assertIn('First', text)
        self.assertFalse((self.workspace / '.workbuddy').exists())
        self.assertEqual(self.data('ensure_quickstart')['status'], 'unchanged')

    def test_system_check_is_readonly_and_reports_tree_and_mailbox_state(self):
        self.tree()
        before = {str(p): p.read_bytes() for p in self.workspace.rglob('*') if p.is_file()}
        result = self.data('system_check')
        after = {str(p): p.read_bytes() for p in self.workspace.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['model_calls'], 0)
        self.assertTrue(any(item['id'] == 'workspace' and item['ok'] for item in result['checks']))
        self.assertTrue(any(item['id'] == 'task_tree' and item['ok'] for item in result['checks']))
        self.assertFalse(any('secret' in str(key).casefold() for key in result))

    def test_system_check_reports_webview2_runtime_and_install_guidance(self):
        self.tree()
        missing_runtime = {
            'platform': 'win32', 'webview2_installed': False,
            'webview2_version': None,
            'guidance': '请安装 Microsoft Edge WebView2 Evergreen Runtime，然后重新打开 AgentBus。',
        }
        with patch('agentbus.web_desktop.runtime_diagnostics', return_value=missing_runtime):
            result = self.data('system_check')
        runtime = next(item for item in result['checks'] if item['id'] == 'webview2')
        self.assertFalse(runtime['ok'])
        self.assertIn('Microsoft Edge WebView2 Evergreen Runtime', runtime['detail'])

        installed_runtime = dict(missing_runtime, webview2_installed=True,
                                  webview2_version='154.0.4258.62')
        with patch('agentbus.web_desktop.runtime_diagnostics', return_value=installed_runtime):
            result = self.data('system_check')
        runtime = next(item for item in result['checks'] if item['id'] == 'webview2')
        self.assertTrue(runtime['ok'])
        self.assertIn('154.0.4258.62', runtime['detail'])

    def test_switching_workspace_persists_only_after_a_valid_snapshot(self):
        from agentbus.gui_state import load_last_workspace
        second = self.workspace / 'second-project'
        load(second, create=True)
        state_file = self.workspace / 'user-ui-state.json'
        with patch('agentbus.gui_state.state_path', return_value=state_file):
            from agentbus.web_bridge import WebBridge
            self.bridge = WebBridge(self.workspace, remember_workspace=True)
            changed = self.data('set_workspace', {'workspace': str(second)})
            self.assertEqual(changed['project']['workspace'], str(second.resolve()))
            self.assertEqual(load_last_workspace(), str(second.resolve()))
            rejected = self.bridge.call('set_workspace', {'workspace': str(second / 'missing')})
            self.assertFalse(rejected['ok'])
            self.assertEqual(load_last_workspace(), str(second.resolve()))

    def test_viewport_persistence_is_scoped_to_the_current_workspace(self):
        second = self.workspace / 'another-project'
        second.mkdir()
        viewport = {'version': 1, 'scale': 2.25, 'panX': -120, 'panY': 63,
                    'width': 1280, 'height': 760}
        with tempfile.TemporaryDirectory() as state_directory:
            state_file = Path(state_directory) / 'ui-state.json'
            with patch('agentbus.gui_state.state_path', return_value=state_file):
                self.assertTrue(self.data('save_viewport', {
                    'workspace': str(self.workspace), 'viewport': viewport})['saved'])
                self.assertEqual(self.data('load_viewport', {
                    'workspace': str(self.workspace)})['viewport'],
                    {key: float(value) if key in ('scale', 'panX', 'panY', 'width', 'height')
                     else value for key, value in viewport.items()})
                cross_project = self.bridge.call('load_viewport', {
                    'workspace': str(second)})
                self.assertFalse(cross_project['ok'])
                self.assertEqual(self.bridge._workspace, self.workspace.resolve())

    def test_branch_fold_preferences_survive_snapshots_and_are_workspace_scoped(self):
        second = self.workspace / 'another-project'
        second.mkdir()
        with tempfile.TemporaryDirectory() as state_directory:
            state_file = Path(state_directory) / 'ui-state.json'
            with patch('agentbus.gui_state.state_path', return_value=state_file):
                self.assertEqual(self.data('snapshot')['ui_state']['collapsed_branches'], [])
                self.assertEqual(self.data('save_branch_folds', {
                    'workspace': str(self.workspace),
                    'branch_ids': ['build'],
                })['branch_ids'], ['build'])
                self.assertEqual(self.data('snapshot')['ui_state']['collapsed_branches'], ['build'])
                cross_project = self.bridge.call('save_branch_folds', {
                    'workspace': str(second), 'branch_ids': ['other'],
                })
                self.assertFalse(cross_project['ok'])
                self.assertEqual(self.data('load_branch_folds', {
                    'workspace': str(self.workspace),
                })['branch_ids'], ['build'])

    def test_configure_preserves_processing_permissions_and_reloads_members(self):
        self.tree()
        self.forest.claim_leaf('first', 'alpha')
        path = next((self.workspace / '.agentbus/agents/alpha/processing').glob('*.json'))
        original = path.read_bytes()
        self.cfg.data['workers']['alpha']['sandbox'] = 'read-only'
        save(self.cfg)
        output = self.workspace / 'outputs'
        output.mkdir()
        view = self.data('configure', {'workspace': str(self.workspace), 'output_root': str(output),
            'director': 'beta', 'members': [{'id': 'alpha', 'display_name': 'Alice', 'enabled': True,
            'communication_mode': 'file'}, {'id': 'beta', 'display_name': 'Bob', 'enabled': True,
            'communication_mode': 'file'}]})
        self.assertEqual(view['config']['director'], 'beta')
        self.assertEqual(load(self.workspace).worker('alpha')['sandbox'], 'read-only')
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(view['project']['output_root'], str(output.resolve()))

    def test_snapshot_exposes_executor_disabled_for_locally_claimed_owner(self):
        self.tree()
        self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        cfg = load(self.workspace)
        cfg.data['workers']['alpha']['executor_enabled'] = False
        cfg = save(cfg)
        view = self.data('snapshot')
        owner = next(member for member in view['members'] if member['id'] == 'alpha')
        self.assertTrue(owner['enabled'])
        self.assertFalse(owner['executor_enabled'])
        leaf = next(item for item in view['forest']['leaves'] if item['id'] == 'first')
        self.assertEqual(leaf['owner'], 'alpha')
        recovered = self.data('handoff_leaf', {'leaf_id': 'first', 'agent': 'codex',
            'owner_confirmed_inactive': True, 'reason': 'Original session stopped; retain outputs', 'expected_task_id': leaf['task_id'], 'expected_owner': 'alpha', 'expected_claim_epoch': leaf['claim_epoch']})
        self.assertIsNotNone(recovered)

    def test_dispatch_leaf_routes_formal_task_without_claim_ticket_in_webview(self):
        self.tree()
        result = self.data('dispatch_leaf', {'leaf_id': 'first', 'agent': 'beta'})
        task_id = result['dispatched_task_id']
        leaf = next(item for item in result['forest']['leaves'] if item['id'] == 'first')
        ready = self.workspace / '.agentbus' / 'agents' / 'beta' / 'ready' / (task_id + '.json')
        processing = self.workspace / '.agentbus' / 'agents' / 'beta' / 'processing' / (task_id + '.json')
        self.assertEqual(leaf['task_id'], task_id)
        self.assertEqual(leaf['state'], 'queued')
        self.assertTrue(ready.is_file())
        self.assertFalse(processing.exists())
        self.assertNotIn('ticket', result)

    def test_operator_can_formally_transcribe_completed_member_work(self):
        self.tree()
        dispatched = self.data('dispatch_leaf', {'leaf_id': 'first', 'agent': 'beta'})
        task_id = dispatched['dispatched_task_id']
        rejected = self.bridge.call('submit_leaf_result', {
            'leaf_id': 'first', 'agent': 'beta', 'summary': 'Implementation is complete',
            'artifact_paths': [], 'operator_confirmed': False})
        self.assertFalse(rejected['ok'])
        ready = self.workspace / '.agentbus/agents/beta/ready' / (task_id + '.json')
        self.assertTrue(ready.is_file(), 'missing confirmation must not claim or change the task')

        artifact = self.workspace / 'report.md'
        artifact.write_text('verified output', encoding='utf-8')
        self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'beta'})
        self.establish_live_execution(task_id, 'first', 'beta')
        result = self.data('submit_leaf_result', {
            'leaf_id': 'first', 'agent': 'beta', 'summary': 'Implementation is complete',
            'artifact_paths': ['report.md'], 'operator_confirmed': True})
        self.assertTrue(result['operator_recorded'])
        self.assertEqual(result['result_to'], 'codex')
        self.assertTrue(result['submitted_result_id'])
        self.assertEqual(result['forest']['leaves'][0]['state'], 'delivered')
        receipt = Bus(self.workspace / '.agentbus').find_result(task_id, sender='beta', recipient='codex')
        self.assertIsNotNone(receipt)
        self.assertIn('由项目操作人代录', receipt.body)
        self.assertEqual(receipt.artifacts[0]['path'], 'report.md')
        self.assertNotIn('ticket', result)
        events = Bus(self.workspace / '.agentbus').events()
        self.assertTrue(any(event.get('event') == 'ui_operator_submitted_result'
                            and event.get('result_id') == receipt.id for event in events))

    def test_operator_cannot_submit_for_externally_claimed_task_without_its_ticket(self):
        self.tree()
        dispatched = self.data('dispatch_leaf', {'leaf_id': 'first', 'agent': 'beta'})
        self.forest.claim_leaf('first', 'beta')
        result = self.bridge.call('submit_leaf_result', {
            'leaf_id': 'first', 'agent': 'beta', 'summary': 'Claimed elsewhere',
            'artifact_paths': [], 'operator_confirmed': True})
        self.assertFalse(result['ok'])
        self.assertIn('原认领票据', result['error'])
        processing = self.workspace / '.agentbus/agents/beta/processing' / (dispatched['dispatched_task_id'] + '.json')
        self.assertTrue(processing.is_file())
        self.assertIsNone(Bus(self.workspace / '.agentbus').find_result(dispatched['dispatched_task_id']))

    def test_reveal_artifact_only_opens_a_verified_registered_output(self):
        self.tree()
        tree = json.loads((self.workspace / '.agentbus/task-tree.json').read_text(encoding='utf-8'))
        tree['leaves'][0]['expected_artifacts'] = ['report.md']
        self.forest.replace_structure(tree)
        output = self.workspace / 'outputs with spaces'
        output.mkdir()
        cfg = load(self.workspace)
        cfg.data['output_root'] = str(output)
        save(cfg)
        dispatched = self.data('dispatch_leaf', {'leaf_id': 'first', 'agent': 'beta'})
        (output / 'report.md').write_text('verified output', encoding='utf-8')
        self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'beta'})
        self.establish_live_execution(dispatched['dispatched_task_id'], 'first', 'beta')
        self.data('submit_leaf_result', {'leaf_id': 'first', 'agent': 'beta',
            'summary': 'Done', 'artifact_paths': ['report.md'], 'operator_confirmed': True})
        with patch('agentbus.web_bridge.subprocess.Popen') as popen:
            result = self.data('reveal_artifact', {'leaf_id': 'first', 'path': 'report.md'})
        self.assertTrue(result['revealed'])
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ['explorer.exe', '/select,', str((output / 'report.md').resolve())])
        self.assertFalse(kwargs.get('shell', True))
        for path in ('../secret.txt', str(output / 'report.md'), 'unlisted.txt'):
            rejected = self.bridge.call('reveal_artifact', {'leaf_id': 'first', 'path': path})
            self.assertFalse(rejected['ok'], path)

    def test_project_tree_scan_is_bounded_and_reports_previewable_candidates(self):
        self.tree()
        from agentbus.tree_templates import export_template
        templates = self.workspace / 'templates'
        templates.mkdir()
        (templates / 'starter.tree.json').write_text(
            json.dumps(export_template(self.forest), ensure_ascii=False), encoding='utf-8')
        (templates / 'broken.tree.json').write_text('{bad json', encoding='utf-8')
        result = self.data('scan_project_trees')
        paths = {item['path'] for item in result['candidates']}
        self.assertIn('templates/starter.tree.json', paths)
        self.assertIn('templates/broken.tree.json', paths)
        good = next(item for item in result['candidates'] if item['path'].endswith('starter.tree.json'))
        bad = next(item for item in result['candidates'] if item['path'].endswith('broken.tree.json'))
        self.assertEqual(good['leaf_count'], 1)
        self.assertIsNone(bad['leaf_count'])
        self.assertIn('JSON', bad['error'])

    def test_gemini_style_tree_preview_strips_execution_state_and_imports_structure(self):
        self.tree()
        path = self.workspace / 'gemini-export.json'
        document = {'schema_version': 1, 'project_name': 'Gemini 协作树',
            'generated_at': '2026-10-09T12:00:00Z',
            'roots': [{'id': 'root', 'parent_id': None, 'title': '发布工具',
                       'goal': '交付可以复用的协作工具', 'state': 'done', 'owner': 'old-agent',
                       'task_id': 'm-old-task', 'results': 'old result', 'progress': 100,
                       'claimable': False, 'eligible_agents': ['old-agent'],
                       'history_task_ids': ['m-old-history'], 'section_no': '0.0'}],
            'branches': [{'id': 'branch', 'parent_id': 'root', 'title': '文件协作',
                          'state': 'running', 'job_id': 'j-old-job',
                          'history_task_ids': ['m-old-branch'], 'section_no': '01'}],
            'leaves': [{'id': 'leaf', 'parent_id': 'branch', 'title': '兼容导入',
                        'goal': '导入可复用结构', 'dependencies': [],
                        'required_capabilities': ['file-read'],
                        'expected_artifacts': ['docs/import.md'], 'state': 'done',
                        'task_id': 'm-old-leaf', 'artifacts': [{'path': 'old.txt'}],
                        'progress_percent': 100, 'recommended_agent': 'old-agent',
                        'recommendation_reason': 'old recommendation',
                        'unmet_dependencies': [], 'evidence_error': 'old evidence',
                        'section_no': '01.1'}]}
        path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        before_tree = (self.workspace / '.agentbus/task-tree.json').read_bytes()
        preview = self.data('import_tree', {'path': str(path), 'confirm': False})
        self.assertTrue(preview.get('preview_id'))
        self.assertIn('旧执行状态不会导入', ' '.join(preview['preview']['warnings']))
        self.assertIn('重新编号', ' '.join(preview['preview']['warnings']))
        self.assertEqual(preview['preview']['title'], 'Gemini 协作树')
        outline_titles = [item['title'] for item in preview['outline']['items']]
        self.assertIn('发布工具', outline_titles)
        self.assertIn('兼容导入', outline_titles)
        self.assertEqual((self.workspace / '.agentbus/task-tree.json').read_bytes(), before_tree)
        imported = self.data('import_tree', {'path': str(path), 'confirm': True,
                                             'preview_id': preview['preview_id']})
        self.assertTrue(imported['imported'])
        roots = imported['forest']['roots']
        self.assertTrue(any(root['title'] == '发布工具' for root in roots))
        leaf = next(item for item in imported['forest']['leaves'] if item['title'] == '兼容导入')
        self.assertIsNone(leaf.get('task_id'))
        self.assertEqual(leaf['expected_artifacts'], ['docs/import.md'])
        self.assertEqual(leaf['dependencies'], [])

    def test_tree_import_confirmation_rejects_source_changed_after_preview(self):
        self.tree()
        path = self.workspace / 'reviewed-tree.json'
        original = {'schema_version': 1, 'title': 'Reviewed tree',
            'roots': [{'id': 'root', 'parent_id': None, 'title': 'Approved title', 'goal': 'Approved goal'}],
            'branches': [], 'leaves': []}
        path.write_text(json.dumps(original), encoding='utf-8')
        before = (self.workspace / '.agentbus/task-tree.json').read_bytes()
        preview = self.data('import_tree', {'path': str(path), 'confirm': False})
        self.assertEqual(preview['preview']['title'], 'Reviewed tree')
        self.assertTrue(preview.get('preview_id'))

        changed = {**original, 'title': 'Unreviewed replacement'}
        path.write_text(json.dumps(changed), encoding='utf-8')
        result = self.bridge.call('import_tree', {'path': str(path), 'confirm': True,
            'preview_id': preview['preview_id']})

        self.assertFalse(result['ok'])
        self.assertIn('预览后', result['error'])
        self.assertEqual((self.workspace / '.agentbus/task-tree.json').read_bytes(), before)

    def test_tree_import_confirmation_requires_an_unused_preview_receipt(self):
        self.tree()
        path = self.workspace / 'reviewed-tree.json'
        path.write_text(json.dumps({'schema_version': 1, 'title': 'Reviewed tree',
            'roots': [{'id': 'root', 'parent_id': None, 'title': 'Approved title', 'goal': 'Approved goal'}],
            'branches': [], 'leaves': []}), encoding='utf-8')
        missing = self.bridge.call('import_tree', {'path': str(path), 'confirm': True})
        self.assertFalse(missing['ok'])
        preview = self.data('import_tree', {'path': str(path), 'confirm': False})
        imported = self.data('import_tree', {'path': str(path), 'confirm': True,
                                              'preview_id': preview['preview_id']})
        self.assertTrue(imported['imported'])
        reused = self.bridge.call('import_tree', {'path': str(path), 'confirm': True,
            'preview_id': preview['preview_id']})
        self.assertFalse(reused['ok'])

    def test_dynamic_new_member_uses_file_route_without_spawn_permission(self):
        view = self.data('configure', {'coordinator': 'new-peer', 'members': [
            {'id': 'new-peer', 'display_name': 'New Peer', 'enabled': True,
             'communication_mode': 'file'}, {'id': 'alpha', 'enabled': True}]})
        worker = load(self.workspace).worker('new-peer')
        self.assertFalse(worker['can_spawn'])
        self.assertEqual(worker['transport'], 'file')
        self.assertEqual(next(m for m in view['members'] if m['id'] == 'new-peer')['label'], 'New Peer')

    def test_invalid_selection_is_rejected_before_config_mutation(self):
        before = self.cfg.path.read_bytes()
        result = self.bridge.call('configure', {'coordinator': 'beta', 'members': ['alpha']})
        self.assertFalse(result['ok'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_configure_rejects_command_and_credential_fields(self):
        before = self.cfg.path.read_bytes()
        for value in ({'members': ['alpha'], 'director': 'alpha', 'command': 'whoami'},
                      {'members': [{'id': 'new-peer', 'command': ['whoami']}], 'director': 'new-peer'},
                      {'members': ['alpha'], 'director': 'alpha', 'api_key': 'not-a-key'}):
            self.assertFalse(self.bridge.call('configure', value)['ok'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_configure_persists_strict_supervision_without_exposing_raw_commands(self):
        checks = [{'name': 'syntax', 'argv': ['python', '-m', 'compileall', '-q', 'agentbus'],
                   'timeout_seconds': 120}]
        view = self.data('configure', {'members': ['alpha', 'beta'], 'director': 'alpha',
            'supervision': {'strict_supervision': True, 'designated_auditor': 'beta',
                            'checks': checks}})
        saved = load(self.workspace).data['supervision']
        self.assertEqual(saved['designated_auditor'], 'beta')
        self.assertTrue(saved['strict_supervision'])
        self.assertEqual(saved['checks'], checks)
        self.assertEqual(view['config']['supervision']['designated_auditor'], 'beta')
        self.assertEqual(view['config']['supervision']['checks'], checks)

    def test_incomplete_strict_supervision_is_rejected_without_mutation(self):
        before = self.cfg.path.read_bytes()
        result = self.bridge.call('configure', {'members': ['alpha', 'beta'],
            'director': 'alpha', 'supervision': {'strict_supervision': True,
                                                   'designated_auditor': 'beta', 'checks': []}})
        self.assertFalse(result['ok'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_supervision_policy_cannot_change_while_work_is_outstanding(self):
        self.data('start_goal', {'caller': 'alpha', 'goal': 'Finish the active deliverable',
                                 'request_id': 'supervision-change-guard'})
        before = self.cfg.path.read_bytes()
        workers = [{'id': name, 'display_name': name.title(), 'enabled': True,
                    'communication_mode': 'file'}
                   for name, worker in self.cfg.data['workers'].items()
                   if worker.get('enabled', True)]
        result = self.bridge.call('configure', {
            'members': workers, 'coordinator': self.cfg.director,
            'supervision': {'strict_supervision': True, 'designated_auditor': 'beta',
                            'checks': [{'name': 'syntax', 'argv': ['python', '-m', 'compileall'],
                                        'timeout_seconds': 30}]}})
        self.assertFalse(result['ok'])
        self.assertIn('待办', result['error'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_supervision_policy_can_change_when_mailboxes_have_no_open_work(self):
        workers = [{'id': name, 'display_name': name.title(), 'enabled': True,
                    'communication_mode': 'file'}
                   for name, worker in self.cfg.data['workers'].items()
                   if worker.get('enabled', True)]
        result = self.data('configure', {'members': workers, 'coordinator': self.cfg.director,
            'supervision': {'strict_supervision': True, 'designated_auditor': 'beta',
                            'checks': [{'name': 'syntax', 'argv': ['python', '-m', 'compileall'],
                                        'timeout_seconds': 30}]}})
        self.assertTrue(result['config']['supervision']['strict_supervision'])
        self.assertEqual(result['config']['supervision']['designated_auditor'], 'beta')

    def test_start_goal_is_formal_idempotent_and_pending_decomposition(self):
        args = {'caller': 'alpha', 'goal': 'Build the requested report', 'request_id': 'browser-request-1'}
        first = self.data('start_goal', args)
        second = self.data('start_goal', args)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['status'], '待分解')
        bus = Bus(self.workspace / '.agentbus')
        goals = [bus.read(p) for p in (bus.agents_dir / 'codex/ready').glob('*.json')]
        self.assertEqual(len([m for m in goals if m.type == 'goal']), 1)
        self.assertEqual(goals[0].body, 'Build the requested report')
        self.assertEqual(goals[0].result_to, 'alpha')
        self.assertEqual(first['model_calls'], 0)

    def test_pending_goal_is_visible_without_becoming_a_claimable_leaf(self):
        self.tree()
        job = self.data('start_goal', {'caller': 'alpha', 'goal': 'New project objective', 'request_id': 'pending-goal'})
        view = self.data('snapshot')
        self.assertIn('pending_goals', view, 'formal undecomposed goals need a visible projection')
        pending = next(g for g in view['pending_goals'] if g['job_id'] == job['id'])
        self.assertEqual(pending['title'], 'New project objective')
        self.assertEqual(pending['recipient'], 'codex')
        self.assertEqual(pending['status'], '待分解')
        self.assertNotIn('claimable', pending)
        self.assertFalse(self.bridge.call('claim_leaf', {'leaf_id': pending['id'], 'agent': 'codex'})['ok'])

    def test_invalid_caller_and_unknown_actions_create_no_goal(self):
        before = self.cfg.path.read_bytes()
        self.assertFalse(self.bridge.call('start_goal', {'caller': '../escape', 'goal': 'bad'})['ok'])
        self.assertFalse(self.bridge.call('run_shell', {'command': 'whoami'})['ok'])
        self.assertFalse(self.bridge.call('call', {'action': 'snapshot'})['ok'])
        self.assertEqual(before, self.cfg.path.read_bytes())
        self.assertFalse((self.workspace / '.agentbus/jobs').exists())

    def test_claim_then_checkpoint_uses_original_local_ticket(self):
        self.tree()
        self.assertFalse(self.data('snapshot')['forest']['leaves'][0].get('operator_can_record', False))
        claim = self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        self.assertEqual(claim['leaf']['state'], 'claimed')
        self.assertNotIn('ticket', claim)
        view = self.data('snapshot')
        self.assertTrue(view['forest']['leaves'][0]['operator_can_record'])
        self.assertNotIn('ticket', json.dumps(view))
        self.data('checkpoint_leaf', {'leaf_id': 'first', 'agent': 'alpha', 'note': 'Continue here'})
        self.assertEqual(self.forest.snapshot()['leaves'][0]['handoff'], 'Continue here')
        view = self.data('snapshot')
        self.assertEqual(next(m for m in view['members'] if m['id'] == 'alpha')['status'], '已认领')
        self.assertNotIn('claim_id', json.dumps(view))

    def test_checkpoint_ui_payload_persists_note_and_progress(self):
        self.tree()
        self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        result = self.data('checkpoint_leaf', {
            'leaf_id': 'first', 'agent': 'alpha',
            'note': 'Implemented parser; next add edge cases', 'progress': 45})
        self.assertEqual(result['handoff'], 'Implemented parser; next add edge cases')
        self.assertEqual(result['progress_percent'], 45)
        reopened = self.bridge.__class__(self.workspace)
        snapshot = reopened.call('snapshot', {})
        self.assertTrue(snapshot['ok'], snapshot)
        leaf = snapshot['data']['forest']['leaves'][0]
        self.assertEqual(leaf['handoff'], 'Implemented parser; next add edge cases')
        self.assertEqual(leaf['progress_percent'], 45)

    def test_checkpoint_rejects_invalid_progress_without_mutating_tree(self):
        self.tree()
        self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        before = self.forest.path.read_bytes()
        response = self.bridge.call('checkpoint_leaf', {
            'leaf_id': 'first', 'agent': 'alpha', 'note': 'bad progress', 'progress': -1})
        self.assertFalse(response['ok'])
        self.assertEqual(before, self.forest.path.read_bytes())

    def test_processing_mtime_never_becomes_running_evidence(self):
        self.tree()
        self.forest.claim_leaf('first', 'alpha')
        path = next((self.workspace / '.agentbus/agents/alpha/processing').glob('*.json'))
        os.utime(path, None)
        view = self.data('snapshot')
        self.assertEqual(view['leaves'][0]['state'], 'claimed')
        self.assertEqual(next(m for m in view['members'] if m['id'] == 'alpha')['status'], '已认领')

    def test_external_claim_does_not_give_bridge_authority_to_checkpoint(self):
        self.tree()
        self.forest.claim_leaf('first', 'alpha')
        before = self.forest.path.read_bytes()
        self.assertFalse(self.bridge.call('checkpoint_leaf', {'leaf_id': 'first', 'agent': 'alpha',
                                                            'note': 'Illegal newest ticket lookup'})['ok'])
        reopened = self.bridge.__class__(self.workspace)
        self.assertFalse(reopened.call('checkpoint_leaf', {'leaf_id': 'first', 'agent': 'alpha',
                                                           'note': 'Must not recover from disk'})['ok'])
        self.assertEqual(before, self.forest.path.read_bytes())

    def test_wrong_ticket_is_rejected_and_does_not_replace_local_ticket(self):
        self.tree()
        claim = self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        bad = {'claim_epoch': 1, 'claim_id': '0' * 32, 'task_id': claim['task_id'], 'agent': 'alpha'}
        self.assertFalse(self.bridge.call('checkpoint_leaf', {'leaf_id': 'first', 'agent': 'alpha',
                                                             'ticket': bad, 'note': 'bad ticket'})['ok'])
        self.data('checkpoint_leaf', {'leaf_id': 'first', 'agent': 'alpha', 'note': 'Original still works'})
        self.assertEqual(self.forest.snapshot()['leaves'][0]['handoff'], 'Original still works')

    def test_handoff_preserves_history_and_releases_only_own_claim(self):
        self.tree()
        claim = self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        self.data('handoff_leaf', {'leaf_id': 'first', 'agent': 'alpha', 'reason': 'Continue with beta',
                                  'successor': 'beta'})
        view = self.forest.snapshot()['leaves'][0]
        self.assertEqual(view['owner'], 'beta')
        self.assertIn(claim['task_id'], view['history_task_ids'])
        self.assertFalse(self.bridge.call('checkpoint_leaf', {'leaf_id': 'first', 'agent': 'alpha',
                                                             'note': 'Old owner'})['ok'])

    def test_dependency_blocks_claim_without_allocating_a_message(self):
        self.tree(dependency=True)
        self.assertFalse(self.bridge.call('claim_leaf', {'leaf_id': 'second', 'agent': 'beta'})['ok'])
        self.assertFalse((self.workspace / '.agentbus/jobs').exists())

    def test_stop_and_resume_use_marker_audit_without_changing_processing(self):
        self.tree()
        self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        path = next((self.workspace / '.agentbus/agents/alpha/processing').glob('*.json'))
        before = path.read_bytes()
        self.assertTrue(self.data('stop', {'caller': 'alpha'})['project']['stopped'])
        self.assertFalse(self.bridge.call('start_goal', {'caller': 'alpha', 'goal': 'Blocked goal'})['ok'])
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(self.data('resume', {'caller': 'alpha'})['project']['stopped'])
        events = Bus(self.workspace / '.agentbus').events()
        self.assertTrue(any(e.get('event') == 'stop' for e in events))
        self.assertTrue(any(e.get('event') == 'resume' for e in events))
        self.assertEqual(before, path.read_bytes())

    def test_private_traversal_and_file_targets_are_rejected(self):
        before = self.cfg.path.read_bytes()
        for path in (str(self.workspace / '.workbuddy'), str(self.workspace / '.ssh'),
                     str(self.workspace / '不要访问'), str(self.workspace / 'child/../outputs'),
                     str(self.cfg.path), 'file:///secret'):
            self.assertFalse(self.bridge.call('set_workspace', {'workspace': path})['ok'])
            self.assertFalse(self.bridge.call('configure', {'members': ['alpha'], 'director': 'alpha',
                                                           'output_root': path})['ok'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_switch_workspace_clears_previous_claim_authority(self):
        self.tree()
        self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        other = self.workspace / 'other'
        other.mkdir()
        save(load(other, create=True))
        self.data('set_workspace', {'workspace': str(other)})
        self.data('set_workspace', {'workspace': str(self.workspace)})
        self.assertFalse(self.bridge.call('checkpoint_leaf', {'leaf_id': 'first', 'agent': 'alpha',
                                                             'note': 'Session was cleared'})['ok'])

    def test_reselecting_same_workspace_preserves_current_claim_authority(self):
        self.tree()
        self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        self.data('set_workspace', {'workspace': str(self.workspace)})
        self.data('checkpoint_leaf', {'leaf_id': 'first', 'agent': 'alpha', 'note': 'Still same project'})
        self.assertEqual(self.forest.snapshot()['leaves'][0]['handoff'], 'Still same project')

    def test_members_only_configuration_preserves_existing_label_and_verified_route(self):
        self.cfg.data['workers']['opencode'].update(enabled=True, executor_enabled=True,
            transport='cli', communication_mode='cli', display_name='OpenCode verified')
        save(self.cfg)
        view = self.data('configure', {'members': ['opencode', 'alpha'], 'director': 'alpha'})
        member = next(m for m in view['members'] if m['id'] == 'opencode')
        self.assertEqual(member['mode'], 'cli')
        self.assertEqual(member['label'], 'OpenCode verified')

    def test_activity_is_bounded_and_excludes_ticket_and_raw_body(self):
        bus = Bus(self.workspace / '.agentbus')
        bus.init()
        for i in range(8):
            bus.audit(event='observed', agent='alpha', task_id='m-test', body='private source body',
                      claim_id='0' * 32, index=i)
        events = self.data('activity', {'limit': 3})['events']
        self.assertEqual(len(events), 3)
        self.assertNotIn('private source body', json.dumps(events))
        self.assertNotIn('claim_id', json.dumps(events))

    def test_recovery_is_observation_and_does_not_reclaim(self):
        self.tree()
        self.forest.claim_leaf('first', 'alpha')
        path = next((self.workspace / '.agentbus/agents/alpha/processing').glob('*.json'))
        before = path.read_bytes()
        result = self.data('recovery')
        self.assertFalse(result['auto_execute'])
        self.assertIn('handoff_index', result)
        entry = next(item for item in result['handoff_index']['entries']
                     if item['task_id'] == path.stem and item['relation'] == 'current')
        self.assertTrue(entry['conversation_id'].startswith('cv-'))
        self.assertIn(entry['state'], ('claimed', 'running'))
        self.assertEqual(before, path.read_bytes())

    def test_output_root_cannot_rebind_inflight_artifact_requirements(self):
        self.tree()
        tree = json.loads(self.forest.path.read_text(encoding='utf-8'))
        tree['leaves'][0]['expected_artifacts'] = ['report.txt']
        self.forest.replace_structure(tree)
        self.forest.claim_leaf('first', 'alpha')
        before = self.cfg.path.read_bytes()
        output = self.workspace / 'other-output'
        output.mkdir()
        result = self.bridge.call('configure', {'members': ['alpha', 'beta'], 'director': 'beta',
                                                'output_root': str(output)})
        self.assertFalse(result['ok'])
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_new_workspace_configuration_has_formal_workflow_and_copyable_prompt(self):
        empty = self.workspace / 'new-project'
        empty.mkdir()
        view = self.data('configure', {'workspace': str(empty), 'members': ['alpha'], 'director': 'alpha'})
        self.assertTrue(view['saved'])
        self.assertTrue(view['workflow_ready'])
        prompt = self.data('workflow_prompt', {'agent': 'alpha'})
        self.assertFalse(prompt['executed'])
        self.assertIn(str(empty), prompt['prompt'])
        self.assertTrue(Path(prompt['path']).is_file())
        self.assertTrue(Path(prompt['skill_path']).is_file())

    def test_skills_are_project_local_readonly_and_never_execute_entrypoint(self):
        skill = self.workspace / 'shared-skills/example'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: example\ndescription: example\n---\nProject skill', encoding='utf-8')
        script = skill / 'must-not-run.py'
        script.write_text("raise RuntimeError('must not execute')", encoding='utf-8')
        before = {str(p): p.read_bytes() for p in skill.iterdir()}
        listing = self.data('skills_list')
        self.assertTrue(listing['read_only'])
        inspected = self.data('skills_inspect', {'path': 'shared-skills/example'})
        self.assertTrue(inspected['read_only'])
        self.assertFalse(inspected['execution_verified'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in skill.iterdir()})
        self.assertFalse(self.bridge.call('skills_inspect', {'path': '../outside'})['ok'])
        self.assertFalse(self.bridge.call('skills_inspect', {'path': '.workbuddy/private'})['ok'])

    def test_native_folder_picker_cancellation_and_selection_are_explicit(self):
        import types
        other = self.workspace / 'picked-project'
        other.mkdir()
        class NativePicker:
            selection = None
            def create_file_dialog(self, dialog, **kwargs):
                if dialog != 17 or kwargs.get('allow_multiple') is not False:
                    raise ValueError('wrong native picker request')
                return self.selection
        picker = NativePicker()
        self.bridge.bind_window(picker)
        with patch.dict('sys.modules', {'webview': types.SimpleNamespace(FileDialog=types.SimpleNamespace(FOLDER=17))}):
            self.assertTrue(self.data('choose_workspace')['cancelled'])
            picker.selection = [str(other)]
            self.assertEqual(self.data('choose_output')['path'], str(other))
            self.assertEqual(self.data('choose_workspace')['project']['workspace'], str(other))
        self.assertFalse(self.bridge.call('snapshot', {'shell': 'whoami'})['ok'])

    def test_member_icon_path_rejects_symlink_before_resolving_target(self):
        from agentbus.web_bridge import WebBridge
        link = self.workspace / 'desktop-agent.exe'
        target = self.workspace / 'real-agent.exe'
        target.write_bytes(b'public icon target')
        with patch.object(Path, 'is_symlink', autospec=True,
                          side_effect=lambda candidate: candidate == link):
            with patch.object(Path, 'resolve', autospec=True, return_value=target):
                with self.assertRaisesRegex(ValueError, '不可访问'):
                    WebBridge._member_icon_path(str(link))

    def test_member_icon_path_rejects_linked_parent_and_shortcuts(self):
        from agentbus.web_bridge import WebBridge
        linked_parent = self.workspace / 'linked'
        candidate = linked_parent / 'desktop-agent.exe'
        with patch.object(Path, 'is_symlink', autospec=True,
                          side_effect=lambda value: value == linked_parent):
            with self.assertRaisesRegex(ValueError, '不可访问'):
                WebBridge._member_icon_path(str(candidate))
        shortcut = self.workspace / 'desktop-agent.lnk'
        with self.assertRaisesRegex(ValueError, '本机 .exe'):
            WebBridge._member_icon_path(str(shortcut))

    def test_member_icon_placeholder_is_not_cached_after_shell_failure(self):
        from agentbus.desktop_app_icon import DesktopIconResult
        executable = self.workspace / 'desktop-agent.exe'
        executable.write_bytes(b'icon source')
        with patch('agentbus.desktop_app_icon.get_desktop_app_icon', side_effect=[
                DesktopIconResult('placeholder', 'data:image/svg+xml;base64,AQ==', 'unavailable'),
                DesktopIconResult('ok', 'data:image/png;base64,AQID')]) as extract:
            first = self.bridge._member_icon(str(executable))
            second = self.bridge._member_icon(str(executable))
        self.assertEqual(first['status'], 'placeholder')
        self.assertEqual(second['status'], 'ok')
        self.assertEqual(extract.call_count, 2)

    def test_configure_returns_offline_identity_and_has_no_executable_icon_picker(self):
        view = self.data('configure', {
            'workspace': str(self.workspace), 'output_root': str(self.workspace),
            'coordinator': 'alpha', 'members': [
                {'id': 'alpha', 'display_name': 'Alpha', 'enabled': True,
                 'communication_mode': 'file'},
                {'id': 'beta', 'display_name': 'Beta', 'enabled': True,
                 'communication_mode': 'file'}]})
        member = next(item for item in view['members'] if item['id'] == 'alpha')
        self.assertTrue(member['identity_icon_data'].startswith('data:image/svg+xml;base64,'))
        self.assertNotIn('desktop_icon_path', member)
        self.assertFalse(self.bridge.call('choose_member_icon', {})['ok'])
        rejected = self.bridge.call('configure', {
            'workspace': str(self.workspace), 'output_root': str(self.workspace),
            'coordinator': 'alpha', 'members': [
                {'id': 'alpha', 'display_name': 'Alpha', 'enabled': True,
                 'communication_mode': 'file', 'desktop_icon_path': 'C:/private/agent.exe'},
                {'id': 'beta', 'display_name': 'Beta', 'enabled': True,
                 'communication_mode': 'file'}]})
        self.assertFalse(rejected['ok'])
        self.assertTrue(rejected['error'])

    def test_legacy_icon_paths_migrate_on_settings_save_and_leave_project_config(self):
        from agentbus.gui_state import load_member_icon_paths
        executable = self.workspace / 'legacy-agent.exe'
        executable.write_bytes(b'legacy icon')
        legacy_path = str(executable.resolve())
        cfg = load(self.workspace)
        cfg.data['workers']['alpha']['desktop_icon_path'] = legacy_path
        cfg = save(cfg)
        local_state = self.workspace / 'private-ui-state.json'
        with patch('agentbus.gui_state.state_path', return_value=local_state):
            self.data('configure', {
                'workspace': str(self.workspace), 'output_root': str(self.workspace),
                'coordinator': 'alpha', 'members': [
                    {'id': 'alpha', 'display_name': 'Alpha', 'enabled': True,
                     'communication_mode': 'file'},
                    {'id': 'beta', 'display_name': 'Beta', 'enabled': True,
                     'communication_mode': 'file'}]})
        self.assertNotIn('desktop_icon_path', load(self.workspace).worker('alpha'))
        self.assertEqual(load_member_icon_paths(self.workspace, local_state),
                         {'alpha': legacy_path})

    def test_old_ticket_cannot_checkpoint_successor_claim(self):
        self.tree()
        claim = self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        old_ticket = self.bridge._tickets[('first', 'alpha')]
        self.forest.handoff_leaf('first', 'alpha', old_ticket,
                                 reason='Formal external handoff', successor='alpha')
        newer = self.forest.claim_leaf('first', 'alpha')
        self.assertNotEqual(captured_ticket(newer).claim_id, old_ticket.claim_id)
        before = self.forest.path.read_bytes()
        self.assertFalse(self.bridge.call('checkpoint_leaf', {'leaf_id': 'first', 'agent': 'alpha',
                                                             'note': 'Cannot steal successor'})['ok'])
        self.assertEqual(before, self.forest.path.read_bytes())

    def test_explicit_inactive_owner_confirmation_can_handoff_external_claim(self):
        self.tree()
        original = self.forest.claim_leaf('first', 'alpha')
        result = self.data('handoff_leaf', {'leaf_id': 'first', 'agent': 'codex',
            'successor': 'beta', 'reason': 'User confirms original session has stopped',
            'owner_confirmed_inactive': True, 'expected_task_id': original.id, 'expected_owner': 'alpha', 'expected_claim_epoch': original.claim_epoch})
        self.assertEqual(result['owner'], 'beta')
        self.assertIn(original.id, result['history_task_ids'])
        events = self.forest.bus.events()
        self.assertTrue(any(e.get('event') == 'relinquish_authorized'
            and e.get('owner_confirmed_inactive') is True for e in events))

    def test_external_handoff_requires_literal_true_confirmation(self):
        self.tree()
        self.forest.claim_leaf('first', 'alpha')
        before = self.forest.path.read_bytes()
        for flag in (None, False, 1, 'true'):
            args = {'leaf_id': 'first', 'agent': 'codex', 'successor': 'beta', 'reason': 'No actual confirmation'}
            if flag is not None:
                args['owner_confirmed_inactive'] = flag
            self.assertFalse(self.bridge.call('handoff_leaf', args)['ok'])
        self.assertEqual(before, self.forest.path.read_bytes())

    def test_inactive_confirmation_does_not_validate_supplied_old_ticket(self):
        self.tree()
        claim = self.data('claim_leaf', {'leaf_id': 'first', 'agent': 'alpha'})
        old_ticket = self.bridge._tickets[('first', 'alpha')]
        self.forest.handoff_leaf('first', 'alpha', old_ticket,
                                 reason='External release', successor='alpha')
        self.forest.claim_leaf('first', 'alpha')
        before = self.forest.path.read_bytes()
        result = self.bridge.call('handoff_leaf', {'leaf_id': 'first', 'agent': 'alpha',
            'successor': 'beta', 'reason': 'Old ticket must not gain validity', 'ticket': old_ticket,
            'owner_confirmed_inactive': True})
        self.assertFalse(result['ok'])
        self.assertEqual(before, self.forest.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
