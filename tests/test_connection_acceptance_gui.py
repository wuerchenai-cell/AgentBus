"""Source: Trae/ZCode product review and Codex, 2026-10-04.

Rendering tests only: no filesystem, model, network or credential access.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from copy import deepcopy
import unittest
from unittest.mock import patch

from agentbus.gui import render_connection_summary


class ConnectionAcceptanceGuiTests(unittest.TestCase):
    def member(self, name='workbuddy', mode='对话接入', **extra):
        value = {
            'name': name, 'display_name': name.title(), 'mode_label': mode,
            'indicator': {'symbol': '●', 'label': '暂无任务，会话未确认'},
            'receipt_count': 0, 'successful_receipt_count': 0,
            'failed_receipt_count': 0, 'next_step': '打开当前项目并发送接入指令。',
        }
        value.update(extra)
        return value

    def render(self, *members, **extra):
        value = {'workspace': 'example-project', 'coordinator': 'codex',
                 'members': list(members)}
        value.update(extra)
        return render_connection_summary(value)

    def test_each_member_has_two_separate_acceptance_rows(self):
        text = self.render(self.member(), self.member('codex', '独立接口（已配置）'))
        self.assertEqual(text.count('原生新聊天接入：'), 2)
        self.assertEqual(text.count('长期无人接力：'), 2)
        self.assertEqual(text.count('原生新聊天接入：未验证（需当前聊天调用技能）'), 2)

    def test_admission_and_successful_receipts_never_certify_either_acceptance(self):
        text = self.render(self.member(
            admission_confirmation_at='2026-10-04T10:00:00+08:00',
            receipt_count=100, successful_receipt_count=100,
            last_receipt_at='2026-10-04T10:01:00+08:00', latest_task_outcome='done'))
        self.assertIn('文件接入确认：已登记', text)
        self.assertIn('任务回执：100 条（成功 100 / 失败 0）', text)
        rows = [row for row in text.splitlines()
                if row.startswith(('原生新聊天接入：', '长期无人接力：'))]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('未验证' in row and '通过' not in row for row in rows))

    def test_independent_interface_configuration_does_not_certify_long_relay(self):
        text = self.render(self.member('opencode', '独立接口（已配置）'))
        self.assertIn('接入方式：独立接口（已配置）', text)
        self.assertIn('长期无人接力：未验证（未完成多轮/空闲/重启验收）', text)

    def test_desktop_interface_explicitly_requires_human_handoff(self):
        text = self.render(self.member('trae'))
        self.assertIn('长期无人接力：未验证（桌面对话需人工接手）', text)
        self.assertIn('再次需要人工唤醒不算无人接力通过', text)
        self.assertIn('任务可保存、等待人工接手只说明人工交接可用', text)

    def test_member_source_and_acceptance_next_step_are_visible(self):
        text = self.render(self.member())
        self.assertIn('状态来源：本项目配置与文件总线记录', text)
        self.assertIn('验收下一步：在当前项目的新对话调用小组技能', text)
        self.assertIn('独立接口还需核验连续多轮、空闲后接力和重启恢复', text)
        self.assertIn('下一步：打开当前项目并发送接入指令。', text)

    def test_known_desktop_vendors_expose_separate_cli_auth_and_permission_boundary(self):
        for name in ('trae', 'workbuddy', 'zcode'):
            for mode in ('对话接入', '独立接口（已配置）'):
                with self.subTest(name=name, mode=mode):
                    text = self.render(self.member(name, mode))
                    self.assertIn('厂商 CLI 的认证与权限需单独确认', text)
                    self.assertIn('不会自动继承桌面 GUI 会话的授权', text)
                    self.assertNotIn('旗舰', text)
                    self.assertNotIn('订阅', text)

    def test_dynamic_members_display_same_acceptance_without_vendor_assumptions(self):
        text = self.render(self.member('custom-agent', '独立接口（已配置）'))
        self.assertIn('[custom-agent]', text)
        self.assertIn('原生新聊天接入：未验证（需当前聊天调用技能）', text)
        self.assertIn('长期无人接力：未验证（未完成多轮/空闲/重启验收）', text)
        self.assertNotIn('厂商 CLI 的认证与权限', text)

    def test_unknown_mode_is_not_assumed_to_be_independently_runnable(self):
        text = self.render(self.member('custom-agent', '自定义接入'))
        self.assertIn('长期无人接力：未验证（接入方式未确认）', text)
        self.assertNotIn('桌面对话需人工接手', text)

    def test_existing_summary_flags_are_not_native_or_long_relay_certificates(self):
        text = self.render(self.member(current_session_verified=True),
                           current_sessions_verified=True, continuous_relay_verified=True)
        self.assertIn('原生新聊天接入：未验证（需当前聊天调用技能）', text)
        self.assertIn('长期无人接力：未验证（桌面对话需人工接手）', text)

    def test_pending_desktop_task_stays_visible_as_human_handoff(self):
        text = self.render(self.member(
            indicator={'symbol': '●', 'label': '有任务待接手'},
            next_step='复制接入指令，等待 WorkBuddy 人工接手。'))
        self.assertIn('有任务待接手', text)
        self.assertIn('桌面对话需人工接手', text)
        self.assertIn('下一步：复制接入指令，等待 WorkBuddy 人工接手。', text)

    def test_render_is_pure_and_preserves_existing_summary_and_indicator(self):
        value = {'workspace': 'example-project', 'coordinator': 'codex',
                 'members': [self.member()]}
        before = deepcopy(value)
        with patch('builtins.open', side_effect=AssertionError('unexpected file access')), \
                patch('io.open', side_effect=AssertionError('unexpected path access')), \
                patch('agentbus.gui.connection_summary', side_effect=AssertionError('unexpected observation')), \
                patch('subprocess.Popen', side_effect=AssertionError('unexpected process')):
            text = render_connection_summary(value, selected='workbuddy')
        self.assertEqual(value, before)
        self.assertIn('● Workbuddy [workbuddy] → 当前选择 · 暂无任务，会话未确认', text)
        self.assertIn('原生新聊天接入：', text)

    def test_empty_team_retains_configuration_guidance(self):
        text = self.render()
        self.assertIn('先选择项目、勾选成员和组长，然后保存团队。', text)
        self.assertNotIn('原生新聊天接入：', text)


if __name__ == '__main__':
    unittest.main()
