"""Source: real team long-job continuity findings, Codex, 2026-10-03."""
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
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agentbus import cli, gui
from agentbus.project_workflow import peer_skill


class ContinuationInterfaceTests(unittest.TestCase):
    def command(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(argv)
        return code, output.getvalue()

    def test_cli_explicit_parent_is_passed_to_start_api(self):
        with patch('agentbus.cli._load', return_value='current cfg'), \
             patch('agentbus.jobs.start_job', return_value={'id': 'j-new'}) as start:
            code, output = self.command(['team', 'start', '--workspace', 'project',
                '--caller', 'codex', '--goal', 'A new authorized stage',
                '--request-id', 'same-request', '--from-job', 'j-parent'])
        self.assertEqual(code, 0)
        self.assertIn('j-new', output)
        start.assert_called_once_with('current cfg', 'codex', 'A new authorized stage',
                                      'same-request', parent_job_id='j-parent')

    def test_cli_normal_continue_status_does_not_create_job(self):
        with patch('agentbus.cli._load', return_value='cfg'), \
             patch('agentbus.jobs.list_jobs', return_value=[]) as status, \
             patch('agentbus.jobs.start_job') as start:
            code, _ = self.command(['team', 'status', '--workspace', 'project'])
        self.assertEqual(code, 0)
        status.assert_called_once_with('cfg')
        start.assert_not_called()

    def test_parent_option_is_rejected_for_non_start_actions(self):
        with patch('agentbus.cli._load', return_value='cfg'), \
             patch('agentbus.jobs.list_jobs') as status:
            code, _ = self.command(['team', 'status', '--workspace', 'project',
                                    '--from-job', 'j-parent'])
        self.assertEqual(code, 4)
        status.assert_not_called()

    def test_jobs_view_identifies_previous_stage_without_suggesting_auto_restart(self):
        cfg = SimpleNamespace(workspace=Path('current project'), enabled_workers=lambda: {})
        job = {'id': 'j-new', 'state': 'queued', 'goal': 'new stage',
               'parent_job_id': 'j-old', 'parent_snapshot': {
                   'state': 'blocked', 'done_task_ids': ['m-one', 'm-two']}}
        text = gui.jobs_summary(cfg, [job])
        self.assertIn('上一阶段：j-old', text)
        self.assertIn('已完成任务：2', text)
        self.assertIn('旧阶段期限与记录保留', text)

    def test_peer_workflow_distinguishes_continue_and_new_stage(self):
        text = peer_skill('trae')
        self.assertIn('--from-job', text)
        self.assertIn('明确授权开启新阶段', text)
        self.assertIn('不修改旧期限', text)

    def test_workflow_status_failed_readiness_has_nonzero_exit(self):
        with patch('agentbus.cli._load', return_value='cfg'), \
             patch('agentbus.cli.workflow_status', return_value={
                 'ready': False, 'reason': '打开新位置的 EXE → 保存小组'}):
            code, output = self.command(['workflow', 'status', '--workspace', 'project'])
        self.assertEqual(code, 1)
        self.assertIn('打开新位置', output)

    def test_copy_resume_preserves_specific_launcher_repair_reason(self):
        app = SimpleNamespace(_workflow_cfg=lambda: 'cfg', root=SimpleNamespace(
            clipboard_clear=lambda: self.fail('Invalid entry must not copy')))
        reason = '程序入口不存在。打开新位置的 EXE → 选择当前项目 → 保存小组'
        with patch('agentbus.gui.workflow_status', return_value={
                 'ready': False, 'reason': reason}), \
             patch('agentbus.gui.messagebox.showwarning') as warning, \
             patch('agentbus.gui.resume_prompt') as prompt:
            gui.Launcher._copy_resume(app)
        warning.assert_called_once_with('无法复制', reason)
        prompt.assert_not_called()

    def test_config_saved_does_not_claim_broken_workflow_is_ready(self):
        output = io.StringIO()
        with patch('agentbus.cli.prepare_project_workflow', return_value={'changed': []}), \
             patch('agentbus.cli.workflow_status', return_value={
                 'ready': False, 'reason': '程序入口不存在，请保存小组'}), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli._saved_configuration('cfg', {})
        self.assertEqual(code, 4)
        data = json.loads(output.getvalue())
        self.assertTrue(data['saved'])
        self.assertFalse(data['workflow_ready'])
        self.assertIn('程序入口不存在', data['workflow_error'])

    def test_gui_prepare_reports_invalid_launcher_instead_of_ready(self):
        cfg = SimpleNamespace(workspace=Path('project'), director='codex')
        render = MagicMock()
        app = SimpleNamespace(_workflow_cfg=lambda: cfg, workflow_box=MagicMock(),
                              _refresh_workflow=render)
        reason = '程序入口不存在，请打开新位置的 EXE 并保存小组'
        with patch('agentbus.gui.prepare_project_workflow'), \
             patch('agentbus.gui.workflow_status', return_value={
                 'ready': False, 'reason': reason, 'files': {}, 'native_loading': ''}), \
             patch('agentbus.gui.messagebox.showwarning') as warning:
            gui.Launcher._prepare_workflow(app)
        warning.assert_called_once_with('工作流需处理', reason)
        render.assert_called_once_with(cfg, error=reason)


if __name__ == '__main__':
    unittest.main()
