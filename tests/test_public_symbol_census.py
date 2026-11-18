"""§7.36 公开符号普查工具的契约测试。

守卫两件核心纪律：
  1. 判定"已接线"必须有真实引用证据，四种通道都能被识别；
  2. **静态未引用 ≠ 可删除**——工具只输出候选，绝不删任何东西。
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
    from tools import public_symbol_census as psc
except ImportError:
    psc = None


@unittest.skipIf(psc is None, "public_symbol_census tool not available in snapshot environment")
class DetectionChannelTests(unittest.TestCase):
    """四种引用通道必须都能把符号判为"已接线"。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pkg = self.root / 'pkg'
        self.pkg.mkdir()
        (self.pkg / '__init__.py').write_text('', encoding='utf-8')

    def _write(self, name, text):
        (self.root / name).write_text(textwrap.dedent(text), encoding='utf-8')

    def _census(self):
        return psc.census(package=str(self.pkg), roots=(str(self.pkg), str(self.root)))

    def _unwired_names(self):
        return {u['name'] for u in self._census()['unwired']}

    def test_plain_call_counts_as_wired(self):
        (self.pkg / 'm.py').write_text('def alpha():\n    return 1\n', encoding='utf-8')
        self._write('user.py', 'from pkg import m\nm.alpha()\n')
        self.assertNotIn('alpha', self._unwired_names())

    def test_attribute_call_counts_as_wired(self):
        (self.pkg / 'm.py').write_text('def beta():\n    return 1\n', encoding='utf-8')
        self._write('user.py', 'import pkg.m as m\nm.beta()\n')
        self.assertNotIn('beta', self._unwired_names())

    def test_keyword_registration_counts_as_wired(self):
        (self.pkg / 'm.py').write_text('def gamma():\n    return 1\n', encoding='utf-8')
        self._write('reg.py', 'from pkg.m import gamma\nhandlers = []\nhandlers.append(dict(func=gamma))\n')
        self.assertNotIn('gamma', self._unwired_names())

    def test_ternary_keyword_registration_counts_as_wired(self):
        # 形如 set_defaults(func=cmd_a if cmd == 'verify' else cmd_b)
        (self.pkg / 'm.py').write_text(
            'def cmd_a():\n    return 1\n\ndef cmd_b():\n    return 2\n', encoding='utf-8')
        self._write('reg.py',
                    'from pkg.m import cmd_a, cmd_b\n'
                    'def _register(**kw):\n    return kw\n'
                    "cfg = _register(func=cmd_a if 1 else cmd_b)\n")
        names = self._unwired_names()
        self.assertNotIn('cmd_a', names)
        self.assertNotIn('cmd_b', names)

    def test_decorator_counts_as_wired(self):
        (self.pkg / 'm.py').write_text('def delta(f):\n    return f\n', encoding='utf-8')
        self._write('use.py', 'from pkg.m import delta\n\n@delta\ndef target():\n    pass\n')
        self.assertNotIn('delta', self._unwired_names())

    def test_string_registration_counts_as_wired(self):
        (self.pkg / 'm.py').write_text('def epsilon():\n    return 1\n', encoding='utf-8')
        self._write('table.py', 'DISPATCH = {"epsilon": None}\n')
        self.assertNotIn('epsilon', self._unwired_names())

    def test_zero_reference_is_reported_unwired(self):
        (self.pkg / 'm.py').write_text(
            'def orphan():\n    return 1\n\ndef used():\n    return 2\n', encoding='utf-8')
        self._write('user.py', 'from pkg import m\nm.used()\n')
        self.assertIn('orphan', self._unwired_names())
        self.assertNotIn('used', self._unwired_names())

    def test_private_names_are_excluded(self):
        (self.pkg / 'm.py').write_text(
            'def _hidden():\n    return 1\n\ndef visible():\n    return 2\n', encoding='utf-8')
        report = self._census()
        all_names = {u['name'] for u in report['unwired']}
        self.assertNotIn('_hidden', all_names)


@unittest.skipIf(psc is None, "public_symbol_census tool not available in snapshot environment")
class ReadOnlyToolTests(unittest.TestCase):
    """普查工具本身必须只读，且绝不删除任何东西。"""

    def test_tool_source_has_no_destructive_calls(self):
        src = Path(psc.__file__).read_text(encoding='utf-8')
        for token in ('unlink', 'rmtree', 'os.remove', 'os.rmdir', 'write_text',
                      'write_bytes', 'shutil.move'):
            self.assertNotIn(token, src, '普查工具不得包含破坏性调用：%s' % token)

    def test_report_carries_the_not_deletable_note(self):
        report = psc.census(package=str(Path(psc.__file__).parent.parent / 'agentbus'))
        self.assertIn('不等于可删除', report['note'])

    def test_schema_and_counts_are_consistent(self):
        report = psc.census(package=str(Path(psc.__file__).parent.parent / 'agentbus'))
        self.assertEqual(report['wired_count'] + report['unwired_count'],
                         report['public_symbols'])
        self.assertEqual(report['schema_version'], 1)


if __name__ == '__main__':
    unittest.main()
