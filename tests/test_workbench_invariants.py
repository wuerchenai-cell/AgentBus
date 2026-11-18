"""工作台契约的**语义不变量**层（§8.5 的配套）。

背景：`tests/test_workbench_contract.py` 用"整体源码 sha256 + 方法 AST 摘要"冻结契约。
该做法**合法迭代即假红**——一次纯本地的变量重命名就会让 `_show_permission_dialog`
被判为破坏（本模块的 `test_local_rename_is_not_a_break` 正是这条反例的固化）。

本模块提供**单调不变量**替代方案：冻结每个受保护实体的**符号面**
（它引用的 `self.<操作>` 与模块级名字），并断言

    冻结的符号面 ⊆ 现在的符号面

即：**新增合法，删减即破坏**。这样

  * 局部变量重命名、语句重排、加注释、抽小函数  → 符号面不变 → **绿**；
  * 删掉一次 `self._save_group()` 之类的真实操作调用 → 符号面收缩 → **红**。

数据源：`tests/data/workbench_contract_surface.json`，由
`tools/workbench_contract_regen.py --surface` 生成（再生成属受控流程，须人工审查 diff）。

本模块**只读**：不改被测代码，也不改冻结测试；它是并行的一层，不是替代品——
两者都通过时，才同时满足"合法迭代不红"与"破坏仍被拦"。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import ast
import copy
import json
import re
from pathlib import Path
import unittest

_PACKAGE = Path(__file__).resolve().parents[1] / "agentbus"
_SURFACE_FILE = Path(__file__).resolve().parent / "data" / "workbench_contract_surface.json"


def _load_surface() -> dict:
    return json.loads(_SURFACE_FILE.read_text(encoding="utf-8"))


def _module_level_names(tree):
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                names.add(a.asname or a.name)
    return names


def _local_names(node):
    out = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for a in (node.args.args + node.args.kwonlyargs + node.args.posonlyargs):
            out.add(a.arg)
        if node.args.vararg:
            out.add(node.args.vararg.arg)
        if node.args.kwarg:
            out.add(node.args.kwarg.arg)
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        if isinstance(n, ast.comprehension):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    out.add(t.id)
        if isinstance(n, (ast.For, ast.AsyncFor)):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    out.add(t.id)
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars:
                    for t in ast.walk(item.optional_vars):
                        if isinstance(t, ast.Name):
                            out.add(t.id)
        if isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
    out.discard("self")
    return out


def current_surface(node, module_names) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            text = ast.unparse(n)
            if text.startswith("self."):
                out.add(text)
        elif isinstance(n, ast.Name) and n.id in module_names and not isinstance(n.ctx, ast.Store):
            out.add(n.id)
    locals_ = _local_names(node)
    return {s for s in out if s.split(".")[0] not in locals_}


def _entities(tree):
    launcher = next(n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "Launcher")
    methods = {i.name: i for i in launcher.body
               if isinstance(i, (ast.FunctionDef, ast.AsyncFunctionDef))}
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    return methods, classes, functions


@unittest.skipIf(not _SURFACE_FILE.is_file(), "workbench_contract_surface.json not available in snapshot environment")
class WorkbenchInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (_PACKAGE / "gui.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.module_names = _module_level_names(cls.tree)
        cls.methods, cls.classes, cls.functions = _entities(cls.tree)
        cls.frozen = _load_surface()

    def _missing(self, kind):
        mapping = {"methods": self.methods, "helpers": self.classes,
                   "functions": self.functions}[kind]
        problems = []
        for name, expected in (self.frozen.get(kind) or {}).items():
            node = mapping.get(name)
            if node is None:
                problems.append("%s: 实体已消失" % name)
                continue
            lost = sorted(set(expected) - current_surface(node, self.module_names))
            if lost:
                problems.append("%s: 丢失符号 %s" % (name, lost))
        return problems

    def test_launcher_methods_keep_their_operation_surface(self):
        self.assertEqual([], self._missing("methods"),
                         "受保护方法引用的 self 操作/模块名不得被移除（新增可以）。")

    def test_protected_classes_keep_their_operation_surface(self):
        self.assertEqual([], self._missing("helpers"))

    def test_protected_functions_keep_their_operation_surface(self):
        self.assertEqual([], self._missing("functions"))

    def test_local_rename_is_not_a_break(self):
        """反例固化：纯本地重命名不得被判破坏（这正是冻结 AST 的假红来源）。"""
        method = self.methods["_show_permission_dialog"]
        renamed_src = re.sub(r"(?<![\w.])detail(?![\w])", "detail_text",
                             ast.unparse(method))
        renamed = ast.parse(renamed_src).body[0]
        expected = self.frozen["methods"]["_show_permission_dialog"]
        self.assertFalse(sorted(set(expected) - current_surface(renamed, self.module_names)),
                         "本地重命名不应收缩符号面")

    def test_statement_reorder_is_not_a_break(self):
        """反例固化：语句重排（行为等价）不得收缩符号面。"""
        method = copy.deepcopy(self.methods["_save_group"])
        body = list(method.body)
        # 把最后一条可交换的简单语句提到前面（仅当不影响语义的表达式语句）
        if len(body) > 2:
            body = [body[0]] + body[2:] + [body[1]]
            method.body = body
        expected = self.frozen["methods"]["_save_group"]
        self.assertFalse(sorted(set(expected) - current_surface(method, self.module_names)))

    def test_removing_an_operation_call_is_detected(self):
        """负向控制：删掉一次真实操作调用必须被发现。"""
        method = copy.deepcopy(self.methods["_save_group"])
        before = current_surface(method, self.module_names)
        # 删掉所有 self.* 调用语句
        method.body = [n for n in method.body
                       if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                               and ast.unparse(n.value).startswith("self."))]
        after = current_surface(method, self.module_names)
        self.assertTrue(sorted(before - after),
                        "移除 self 操作调用后符号面必须收缩")

    def test_surface_file_matches_current_gui(self):
        """侧车应与当前 gui.py 同源——否则说明需要受控再生成。"""
        problems = (self._missing("methods") + self._missing("helpers")
                    + self._missing("functions"))
        self.assertEqual([], problems)
        self.assertEqual(1, self.frozen.get("schema_version"))


if __name__ == "__main__":
    unittest.main()
