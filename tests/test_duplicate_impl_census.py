"""§8.12 重复实现普查工具的契约测试。

守卫三条纪律：
  1. "逐语句相同"必须靠 AST 等价判定——同体**不同名**也要测出；
  2. 惯用法归类只归类，**不得**被当成"等价"结论；
  3. 工具本身**只读**，绝不合并/删除/写任何被测文件。
"""
import ast
from pathlib import Path
import tempfile
import textwrap
import unittest

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from tools import duplicate_impl_census as dic
except ImportError:
    dic = None


@unittest.skipIf(dic is None, "duplicate_impl_census tool not available in snapshot environment")
class ExactDuplicateDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pkg = Path(self.temp.name) / 'pkg'
        self.pkg.mkdir()
        (self.pkg / '__init__.py').write_text('', encoding='utf-8')

    def _write(self, name, text):
        (self.pkg / name).write_text(textwrap.dedent(text), encoding='utf-8')

    def _groups(self):
        report = dic.census(package=str(self.pkg))
        return [g['members'] for g in report['exact_duplicate_groups']]

    def test_same_body_different_name_is_detected(self):
        # 关键：不同名但同体，必须被测出（旧实现漏判的正是这一类）
        self._write('a.py', 'def read_json(p):\n    return p.read_text()\n')
        self._write('b.py', 'def read_state(p):\n    return p.read_text()\n')
        flat = {tuple(g) for g in self._groups()}
        self.assertIn(('a.py.read_json', 'b.py.read_state'), flat)

    def test_identical_name_same_module_not_flagged(self):
        self._write('a.py', 'def only(p):\n    return p\n')
        self.assertEqual(self._groups(), [])

    def test_different_body_not_flagged(self):
        self._write('a.py', 'def f(p):\n    return p.read_text()\n')
        self._write('b.py', 'def g(p):\n    return p.read_bytes()\n')
        self.assertEqual(self._groups(), [])

    def test_multiway_duplicate_grouped_once(self):
        body = 'def {0}(p):\n    return p.strip()\n'
        for name, fn in (('a.py', 'x'), ('b.py', 'y'), ('c.py', 'z')):
            self._write(name, body.format(fn))
        groups = self._groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0], ['a.py.x', 'b.py.y', 'c.py.z'])


@unittest.skipIf(dic is None, "duplicate_impl_census tool not available in snapshot environment")
class IdiomClassificationTests(unittest.TestCase):
    def test_atomic_write_with_fsync_classified(self):
        seg = ("def w(p, d):\n"
               "    fd, n = tempfile.mkstemp()\n"
               "    json.dump(d, s)\n"
               "    os.fsync(s.fileno())\n"
               "    os.replace(n, p)\n")
        self.assertEqual(dic.classify_idiom(seg), 'json_write_atomic_fsync')

    def test_atomic_write_without_fsync_classified(self):
        seg = ("def w(p, d):\n"
               "    t = p.with_name(p.name + '.tmp')\n"
               "    t.write_text(json.dumps(d))\n"
               "    os.replace(t, p)\n")
        self.assertEqual(dic.classify_idiom(seg), 'json_write_atomic')

    def test_plain_write_classified(self):
        seg = "def w():\n    return json.dumps({})\n"
        self.assertEqual(dic.classify_idiom(seg), 'json_write_plain')

    def test_read_classified(self):
        seg = "def r(p):\n    return json.loads(p.read_text())\n"
        self.assertEqual(dic.classify_idiom(seg), 'json_read')

    def test_non_json_function_unclassified(self):
        self.assertEqual(dic.classify_idiom('def f(x):\n    return x + 1\n'), '')


@unittest.skipIf(dic is None, "duplicate_impl_census tool not available in snapshot environment")
class ReadOnlyToolTests(unittest.TestCase):
    def test_tool_source_has_no_destructive_calls(self):
        src = Path(dic.__file__).read_text(encoding='utf-8')
        for token in ('unlink', 'rmtree', 'os.remove', 'os.rmdir', 'write_text(',
                      'write_bytes', 'shutil.move'):
            self.assertNotIn(token, src, '普查工具不得包含破坏性调用：%s' % token)

    def test_report_note_says_classification_is_not_equivalence(self):
        report = dic.census(package=str(Path(dic.__file__).parent.parent / 'agentbus'))
        self.assertIn('逐语句相同', report['note'])
        self.assertIn('等价性', report['note'])

    def test_real_package_yields_provable_group(self):
        # 真实包内应至少存在已知的逐语句重复（attestation.sha256_of == delivery_verify._sha256）
        report = dic.census(package=str(Path(dic.__file__).parent.parent / 'agentbus'))
        flat = {tuple(g['members']) for g in report['exact_duplicate_groups']}
        self.assertIn(('attestation.py.sha256_of', 'delivery_verify.py._sha256'), flat)


if __name__ == '__main__':
    unittest.main()
