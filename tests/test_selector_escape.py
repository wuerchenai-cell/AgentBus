"""§8.13 选择器拼接健壮性回归。

问题：`app.js` 曾以模板串把 id 拼进属性选择器：
    document.querySelectorAll(`path.dep-wire[data-dep-source="${leafId}"]`)
若 `leafId` 含 `"`、`\\` 或换行，选择器语法即被破坏，`querySelectorAll` 抛
`SyntaxError` —— 一处数据就足以中断整条渲染链。

修法（本回归锁定的不变式）：**不再把值拼进选择器**，改为
    [...document.querySelectorAll('path.dep-wire')].filter(n => n.dataset.depSource === id)
属性比较天然免疫任何字符，也与本文件既有的 `.filter(node => node.dataset.group === id)`
写法一致。

本测试做三件事：
  1. 静态守卫：`app.js` 里不得再出现 `[data-…="${…}"]` 形式的插值属性选择器；
  2. 行为验证：把**发货代码里那几条语句原文取出**，在 node 的桩 DOM 上以**恶意 id**
     （含引号/反斜杠/换行）执行，断言不抛且精确匹配；
  3. 对照：同样输入下，旧式插值写法**确实抛错**——证明这次改动不是无病呻吟。

无 node 时跳过行为验证（静态守卫仍然执行）。
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
import re
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest

_PACKAGE = Path(__file__).resolve().parents[1] / "agentbus"
_APP_JS = _PACKAGE / "web_assets" / "app.js"

# 恶意 id：引号会截断属性选择器，反斜杠是选择器转义符，换行直接非法。
HOSTILE_IDS = ['a"b', "a\\b", "a\nb", 'a"] , .x [data-y="z']


def _node() -> str | None:
    for candidate in ("node", "node.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


NODE = _node()


class StaticGuardTests(unittest.TestCase):
    def test_no_interpolated_attribute_selector(self):
        text = _APP_JS.read_text(encoding="utf-8")
        offenders = re.findall(r'\[data-[a-z-]+="\$\{[^}]+\}"\]', text)
        self.assertEqual([], offenders,
                         "不得把值插进属性选择器；改用 dataset 属性比较。")

    def test_no_template_selector_in_query_calls(self):
        text = _APP_JS.read_text(encoding="utf-8")
        offenders = re.findall(r"querySelector(?:All)?\(`", text)
        self.assertEqual([], offenders, "querySelector 不得接收模板串选择器。")

    def test_file_still_parses_under_node(self):
        if not NODE:
            self.skipTest("node 不可用")
        proc = subprocess.run([NODE, "--check", str(_APP_JS)],
                              capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)


_SELECTOR_EXPR = re.compile(
    r"\[\.\.\.document\.querySelectorAll\('([^']*)'\)\]\.(filter|find)\(([^()]*)\)")

# 桩 DOM：所有假节点都带全套 dataset 字段（值 = 该轮的恶意 id），因此任何
# "dataset.X === 某个 scope 变量" 的表达式都应精确命中 1 个。
# 关键：选择器文本里一旦出现恶意 id，就说明"值被拼进了选择器" -> 抛 SyntaxError。
# 静态选择器（如 [data-edge="trunk"]）不含任何恶意 id，正常通过。
_HARNESS = r"""
'use strict';
const IDS = %(ids)s;
const makeNode = id => ({dataset: {id: id, group: id, groupId: id,
  leafId: id, noteId: id, depSource: id, depTarget: id, member: id}});
const all = () => IDS.map(makeNode);
function candidates(selector) {
  if (IDS.some(v => selector.includes(v))) throw new SyntaxError('invalid selector: ' + selector);
  if (selector.includes('.branch-node')) return all();
  if (selector.includes('.leaf-card')) return all();
  if (selector.includes('.sticky-note')) return all();
  if (selector.includes('.member-item')) return all();
  if (selector.includes('path.dep-wire')) return all();
  if (selector.includes('.forest-lines')) return all();
  return [];
}
const document = {querySelectorAll: sel => candidates(sel),
                  getElementById: () => null};
