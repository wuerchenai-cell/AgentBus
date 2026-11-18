"""Source: workbuddy, 2026-10-11. §7.19 界面不得被测试反向塑形（DRY 债守门）。

历史问题：index.html 曾有一整块 sr-only「DOM 兼容辅助容器：保证已有测试选择器
与状态引用 100% 畅通」，测试断言对着隐藏死节点，界面结构被测试反向绑架。

本测试锁定整改后的契约：
1. 不得再出现「测试兼容」性质的 sr-only 容器与对应注释；
2. 统计/标题类状态的**真源**是可见元素上的 data-*（`#forestViewport` / `#progressCapsule`）；
3. 隐藏区只允许保留可访问性真正需要的实时文本节点；
4. `#flowSteps` 等交互面必须落在**可见**的 `<details>` 内。

只读断言，不改文件，不跑模型，不启浏览器。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'agentbus' / 'web_assets'
HTML = ASSETS / 'index.html'
APP_JS = ASSETS / 'app.js'

# 曾为测试保留、现已迁出的隐藏节点（不得再以裸 id 形式出现在 sr-only 块里）。
RETIRED_HIDDEN_IDS = {
    'deliveredCount', 'filterTotal', 'headerProjectName',
    'forestTitle', 'workingCount', 'claimableCount',
}

# 隐藏区只允许保留这些可访问性节点。
ALLOWED_HIDDEN_IDS = {'forestSubtitle', 'assignmentMode', 'assignmentNote', 'sourceLabel'}


def _sr_only_block() -> str:
    text = HTML.read_text(encoding='utf-8')
    start = text.index('<div class="sr-only" aria-hidden="true">')
    depth, i = 0, start
    # 朴素配对：从 sr-only 容器起点找到其闭合 </div>
    end = text.index('</div>', text.index('<div class="forest-toolbar">', start))
    return text[start:end + len('</div>')]


class NoTestShapedCompatibilityLayerTests(unittest.TestCase):
    def setUp(self):
        self.html = HTML.read_text(encoding='utf-8')
        self.js = APP_JS.read_text(encoding='utf-8')

    def test_no_test_compatibility_comment_remains(self):
        self.assertNotIn('DOM 兼容辅助容器', self.html, '仍残留“DOM 兼容辅助容器”注释')
        self.assertNotIn('保证已有测试选择器', self.html, '仍残留“为测试保留”的说明')

    def test_retired_hidden_ids_are_gone_from_the_hidden_block(self):
        block = _sr_only_block()
        for node in RETIRED_HIDDEN_IDS:
            with self.subTest(node=node):
                self.assertNotRegex(
                    block, rf'id="{node}"',
                    f'隐藏区不应再有测试遗留节点 #{node}')

    def test_hidden_block_only_keeps_accessibility_nodes(self):
        block = _sr_only_block()
        ids = set(re.findall(r'id="([^"]+)"', block))
        unexpected = ids - ALLOWED_HIDDEN_IDS
        self.assertEqual(
            unexpected, set(),
            f'隐藏区出现了非可访问性节点：{sorted(unexpected)}')

    def test_stats_have_a_visible_data_source_of_truth(self):
        # 统计真源必须是可见元素上的 data-*，而非隐藏节点文本。
        for attr in ('dataset.deliveredCount', 'dataset.totalCount',
                     'dataset.workingCount', 'dataset.claimableCount'):
            with self.subTest(attr=attr):
                self.assertIn(attr, self.js, f'缺少可见状态镜像：{attr}')

    def test_visible_viewport_and_capsule_carry_the_mirror(self):
        self.assertIn('id="forestViewport"', self.html)
        self.assertIn('id="progressCapsule"', self.html)
        # app.js 应把镜像写到这两个可见元素上。
        self.assertRegex(self.js, r"\$\('forestViewport'\)", '未把镜像写到可见画布')
        self.assertRegex(self.js, r"\$\('progressCapsule'\)", '未把镜像写到可见进度胶囊')

    def test_flow_steps_lives_inside_a_visible_details(self):
        # #flowSteps 必须位于可见的 <details class="project-flow"> 内。
        details_start = self.html.index('<details class="project-flow" id="projectFlow">')
        details_end = self.html.index('</details>', details_start)
        self.assertIn('id="flowSteps"', self.html[details_start:details_end],
                      '#flowSteps 不在可见的协同流程面板内')

    def test_flow_steps_appears_exactly_once(self):
        self.assertEqual(self.html.count('id="flowSteps"'), 1)
        self.assertEqual(self.html.count('id="projectFlow"'), 1)

    def test_no_duplicate_ids_in_document(self):
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dupes, [], f'文档存在重复 id：{dupes}')


if __name__ == '__main__':
    unittest.main()
