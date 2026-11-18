"""
Tests for BACKGROUND-DISPATCH-TRUTH (P1, §7.32)
Source: Antigravity (2026-10-12)

Verifies:
1. WebBridge `_dispatch_ready` guarantees non-model, pure-file dispatch semantics:
   - model_calls == 0
   - automatic_execution is False
   - file_event_only is True
   - dispatch_truth metadata reflects foreground-only boundary and paused-on-hidden policy.
2. In frontend assets (app.js):
   - Document visibility checks (document.hidden) guard both scheduleAutomaticDispatch and runAutomaticDispatch.
   - Visibilitychange event cleanly suspends timers on hide and reschedules on show.
   - UI status text continuously and unambiguously displays paused/suspended state when hidden or minimized.
   - Toast explicitly notes dispatches are file events without claiming external model invocation.
3. System STOP gate enforces dispatch boundary:
   - While STOP is active, dispatch_ready raises ValueError and refuses to dispatch.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus
from agentbus.config import load, save
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class TestBackgroundDispatchTruth(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.cfg = load(self.workspace, create=True)
        self.cfg.data['workers']['codex']['enabled'] = True
        self.cfg.data['workers']['codex']['executor_enabled'] = True
        self.cfg.data['workers']['codex']['capabilities'] = ['code', 'review', 'test']
        self.cfg.data['assignment_mode'] = 'automatic'
        save(self.cfg)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_web_bridge_dispatch_ready_truth_metadata(self):
        # Create a simple task tree with one ready leaf
        tree_path = self.workspace / '.agentbus/task-tree.json'
        tree_content = {
            "schema_version": 1,
            "section_registry": ["1.1"],
            "roots": [{"id": "root", "title": "根目标", "goal": "根目标", "parent_id": None}],
            "branches": [],
            "leaves": [{
                "id": "leaf-1",
                "section_no": "1.1",
                "parent_id": "root",
                "title": "就绪测试任务",
                "goal": "完成测试",
                "task_id": None,
                "job_id": None,
                "dependencies": [],
                "required_capabilities": []
            }]
        }
        tree_path.write_text(json.dumps(tree_content, ensure_ascii=False), encoding='utf-8')

        bridge = WebBridge(self.workspace)
        res = bridge.call('dispatch_ready', {})
        self.assertTrue(res.get('ok'))
        data = res.get('data', {})

        # Verify dispatch truth fields
        self.assertEqual(data.get('model_calls'), 0)
        self.assertIs(data.get('automatic_execution'), False)
        self.assertIs(data.get('file_event_only'), True)

        dispatch_truth = data.get('dispatch_truth', {})
        self.assertEqual(dispatch_truth.get('frontend_state'), 'foreground_only')
        self.assertEqual(dispatch_truth.get('background_policy'), 'paused_on_hidden_or_minimize')
        self.assertEqual(dispatch_truth.get('model_execution'), 'manual_or_external_only')
        self.assertIs(dispatch_truth.get('file_event_only'), True)

        # Verify dispatched message is a pure file event in codex ready mailbox
        self.assertEqual(len(data.get('assignments', [])), 1)
        dispatched_task_id = data['assignments'][0]['task_id']
        dispatched_file = self.workspace / f'.agentbus/agents/codex/ready/{dispatched_task_id}.json'
        self.assertTrue(dispatched_file.is_file())
        task_data = json.loads(dispatched_file.read_text(encoding='utf-8'))
        self.assertEqual(task_data.get('to'), 'codex')
        self.assertEqual(task_data.get('type'), 'task')

    def test_dispatch_ready_fails_when_stopped(self):
        # When stopped, dispatch_ready must reject dispatching
        stop_marker = self.workspace / '.agentbus/STOP'
        stop_marker.write_text(json.dumps({'caller': 'admin', 'reason': 'halt'}), encoding='utf-8')

        bridge = WebBridge(self.workspace)
        res = bridge.call('dispatch_ready', {})
        self.assertFalse(res.get('ok'))
        self.assertIn('项目已暂停', res.get('error', ''))

    def test_frontend_visibility_guards_and_truth_copy(self):
        app_js_path = Path(__file__).resolve().parents[1] / 'agentbus/web_assets/app.js'
        app_js = app_js_path.read_text(encoding='utf-8')

        # 1. Guards on document.hidden
        self.assertIn("if(!autoDispatchActive||document.hidden||!canAutoDispatch(online,snapshot.project,snapshot.forest,assignmentMode()))return;", app_js)
        self.assertIn("if(!autoDispatchActive||autoDispatchInFlight||document.hidden||!canAutoDispatch(online,snapshot.project,snapshot.forest,assignmentMode()))return;", app_js)

        # 2. Visibilitychange event listener suspends/reschedules timer
        self.assertIn("document.addEventListener('visibilitychange'", app_js)
        self.assertIn("if(!document.hidden)scheduleAutomaticDispatch()", app_js)

        # 3. Truthful paused-on-hidden copy in assignment note & flow summary
        self.assertIn("窗口在后台/最小化 · 自动派工已暂停（仅前台激活）", app_js)
        self.assertIn("窗口在后台 · 自动派工已挂起（切回前台后恢复）", app_js)
        self.assertIn("后台挂起中", app_js)

        # 4. Truthful toast without claiming model activation
        self.assertIn("项就绪任务至成员信箱（纯文件事件，不自动调用外部模型）", app_js)


if __name__ == '__main__':
    unittest.main()