let threw = null, expressions = %(count)d, mismatched = 0, bad = [];
for (const id of IDS) {
  const leafId = id, cardId = id, groupId = id, group = {id: id}, leaf = {id: id}, sn = {id: id}, m = id;
  const previousFocus = {dataset: {leafId: id}};
  try {
    %(body)s
  } catch (error) { threw = String(error && error.message); break; }
}
console.log(JSON.stringify({threw: threw, expressions: expressions,
                            mismatched: mismatched, bad: bad}));
"""


def _extract_statements() -> list[str]:
    """从发货代码中取出使用 dataset 比较的**完整选择器表达式**（括号平衡、可独立求值）。"""
    text = _APP_JS.read_text(encoding="utf-8")
    return [m.group(0) for m in _SELECTOR_EXPR.finditer(text)]


def _count_expr(expr: str) -> str:
    """filter 返回数组（长度须为 1）；find 返回单节点（须非空）。"""
    if ".find(" in expr:
        return "if ((%s)) { /* hit */ } else { mismatched++; }" % expr
    return "if ((%s).length === 1) { /* hit */ } else { mismatched++; }" % expr


def _match_expr(expr: str) -> str:
    if ".find(" in expr:
        return "if (!(%s)) bad.push(id + ' missing');" % expr
    return "if ((%s).length !== 1) bad.push(id + ' count=' + (%s).length);" % (expr, expr)


def _run_node(script: str, name: str) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / name
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([NODE, str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


class BehaviourTests(unittest.TestCase):
    """在 node 桩 DOM 上执行发货代码的语句，验证恶意 id 不再抛错。"""

    @unittest.skipUnless(NODE, "node 不可用")
    def test_shipped_statements_survive_hostile_ids(self):
        statements = _extract_statements()
        self.assertTrue(statements, "未能从 app.js 提取到 dataset 比较表达式")

        harness = _HARNESS % {
            "ids": json.dumps(HOSTILE_IDS, ensure_ascii=False),
            "body": "\n  ".join(_count_expr(e) for e in statements),
            "count": len(statements),
        }
        payload = _run_node(harness, "harness.js")
        self.assertIsNone(payload["threw"],
                          "恶意 id 下不得抛错：%s" % payload["threw"])
        self.assertEqual(len(statements), payload["expressions"])
        self.assertEqual(0, payload["mismatched"],
                         "每条表达式都应精确命中 1 个节点")

    @unittest.skipUnless(NODE, "node 不可用")
    def test_selector_expr_returns_exact_match_for_hostile_id(self):
        """逐条表达式在各恶意 id 下都必须精确命中 1 个节点。"""
        statements = _extract_statements()
        harness = _HARNESS % {
            "ids": json.dumps(HOSTILE_IDS, ensure_ascii=False),
            "body": "\n  ".join(_match_expr(e) for e in statements),
            "count": len(statements),
        }
        payload = _run_node(harness, "match.js")
        self.assertEqual([], payload.get("bad", []),
                         "恶意 id 下必须精确命中：%s" % payload.get("bad"))

    @unittest.skipUnless(NODE, "node 不可用")
    def test_old_interpolation_would_have_thrown(self):
        """对照：旧式插值写法在同一桩 DOM 下确实抛错（说明改动有意义）。"""
        script = r"""
'use strict';
const IDS = ['a"b'];
const leafId = 'a"b';
let threw = null;
const document = { querySelectorAll: sel => {
  if (IDS.some(id => sel.includes(id))) throw new SyntaxError('invalid selector');
  return [];
} };
try {
  document.querySelectorAll(`path.dep-wire[data-dep-source="${leafId}"]`);
} catch (error) { threw = error.name; }
console.log(JSON.stringify({threw: threw}));
"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.js"
            path.write_text(script, encoding="utf-8")
            proc = subprocess.run([NODE, str(path)], capture_output=True, text=True)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual("SyntaxError", payload["threw"])


if __name__ == "__main__":
    unittest.main()
