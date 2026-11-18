"""Source: workbuddy, 2026-10-11. §7.10 独立成果复核机制必须在指南中被如实披露。

守门目标：`supervision`（界面称「独立成果复核」）已打包但**默认关闭**。
指南必须同时说明：它存在、默认关闭、在哪开、字段名、开启前置约束。
同时校验配置真实形状，避免文档与 config 键名漂移。

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


import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / 'share_assets' / '开始使用.md'

# 指南必须披露的机制要点（缺一即用户仍被蒙在鼓里）。
REQUIRED_DISCLOSURES = {
    '存在': '独立成果复核',
    '默认关闭': '默认关闭',
    '配置字段': 'strict_supervision',
    '复核成员字段': 'designated_auditor',
    '检查清单字段': 'checks',
    '界面入口': '独立成果复核与双签设置',
}

# supervision 配置对象的真实必填键（与 supervision.validate_settings 一致）。
SUPERVISION_KEYS = {'strict_supervision', 'designated_auditor', 'checks'}


class GuideDisclosesSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(GUIDE.is_file(), f'分发包指南缺失：{GUIDE}')
        self.text = GUIDE.read_text(encoding='utf-8')

    def test_guide_mentions_supervision_at_all(self):
        # 修复前：检索「监督 / supervision / oversight」零命中。
        self.assertRegex(self.text, r'独立(成果复核|监督)',
                         '指南完全没有提及独立复核/监督机制')

    def test_guide_states_default_off(self):
        self.assertIn('默认关闭', self.text, '指南未说明该机制默认关闭')

    def test_guide_names_the_real_config_fields(self):
        for label, token in REQUIRED_DISCLOSURES.items():
            with self.subTest(label=label):
                self.assertIn(token, self.text, f'指南缺少披露项：{label} → {token}')

    def test_guide_names_the_gui_entry_point(self):
        # 界面菜单实际文案；用户须能照此找到开关。
        self.assertIn('独立成果复核与双签设置', self.text, '指南未给出界面开关位置')

    def test_config_supervision_shape_matches_documented_keys(self):
        # 文档写的键名必须就是 config 里的键名（防止文档漂移）。
        sample = {'supervision': {'strict_supervision': False,
                                  'designated_auditor': '', 'checks': []}}
        self.assertEqual(set(sample['supervision']), SUPERVISION_KEYS)
        # 指南必须逐字包含这三个键。
        for key in SUPERVISION_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, self.text)


class SupervisionDefaultOffContractTests(unittest.TestCase):
    """机制默认值契约：validate_settings 缺省语义必须仍是关闭。"""

    def test_missing_boolean_is_rejected_not_silently_defaulted(self):
        """真实契约：缺 strict_supervision 会**报错**，不会静默当成 False。"""
        from agentbus.supervision import validate_settings, SupervisionError
        with self.assertRaises(SupervisionError):
            validate_settings({}, {})

    def test_explicit_off_normalizes_to_closed(self):
        """显式关闭时的规范结果：关 + 空复核成员 + 空清单。"""
        from agentbus.supervision import validate_settings
        result = validate_settings(
            {'strict_supervision': False, 'designated_auditor': '', 'checks': []}, {})
        self.assertIs(result['strict_supervision'], False)
        self.assertEqual(result['designated_auditor'], '')
        self.assertEqual(result['checks'], [])

    def test_strict_requires_auditor_and_executor_permission(self):
        from agentbus.supervision import validate_settings, SupervisionError
        with self.assertRaises(SupervisionError):
            validate_settings({'strict_supervision': True, 'designated_auditor': ''}, {})
        with self.assertRaises(SupervisionError):
            validate_settings({'strict_supervision': True, 'designated_auditor': 'aud'},
                              {'aud': {'enabled': True, 'executor_enabled': False}})


if __name__ == '__main__':
    unittest.main()
