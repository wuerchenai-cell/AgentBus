"""§8.9 前后端动作面交叉核对的契约测试。

守卫三条纪律：
  1. "app.js 没静态调用" **不等于**"死键"——必须逐键给出分类证据；
  2. 前端调用未知键必须被检出（运行时错误）；
  3. 工具只读，不改前后端任何源码。
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from tools import bridge_surface_census as bsc
except ImportError:
    bsc = None


@unittest.skipIf(bsc is None, "bridge_surface_census tool not available in snapshot environment")
class BackendSurfaceTests(unittest.TestCase):
    def test_backend_keys_extracted(self):
        keys = bsc.backend_actions()
        self.assertIn('snapshot', keys)
        self.assertIn('update_leaf_topology', keys)
        self.assertEqual(len(keys), len(set(keys)), '后端动作键不应重复')

    def test_alias_groups_detected(self):
        aliases = bsc.backend_handler_aliases()
        flat = {k for group in aliases.values() for k in group}
        self.assertIn('append_leaf', flat)
        self.assertIn('add_leaf', flat)
        self.assertIn('add_task', flat)


@unittest.skipIf(bsc is None, "bridge_surface_census tool not available in snapshot environment")
class FrontendSurfaceTests(unittest.TestCase):
    def test_static_calls_extracted_with_lines(self):
        calls = bsc.frontend_calls()
        self.assertIn('snapshot', calls['static'])
        self.assertTrue(all(isinstance(n, int) for n in calls['static']['snapshot']))

    def test_dynamic_call_site_is_surfaced(self):
        calls = bsc.frontend_calls()
        self.assertTrue(calls['dynamic_lines'],
                        'app.js 存在变量式 call(view,...) —— 必须被显式列出而非忽略')


@unittest.skipIf(bsc is None, "bridge_surface_census tool not available in snapshot environment")
class CensusContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = bsc.census() if bsc is not None else {}

    def test_no_frontend_call_targets_unknown_action(self):
        self.assertEqual([], self.report['unknown_frontend_keys'],
                         '前端不得调用后端未注册的动作键')

    def test_every_unused_key_has_a_disposition(self):
        for key in self.report['unused_by_frontend']:
            self.assertIn(key, self.report['disposition'])

    def test_disposition_values_are_from_known_set(self):
        known = {'reachable_from_runtime', 'reachable_from_tests',
                 'reachable_dynamically', 'reachable_via_literal',
                 'alias_of_referenced', 'unreferenced_anywhere'}
        for key, kind in self.report['disposition'].items():
            self.assertIn(kind, known, '%s 的分类 %r 非法' % (key, kind))

    def test_note_states_unused_is_not_dead(self):
        self.assertIn('不等于死键', self.report['note'])

    def test_counts_match_disposition(self):
        counts = {}
        for kind in self.report['disposition'].values():
            counts[kind] = counts.get(kind, 0) + 1
        self.assertEqual(counts, self.report['disposition_counts'])
        self.assertEqual(sum(counts.values()), len(self.report['unused_by_frontend']))


@unittest.skipIf(bsc is None, "bridge_surface_census tool not available in snapshot environment")
class ReadOnlyToolTests(unittest.TestCase):
    def test_tool_source_has_no_destructive_calls(self):
        src = Path(bsc.__file__).read_text(encoding='utf-8')
        for token in ('unlink', 'rmtree', 'os.remove', 'os.rmdir',
                      'write_text(', 'write_bytes', 'shutil.move'):
            self.assertNotIn(token, src, '核对工具不得包含破坏性调用：%s' % token)


if __name__ == '__main__':
    unittest.main()
