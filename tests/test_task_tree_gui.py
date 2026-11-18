"""Task tree UI contracts; data and withdrawn Tk only, not frozen GUI acceptance.

Source: Owner's adopted task forest, Codex integration, 2026-10-08.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus import gui, task_tree_view
from agentbus.config import load, save


def forest():
    return {'roots': [{'id': 'project', 'title': '发布 AgentBus', 'goal': '项目总目标'}],
            'branches': [{'id': 'ui', 'parent_id': 'project', 'title': '桌面界面'}],
            'leaves': [{'id': 'leaf-ui', 'parent_id': 'ui', 'title': '任务树看板',
                        'state': 'queued', 'goal': '展示项目的任务树',
                        'results': 'agentbus/task_tree_view.py 已实现；回归待执行',
                        'handoff': '从选择叶子与接手按钮继续', 'owner': '',
                        'task_id': None, 'claimable': True, 'dependencies': []}]}


class ViewContracts(unittest.TestCase):
    def test_tree_is_project_branch_leaf_and_each_leaf_has_three_sections(self):
        view = task_tree_view.build_forest_view(forest())
        self.assertEqual([node['kind'] for node in view['nodes']], ['root', 'branch', 'leaf'])
        leaf = view['nodes'][-1]
        self.assertEqual(leaf['parent_id'], 'ui')
        self.assertEqual(set(leaf['sections']), {'目标', '成果', '接手说明'})
        self.assertIn('task_tree_view.py', leaf['sections']['成果'])

    def test_read_error_clears_completion_and_claimability_but_keeps_handoff(self):
        value = forest()
        value['leaves'][0].update(state='delivered', claimable=True)
        leaf = task_tree_view.build_forest_view(value, read_error='读取超时')['nodes'][-1]
        self.assertEqual(leaf['state_label'], '状态未确认')
        self.assertFalse(leaf['claimable'])
        self.assertIn('选择叶子', leaf['sections']['接手说明'])

    def test_empty_project_does_not_create_example_tasks_or_online_members(self):
        view = task_tree_view.build_forest_view({})
        self.assertEqual(view['nodes'], [])
        self.assertIn('尚无任务树', view['note'])

    def test_continuation_pack_preserves_task_and_owner_without_claiming_processing(self):
        leaf = forest()['leaves'][0]
        leaf.update(task_id='m-existing', owner='member-a', state='claimed')
        text = task_tree_view.continuation_text('/project', leaf, 'member-b')
        self.assertIn('m-existing', text)
        self.assertIn('member-a', text)
        self.assertIn('member-b', text)
        self.assertIn('processing', text)
        self.assertIn('目标', text)
        self.assertIn('成果', text)
        self.assertIn('接手说明', text)

    def test_delivered_is_labelled_receipt_not_project_release(self):
        value = forest()
        value['leaves'][0]['state'] = 'delivered'
        view = task_tree_view.build_forest_view(value)
        self.assertEqual(view['nodes'][-1]['state_label'], '已交付')
        self.assertIn('验收', view['note'])

    def test_artifact_drift_keeps_delivery_label_but_shows_warning_and_blocks_dependency(self):
        value = forest()
        value['leaves'][0].update(state='delivered', artifact_state='mismatch',
                                  dependency_ready=False, result_id='m-result')
        leaf = task_tree_view.build_forest_view(value)['nodes'][-1]
        self.assertEqual(leaf['state'], 'delivered')
        self.assertEqual(leaf['state_label'], '已交付 · 产物待核对')
        self.assertTrue(leaf['artifact_warning'])
        self.assertFalse(leaf['dependency_ready'])

    def test_long_receipt_and_hash_are_kept_in_technical_source(self):
        value = forest()
        original = '产物 SHA256 ' + 'a' * 64 + '\n' + '\n'.join('日志%d' % i for i in range(30))
        value['leaves'][0]['results'] = original
        leaf = task_tree_view.build_forest_view(value)['nodes'][-1]
        self.assertNotIn('a' * 64, leaf['sections']['成果'])
        self.assertNotIn('日志29', leaf['sections']['成果'])
        self.assertEqual(leaf['source']['results'], original)


class ActualTaskTreeActions(unittest.TestCase):
    def setUp(self):
        from agentbus.task_tree import TaskForest
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(self.temp.name, create=True)
        self.cfg.data['workers'] = {
            'codex': {'enabled': True, 'executor_enabled': True},
            'member-a': {'enabled': True, 'executor_enabled': True},
            'observer': {'enabled': True, 'executor_enabled': False},
        }
        save(self.cfg)
        self.tree = TaskForest(self.cfg)
        self.tree.seed_leaf({'id': 'leaf-ui', 'title': '真实任务树回调',
                             'goal': '继续完成任务树', 'handoff': '从原产出继续'}, branch_id='ui')

    def test_actual_claim_creates_one_task_and_copy_preserves_current_ticket(self):
        value = gui._task_tree_action(operation='claim', cfg=self.cfg,
                                     leaf_id='leaf-ui', agent='member-a')
        leaf = self.tree.snapshot()['leaves'][0]
        self.assertEqual(leaf['state'], 'claimed')
        self.assertEqual(leaf['owner'], 'member-a')
        path = self.cfg.workspace / '.agentbus/agents/member-a/processing' / (leaf['task_id'] + '.json')
        original = path.read_bytes()
        claimed = json.loads(original)
        self.assertIn('claim_id: ' + claimed['claim_id'], value['text'])
        self.assertIn('claim_epoch: ' + str(claimed['claim_epoch']), value['text'])
        copied = gui._task_tree_action(operation='copy', cfg=self.cfg,
                                      leaf_id='leaf-ui', agent='codex')
        self.assertEqual(path.read_bytes(), original)
        self.assertIn(leaf['task_id'], copied['text'])
        self.assertIn('member-a', copied['text'])
        self.assertNotIn(claimed['claim_id'], copied['text'])
        self.assertEqual(value['workspace'], str(self.cfg.workspace))
        with self.assertRaises(ValueError):
            gui._task_tree_action(operation='claim', cfg=self.cfg, leaf_id='leaf-ui', agent='codex')
        self.assertEqual(path.read_bytes(), original)

    def test_stop_arriving_after_display_prevents_real_claim(self):
        self.assertTrue(self.tree.snapshot()['leaves'][0]['claimable'])
        (self.cfg.workspace / '.agentbus/STOP').write_text('pause', encoding='utf-8')
        with self.assertRaises(ValueError):
            gui._task_tree_action(operation='claim', cfg=self.cfg, leaf_id='leaf-ui', agent='codex')
        self.assertIsNone(self.tree.snapshot()['leaves'][0]['task_id'])

    def test_saved_member_disabled_after_display_is_rechecked_at_click(self):
        current = load(self.cfg.workspace)
        current.data['workers']['member-a']['enabled'] = False
        save(current)
        with self.assertRaisesRegex(ValueError, '成员'):
            gui._task_tree_action(operation='claim', cfg=self.cfg, leaf_id='leaf-ui', agent='member-a')
        self.assertIsNone(self.tree.snapshot()['leaves'][0]['task_id'])

    def test_observer_cannot_claim_or_receive_execution_pack(self):
        for action in ('copy', 'claim'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                gui._task_tree_action(operation=action, cfg=self.cfg, leaf_id='leaf-ui', agent='observer')

    def test_dashboard_snapshot_is_real_data_without_creating_claim(self):
        before = (self.cfg.workspace / '.agentbus/task-tree.json').read_bytes()
        value = gui.read_dashboard_snapshot(self.cfg)
        self.assertEqual(value['task_forest']['leaves'][0]['id'], 'leaf-ui')
        self.assertEqual(value['model_calls'], 0)
        self.assertIsNone(value['task_forest_error'])
        self.assertEqual(before, (self.cfg.workspace / '.agentbus/task-tree.json').read_bytes())


class TkContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = gui.tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.cleanup)
        with patch('agentbus.gui.configure_initial_window'), \
             patch('agentbus.gui.initial_workspace', return_value=self.temp.name), \
             patch.object(gui.Launcher, '_auto_detect'):
            self.ui = gui.Launcher(self.root)
        self.root.update_idletasks()

    def cleanup(self):
        if hasattr(self, 'ui'):
            self.ui._overview_task.close()
            task = getattr(self.ui, '_task_tree_action_task', None)
            if task:
                task.close()
            self.ui._stop_watchtower()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def test_selection_renders_three_sections_and_real_tree_parentage(self):
        self.ui._render_task_forest(forest())
        self.assertEqual(self.ui.task_tree.parent('leaf-ui'), 'ui')
        self.assertEqual(self.ui.task_tree.parent('ui'), 'project')
        self.ui.task_tree.selection_set('leaf-ui')
        self.ui._select_task_leaf()
        text = self.ui.task_leaf_detail.get('1.0', 'end')
        for heading in ('目标', '成果', '接手说明'):
            self.assertIn(heading, text)
        self.assertIn('选择叶子', text)

    def test_twenty_configured_members_are_not_permanently_bound_to_branches(self):
        cfg = load(self.temp.name, create=True)
        cfg.data['workers'] = {'member-%d' % i: {'enabled': True, 'executor_enabled': True}
                               for i in range(20)}
        cfg.data['workers']['disabled-member'] = {'enabled': False}
        cfg.data['workers']['observer'] = {'enabled': True, 'executor_enabled': False}
        self.ui.cfg = cfg
        self.ui._render_task_forest(forest())
        self.assertEqual(len(self.ui.task_leaf_member_box.cget('values')), 20)
        self.assertTrue(self.ui.task_leaf_claim_btn.instate(['!disabled']))
        self.assertNotIn('member-', self.ui.task_tree.item('ui', 'text'))

    def test_processing_leaf_disables_claim_and_keeps_copy_continuation(self):
        cfg = load(self.temp.name, create=True)
        self.ui.cfg = cfg
        value = forest()
        value['leaves'][0].update(state='claimed', owner='codex', claimable=False)
        self.ui._render_task_forest(value)
        self.assertTrue(self.ui.task_leaf_claim_btn.instate(['disabled']))
        self.assertTrue(self.ui.task_leaf_copy_btn.instate(['!disabled']))

    def test_read_failure_retains_context_disables_actions_and_marks_unknown(self):
        self.ui.cfg = load(self.temp.name, create=True)
        self.ui._render_task_forest(forest(), read_error='状态读取失败')
        self.assertIn('状态未确认', self.ui.task_tree.item('leaf-ui', 'values'))
        self.assertTrue(self.ui.task_leaf_claim_btn.instate(['disabled']))
        self.assertTrue(self.ui.task_leaf_copy_btn.instate(['disabled']))

    def test_selected_leaf_survives_refresh_but_removed_leaf_has_no_stale_action(self):
        self.ui.cfg = load(self.temp.name, create=True)
        self.ui._render_task_forest(forest())
        self.ui.task_tree.selection_set('ui')
        self.ui._render_task_forest(forest())
        self.assertEqual(self.ui.task_tree.selection(), ('ui',))
        self.ui._render_task_forest({})
        self.assertEqual(self.ui.task_tree.selection(), ())
        self.assertTrue(self.ui.task_leaf_claim_btn.instate(['disabled']))
        self.assertIn('尚无任务树', self.ui.task_leaf_detail.get('1.0', 'end'))

    def test_technical_details_are_only_in_explicit_dialog(self):
        value = forest()
        value['leaves'][0]['evidence_sha256'] = 'a' * 64
        self.ui._render_task_forest(value)
        self.assertNotIn('a' * 64, self.ui.task_leaf_detail.get('1.0', 'end'))
        with patch.object(self.ui, '_show_readonly_text') as show:
            self.ui.task_leaf_technical_btn.invoke()
        self.assertIn('a' * 64, show.call_args.args[1])

    def test_member_choice_controls_claim_eligibility_without_changing_owner(self):
        cfg = load(self.temp.name, create=True)
        cfg.data['workers'] = {'member-a': {'enabled': True}, 'member-b': {'enabled': True}}
        self.ui.cfg = cfg
        value = forest()
        value['leaves'][0].update(owner='member-a', eligible_agents=['member-a'])
        self.ui._render_task_forest(value)
        self.assertEqual(self.ui.task_leaf_member_var.get(), 'member-a')
        self.assertTrue(self.ui.task_leaf_claim_btn.instate(['!disabled']))
        self.ui.task_leaf_member_var.set('member-b')
        self.ui._select_task_leaf()
        self.assertTrue(self.ui.task_leaf_claim_btn.instate(['disabled']))
        self.assertEqual(self.ui._task_forest_nodes['leaf-ui']['owner'], 'member-a')

    def test_refresh_preserves_user_collapsed_branch(self):
        self.ui._render_task_forest(forest())
        self.ui.task_tree.item('ui', open=False)
        self.ui._render_task_forest(forest())
        self.assertFalse(self.ui.task_tree.item('ui', 'open'))

    def test_switching_saved_project_clears_previous_tree_during_background_read(self):
        cfg = load(self.temp.name, create=True)
        self.ui.cfg = cfg
        self.ui._render_task_forest(forest())
        self.assertTrue(self.ui.task_tree.exists('leaf-ui'))
        other = load(Path(self.temp.name) / 'other-project', create=True)
        self.ui.cfg = other
        self.ui.ws_var.set(str(other.workspace))
        self.ui.output_var.set(str(other.output_root))
        self.ui.director_var.set(other.director)
        self.ui.worker_vars = {name: gui.tk.BooleanVar(value=True) for name in other.enabled_workers()}
        with patch.object(self.ui._overview_task, 'start', return_value=False):
            self.ui._refresh_workbench_overview()
        self.assertFalse(self.ui.task_tree.exists('leaf-ui'))
        self.assertTrue(self.ui.task_leaf_claim_btn.instate(['disabled']))

    def test_draft_project_change_during_copy_does_not_replace_clipboard(self):
        cfg = load(self.temp.name, create=True)
        self.ui.cfg = cfg
        self.ui.ws_var.set(str(cfg.workspace))
        self.ui._render_task_forest(forest())
        self.root.clipboard_clear()
        self.root.clipboard_append('当前项目剪贴板')
        release = threading.Event()
        def action(**_request):
            release.wait(2)
            return {'workspace': str(cfg.workspace), 'text': '旧项目接手包'}
        with patch.object(self.ui, '_workflow_cfg', return_value=cfg), \
             patch('agentbus.gui._task_tree_action', side_effect=action):
            self.ui.task_leaf_copy_btn.invoke()
            self.ui.ws_var.set(str(cfg.workspace / 'new-project'))
            release.set()
            self.ui._task_tree_action_task.worker.join(2)
            self.ui._poll_task_tree_action()
        self.assertEqual(self.root.clipboard_get(), '当前项目剪贴板')

    def test_action_worker_rechecks_fresh_snapshot_off_tk_thread(self):
        cfg = load(self.temp.name, create=True)
        self.ui.cfg = cfg
        self.ui._render_task_forest(forest())
        member = next(iter(cfg.enabled_workers()))
        self.ui.task_leaf_member_var.set(member)
        seen = []
        def action(**request):
            seen.append((threading.get_ident(), request))
            return {'workspace': str(cfg.workspace), 'text': '实际接手包'}
        with patch.object(self.ui, '_workflow_cfg', return_value=cfg), \
             patch('agentbus.gui._task_tree_action', side_effect=action):
            self.ui.task_leaf_copy_btn.invoke()
            self.ui._task_tree_action_task.worker.join(2)
            self.ui._poll_task_tree_action()
        self.assertNotEqual(seen[0][0], threading.get_ident())
        self.assertEqual(seen[0][1]['leaf_id'], 'leaf-ui')
        self.assertEqual(self.root.clipboard_get(), '实际接手包')


class IsolatedRealTkFixture(unittest.TestCase):
    def test_actual_constructor_and_tree_render_with_isolated_preferences(self):
        from agentbus import gui_state
        from agentbus.task_tree import TaskForest
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / 'project'
            cfg = load(workspace, create=True)
            cfg.data['workers'] = {'codex': {'enabled': True, 'executor_enabled': True}}
            save(cfg)
            TaskForest(cfg).seed_leaf({'id': 'leaf-actual', 'title': '真实控件构建',
                                      'goal': '展示根、子树和叶子',
                                      'handoff': '从真实控件的断点继续'}, branch_id='actual-branch')
            preference = Path(folder) / 'isolated-ui-state.json'
            preference.write_text(json.dumps({'workspace': str(workspace)}), encoding='utf-8')
            original = preference.read_bytes()
            root = gui.tk.Tk()
            root.withdraw()
            errors = []
            root.report_callback_exception = lambda *args: errors.append(args)
            ui = None
            try:
                with patch.object(gui_state, 'state_path', return_value=preference), \
                     patch('agentbus.gui.configure_initial_window'), \
                     patch.object(gui.Launcher, '_auto_detect'):
                    ui = gui.Launcher(root)
                    self.assertEqual(Path(ui.ws_var.get()), workspace)
                    ui.cfg = cfg
                    value = gui.read_dashboard_snapshot(cfg)
                    ui._render_snapshot(cfg.data, value)
                    root.geometry('1150x760')
                    root.update_idletasks()
                    self.assertTrue(ui.task_tree.winfo_exists())
                    self.assertEqual(ui.task_tree.parent('actual-branch'), 'project')
                    self.assertEqual(ui.task_tree.parent('leaf-actual'), 'actual-branch')
                    self.assertIn('目标', ui.task_leaf_detail.get('1.0', 'end'))
                    self.assertIn('成果', ui.task_leaf_detail.get('1.0', 'end'))
                    self.assertIn('接手说明', ui.task_leaf_detail.get('1.0', 'end'))
                    self.assertEqual(errors, [])
                    self.assertEqual(preference.read_bytes(), original)
            finally:
                if ui:
                    ui._overview_task.close()
                    ui._stop_watchtower()
                for callback in root.tk.call('after', 'info'):
                    root.after_cancel(callback)
                root.destroy()


if __name__ == '__main__':
    unittest.main()
