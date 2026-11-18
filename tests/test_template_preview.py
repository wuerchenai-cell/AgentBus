"""Source: workbuddy, 2026-10-11. §7.14 模板终端预览 tree templates --preview。

约定：
- `--preview` 是**只读**分支，不得导入、不得改任务树、不得建消息；
- 不加 `--preview` 时输出**必须**仍是原来的 JSON（向后兼容）；
- 预览必须包含层级缩进、标题与节点计数；未知模板要给出可用清单而非静默。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agentbus.cli import main
from agentbus.config import load
from agentbus.task_tree import TaskForest
from agentbus.tree_templates import builtin_templates, render_template_outline


class RenderOutlineTests(unittest.TestCase):
    def test_renderer_is_deterministic_and_shows_hierarchy(self):
        doc = builtin_templates()[0]['document']
        first = render_template_outline(doc)
        second = render_template_outline(doc)
        self.assertEqual(first, second, '渲染必须确定性且可重复')
        lines = first.splitlines()
        self.assertIn(doc['title'], lines[0])
        self.assertTrue(any(line.startswith('  概览：') for line in lines), '缺少计数概览')
        # 层级缩进：至少存在一个二级（4 空格）与三级（6 空格）节点行
        self.assertTrue(any(line.startswith('    - ') for line in lines), '缺少二级缩进')
        self.assertTrue(any(line.startswith('      - ') for line in lines), '缺少三级缩进')

    def test_renderer_never_emits_raw_ids_or_execution_state(self):
        doc = builtin_templates()[2]['document']  # agile-web-delivery，节点最多
        text = render_template_outline(doc)
        for leak in ('branch-', 'leaf-', 'task_id', 'claim_id', 'job_id'):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text, f'预览泄露内部标识：{leak}')

    def test_renderer_uses_title_override(self):
        doc = builtin_templates()[1]['document']
        text = render_template_outline(doc, title='自定义标题')
        self.assertTrue(text.splitlines()[0].startswith('自定义标题'))


class PreviewCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.cfg = load(self.workspace, create=True)
        self.forest = TaskForest(self.cfg)
        self.forest.replace_structure({
            'schema_version': 1,
            'roots': [{'id': 'project', 'title': '临时项目', 'goal': 'g'}],
            'branches': [], 'leaves': [],
        })

    def command(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(['tree', '--workspace', str(self.workspace), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_preview_is_read_only(self):
        before = self.forest.path.read_bytes()
        ready_before = sorted((self.workspace / '.agentbus/agents').glob('*/ready/*.json'))
        code, out, err = self.command('templates', '--preview')
        self.assertEqual(code, 0, err)
        self.assertIn('概览：', out)
        self.assertEqual(before, self.forest.path.read_bytes(), '预览改写了任务树')
        self.assertEqual(
            ready_before,
            sorted((self.workspace / '.agentbus/agents').glob('*/ready/*.json')),
            '预览产生了新消息',
        )

    def test_preview_without_flag_still_emits_json(self):
        code, out, err = self.command('templates')
        self.assertEqual(code, 0, err)
        payload = json.loads(out)  # 必须仍是可解析 JSON
        self.assertIn('templates', payload)
        self.assertNotIn('概览：', out)

    def test_preview_single_template_filters(self):
        code, out, err = self.command('templates', '--preview', '--template', 'research-project')
        self.assertEqual(code, 0, err)
        self.assertIn('研究与报告', out)
        self.assertNotIn('商业策划与 MVP 闭环', out)

    def test_preview_unknown_template_lists_alternatives(self):
        code, out, err = self.command('templates', '--preview', '--template', 'no-such')
        self.assertNotEqual(code, 0)
        self.assertIn('software-project', err)
        before = self.forest.path.read_bytes()
        self.assertEqual(before, self.forest.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
