"""§7.39 静态卫生守卫：交付/裁定文本不得把源码检查与正式程序互相代称。

本守卫检查两件事（只读）：
  1. `evidence_scope.py` 本身保持只读语义——不含写入调用、不读环境变量值、不碰凭据；
  2. 项目内**当前在用的**证据约定文本（本模块 docstring + 已有 attestation 限制声明）
     不出现"正式程序…源码"类代称措辞。

注意边界：本守卫**不**对历史交付文档做全量扫描（历史记录只保留、不改写），
只对"今后新写的证据约定面"设闸。
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import ast
from pathlib import Path
import unittest

from agentbus import evidence_scope as es

HERE = Path(__file__).resolve().parent
PKG = HERE.parent / 'agentbus'
MODULE = PKG / 'evidence_scope.py'


class ModuleIsReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(MODULE.read_text(encoding='utf-8'))

    def test_no_write_calls(self):
        # 明确的文件系统写操作名。
        hard_banned = {'write_text', 'write_bytes', 'mkdir', 'unlink',
                       'rmdir', 'chmod', 'remove', 'rmtree'}
        offenders = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, 'attr', None) or getattr(func, 'id', None)
            if name in hard_banned:
                offenders.append(name)
        self.assertEqual(offenders, [], '证据范围模块必须只读，不得写入文件')

    def test_no_os_or_shutil_replace(self):
        # Path.replace / os.replace / shutil.* 是真正的文件系统改动，显式禁掉。
        src = MODULE.read_text(encoding='utf-8')
        for token in ('os.replace', 'shutil.', 'Path.replace', '.replace(pattern',
                      'os.rename', 'os.remove', 'os.rmdir'):
            self.assertNotIn(token, src, '不得调用文件系统改动 API：%s' % token)

    def test_no_open_for_writing(self):
        offenders = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'open':
                for kw in node.keywords:
                    if kw.arg == 'mode' and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str) \
                            and any(c in kw.value.value for c in 'wax+'):
                        offenders.append(kw.value.value)
        self.assertEqual(offenders, [], '不得以写模式打开文件')

    def test_no_env_value_reads(self):
        # 允许 os.environ 存在但不得读取其值（本模块不读环境变量值）。
        offenders = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                    and node.value.attr == 'environ':
                offenders.append('environ[...]')
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == 'getenv':
                offenders.append('os.getenv')
        self.assertEqual(offenders, [], '不得读取环境变量值（只允许显式传入环境）')

    def test_no_workbuddy_or_credential_access(self):
        src = MODULE.read_text(encoding='utf-8')
        # .workbuddy 只在"排除目录名"这一处出现，属允许；凭据关键字一律不得出现。
        for token in ('credential', 'password', 'token=', 'api_key', 'secret'):
            self.assertNotIn(token, src.lower().replace('.workbuddy', ''),
                             '不得触碰凭据：%s' % token)


class ConventionTextTests(unittest.TestCase):
    def test_module_docstring_states_the_three_subjects(self):
        src = MODULE.read_text(encoding='utf-8')
        self.assertIn('源码检查 ≠ 候选包检查 ≠ 正式 EXE 检查', src)
        for label in ('source_tree', 'candidate_package', 'formal_exe'):
            self.assertIn(label, src, '必须显式列出三种口径：%s' % label)

    def test_module_docstring_itself_has_no_conflation(self):
        # 只审模块 docstring（对外说明的证据约定文本），不审代码里的字段名/标签。
        doc = ast.get_docstring(self._parse()) or ''
        hits = es.detect_conflation(doc)
        self.assertEqual(hits, [], '证据范围模块的说明文字不得出现代称措辞')

    def _parse(self):
        return ast.parse(MODULE.read_text(encoding='utf-8'))

    def test_subject_labels_are_distinct(self):
        labels = list(es.SUBJECT_LABEL.values())
        self.assertEqual(len(labels), len(set(labels)))
        self.assertIn('正式', es.SUBJECT_LABEL[es.SUBJECT_FORMAL_EXE])


if __name__ == '__main__':
    unittest.main()
