"""User-facing maturity contracts. Source: group review, 2026-10-03."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.config import load
from agentbus.gui import Launcher


class DiagnosticGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name) / '项目', create=True)
        self.ui = Launcher.__new__(Launcher)
        self.ui.root = object()
        self.ui._workflow_cfg = lambda: self.cfg

    def test_save_diagnostic_uses_explicit_destination_and_no_model(self):
        destination = Path(self.temp.name) / '诊断.json'
        with patch('agentbus.gui.filedialog.asksaveasfilename', return_value=str(destination)), \
             patch('agentbus.gui.save_diagnostic', return_value={'model_calls': 0}) as save, \
             patch('agentbus.gui.messagebox.showinfo') as info:
            self.ui._save_diagnostic()
        self.assertEqual(save.call_args.args[1], destination)
        self.assertIn('不含任务正文', info.call_args.args[1])

    def test_cancel_diagnostic_does_not_write(self):
        with patch('agentbus.gui.filedialog.asksaveasfilename', return_value=''), \
             patch('agentbus.gui.save_diagnostic') as save:
            self.ui._save_diagnostic()
        save.assert_not_called()

    def test_existing_destination_is_preserved_with_clear_error(self):
        with patch('agentbus.gui.filedialog.asksaveasfilename', return_value='existing.json'), \
             patch('agentbus.gui.save_diagnostic', side_effect=FileExistsError('existing.json')), \
             patch('agentbus.gui.messagebox.showwarning') as warning:
            self.ui._save_diagnostic()
        self.assertIn('未保存', warning.call_args.args[0])

    def test_host_hint_does_not_certify_loading(self):
        from agentbus.gui import host_loading_hint
        hint = host_loading_hint('trae')
        self.assertIn('技能与命令', hint)
        self.assertIn('启用', hint)
        self.assertIn('不能证明', hint)
        self.assertIn('接入句', host_loading_hint('custom-sixth'))

    def test_goal_processing_shows_owner_and_file_age_without_restarting(self):
        from agentbus.gui import jobs_summary
        job = {'id': 'j-fixture', 'goal': 'bounded work', 'state': 'blocked',
               'goal_location': 'processing', 'goal_status': 'pending',
               'goal_claimed_by': '', 'goal_claim_complete': False,
               'goal_heartbeat_age_s': 620, 'goal_heartbeat_stale': True}
        text = jobs_summary(self.cfg, [job])
        self.assertIn('目标认领者', text)
        self.assertIn('认领未完整提交', text)
        self.assertIn('620', text)
        self.assertIn('不证明进程死亡', text)
        self.assertIn('不能抢占', text)

    def test_recovery_cancel_does_not_touch_processing(self):
        report = {'processing': [{'agent': 'workbuddy', 'id': 'm-fixture'}]}
        with patch('agentbus.gui.inspect_project', return_value=report), \
             patch('agentbus.gui.simpledialog.askstring', return_value='m-fixture'), \
             patch('agentbus.gui.messagebox.askyesno', return_value=False) as question, \
             patch.object(__import__('agentbus.gui', fromlist=['Bus']).Bus,
                          'recover_claim', create=True) as recover:
            self.ui._recover_processing()
        recover.assert_not_called()
        self.assertIn('已停止', question.call_args.args[1])
        self.assertEqual(question.call_args.kwargs['default'], 'no')

    def test_recovery_confirms_exact_message_and_preserves_budget(self):
        report = {'processing': [{'agent': 'workbuddy', 'id': 'm-fixture'}]}
        with patch('agentbus.gui.inspect_project', return_value=report), \
             patch('agentbus.gui.simpledialog.askstring', return_value='m-fixture'), \
             patch('agentbus.gui.messagebox.askyesno', return_value=True), \
             patch('agentbus.gui.messagebox.showinfo') as info, \
             patch.object(__import__('agentbus.gui', fromlist=['Bus']).Bus,
                          'recover_claim', create=True) as recover:
            self.ui._recover_processing()
        recover.assert_called_once_with('workbuddy', 'm-fixture', owner_confirmed_inactive=True)
        self.assertIn('预算', info.call_args.args[1])

    def test_recovery_unknown_message_never_calls_bus(self):
        with patch('agentbus.gui.inspect_project', return_value={'processing': []}), \
             patch('agentbus.gui.messagebox.showinfo') as info, \
             patch.object(__import__('agentbus.gui', fromlist=['Bus']).Bus,
                          'recover_claim', create=True) as recover:
            self.ui._recover_processing()
        recover.assert_not_called()
        self.assertIn('没有', info.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
