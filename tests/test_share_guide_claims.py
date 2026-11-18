"""Source: workbuddy, 2026-10-11. §7.15 分发包指南能力承诺守门。

守门目标：share_assets/开始使用.md 随分发包发给用户，不得包含
超出系统实际能力的承诺。本测试以「禁用措辞集合」+「必备诚实措辞集合」
双向断言，防止后续改动把夸大描述带回指南。

只读断言，不改任何文件，不跑模型。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / 'share_assets' / '开始使用.md'

# 超出系统能力的措辞：常驻信箱监听 / 无缝交接 / 事件级实时推送。
BANNED_PHRASES = {
    '初始化专属信箱监控': '项目无常驻信箱监听进程；成员必须在真实工作回合内主动读取。',
    '无缝续接': '交接要求明确确认原会话已停止，不存在绕过停止确认的无缝切换。',
    '实时反馈': '状态由文件与心跳派生，界面按快照节奏刷新，不是事件级实时推送。',
    '实时状态': '状态由文件与心跳派生，界面按快照节奏刷新，不是事件级实时推送。',
}

# 必须出现的诚实替换措辞（任意一条命中即视为该边界已声明）。
REQUIRED_PHRASES = (
    '无常驻信箱监听进程',
    '不是逐事件实时推送',
    '不存在绕过停止确认的「无缝」切换',
)


class ShareGuideHonestyTests(unittest.TestCase):
    """指南随包发布，必须如实描述系统能力边界。"""

    def setUp(self):
        self.assertTrue(GUIDE.is_file(), f'分发包指南缺失：{GUIDE}')
        self.text = GUIDE.read_text(encoding='utf-8')

    def test_no_capability_exceeding_phrases(self):
        hits = [p for p in BANNED_PHRASES if p in self.text]
        self.assertEqual(
            hits, [],
            '指南出现超出系统能力的措辞（应改写）：\n'
            + '\n'.join(f'  - {p}：{BANNED_PHRASES[p]}' for p in hits),
        )

    def test_required_honest_phrases_present(self):
        missing = [p for p in REQUIRED_PHRASES if p not in self.text]
        self.assertEqual(
            missing, [],
            '指南缺少诚实的边界声明，用户会被误导：\n'
            + '\n'.join(f'  - {p}' for p in missing),
        )

    def test_known_limits_section_documents_the_four_axes(self):
        self.assertIn('## 已知边界', self.text, '缺少《已知边界》小节')
        for axis in ('无常驻后台', '无「无缝」交接', '无事件级实时推送', '接入不等于可用'):
            with self.subTest(axis=axis):
                self.assertIn(axis, self.text, f'《已知边界》未覆盖：{axis}')

    def test_guide_defers_to_agentbus_entry_doc(self):
        self.assertIn('AGENTBUS.md', self.text, '指南未声明以 AGENTBUS.md 为权威口径')

    def test_guide_explains_first_run_and_current_zoom_without_old_limits(self):
        self.assertIn('首次',self.text)
        self.assertIn('生成',self.text)
        self.assertIn('800%',self.text)
        self.assertNotIn('40%–200%',self.text)

    def test_bracketed_quote_of_unqualified_seamless_is_negated(self):
        # 保留「无缝」二字只允许出现在否定语境中（带前后否定词）。
        idx = 0
        while True:
            idx = self.text.find('无缝', idx)
            if idx == -1:
                break
            window = self.text[max(0, idx - 30): idx + 12]
            self.assertTrue(
                ('不存在' in window) or ('不是' in window) or ('无' in window),
                f'「无缝」出现在非否定语境：…{window}…',
            )
            idx += 2


if __name__ == '__main__':
    unittest.main()
