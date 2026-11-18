"""§8.8 构建残留清点工具的契约测试。

守卫三条纪律：
  1. 分类必须**看实际内容**（空/venv/源码副本），不看目录名；
  2. 保留策略只**判定**，绝不删除；
  3. 工具本身**只读**——不含任何删除/移动调用。
"""
import ast
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from tools import build_litter_inventory as bli
except ImportError:
    bli = None


@unittest.skipIf(bli is None, "build_litter_inventory tool not available in snapshot environment")
class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _mk(self, name):
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_empty_dir_classified_empty(self):
        d = self._mk('.build-empty')
        self.assertEqual(bli.classify_dir(d)['kind'], 'empty')

    def test_pyvenv_classified_venv(self):
        d = self._mk('.build-venv-test')
        (d / 'pyvenv.cfg').write_text('home = x\n', encoding='utf-8')
        (d / 'lib').mkdir()
        (d / 'lib' / 'mod.py').write_text('x=1\n', encoding='utf-8')
        self.assertEqual(bli.classify_dir(d)['kind'], 'venv')

    def test_agentbus_package_classified_source_copy(self):
        d = self._mk('.release-build-x')
        (d / 'agentbus').mkdir()
        (d / 'agentbus' / 'bus.py').write_text('x=1\n', encoding='utf-8')
        self.assertEqual(bli.classify_dir(d)['kind'], 'source_copy')

    def test_tests_dir_alone_classified_source_copy(self):
        d = self._mk('.release-build-y')
        (d / 'tests').mkdir()
        (d / 'tests' / 't.py').write_text('x=1\n', encoding='utf-8')
        self.assertEqual(bli.classify_dir(d)['kind'], 'source_copy')

    def test_only_zip_classified_staged_zip(self):
        d = self._mk('.release-build-z')
        (d / 'input.zip').write_bytes(b'PK\x03\x04')
        self.assertEqual(bli.classify_dir(d)['kind'], 'staged_zip')


@unittest.skipIf(bli is None, "build_litter_inventory tool not available in snapshot environment")
class FinderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_finds_prefixed_dirs_only(self):
        (self.root / '.build-a').mkdir()
        (self.root / '.release-build-b').mkdir()
        (self.root / '.web-build-c').mkdir()
        (self.root / 'normal').mkdir()
        (self.root / 'agentbus').mkdir()
        found = sorted(p.name for p in bli.find_litter(self.root))
        self.assertEqual(found, ['.build-a', '.release-build-b', '.web-build-c'])

    def test_does_not_descend_into_litter(self):
        outer = self.root / '.build-outer'
        (outer / '.build-inner').mkdir(parents=True)
        found = [p.name for p in bli.find_litter(self.root)]
        self.assertEqual(found, ['.build-outer'])


@unittest.skipIf(bli is None, "build_litter_inventory tool not available in snapshot environment")
class RetentionVerdictTests(unittest.TestCase):
    def _entry(self, kind, n=1, size=0, path=None):
        return {"path": path or ('x/%s' % kind), "kind": kind,
                "files": n, "bytes": size}

    def test_empty_is_reclaimable_but_requires_confirmation(self):
        entries = [self._entry('empty'), self._entry('venv', size=1000)]
        v = bli.retention_verdict(entries)
        self.assertEqual(v['counts']['empty'], 1)
        self.assertIn('Owner', v['policy']['empty'])
        self.assertIn('确认', v['policy']['empty'])

    def test_keeps_n_source_copies(self):
        entries = [self._entry('source_copy', path='x/a'),
                   self._entry('source_copy', path='x/b'),
                   self._entry('source_copy', path='x/c')]
        v = bli.retention_verdict(entries, keep_source_copies=2)
        self.assertEqual(len(v['source_copies_beyond_keep']), 1)

    def test_note_states_no_deletion(self):
        v = bli.retention_verdict([self._entry('empty')])
        self.assertIn('不删除', v['note'])


@unittest.skipIf(bli is None, "build_litter_inventory tool not available in snapshot environment")
class ReadOnlyToolTests(unittest.TestCase):
    def test_tool_source_has_no_destructive_calls(self):
        src = Path(bli.__file__).read_text(encoding='utf-8')
        for token in ('unlink', 'rmtree', 'os.remove', 'os.rmdir', 'shutil.rmtree',
                      'shutil.move', 'write_text(', 'write_bytes'):
            self.assertNotIn(token, src, '清点工具不得包含破坏性调用：%s' % token)

    def test_inventory_of_empty_root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        report = bli.inventory(Path(temp.name))
        self.assertEqual(report['litter_dirs'], 0)
        self.assertEqual(report['schema_version'], 1)


if __name__ == '__main__':
    unittest.main()
