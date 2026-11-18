"""Real CLI entry contract for portable, numbered task trees. Codex 2026-10-09."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentbus.cli import main
from agentbus.config import load
from agentbus.task_tree import TaskForest


class TaskFlowCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.cfg = load(self.workspace, create=True)
        self.forest = TaskForest(self.cfg)
        self.forest.replace_structure({
            'schema_version': 1,
            'roots': [{'id': 'project', 'title': '交付可复用项目', 'goal': '实现本地交接'}],
            'branches': [{'id': 'implementation', 'parent_id': 'project', 'title': '实现'}],
            'leaves': [{'id': 'build', 'parent_id': 'implementation', 'title': '输出文件',
                        'goal': '创建 output.txt，内容为 OK。'},
                       {'id': 'check', 'parent_id': 'implementation', 'title': '复核',
                        'goal': '只读检查 output.txt。', 'dependencies': ['build']}],
        })

    def command(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(['tree', '--workspace', str(self.workspace), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_section_lookup_reports_only_one_task(self):
        code, out, err = self.command('show', '--leaf', '1.1')
        self.assertEqual(code, 0, err)
        node = json.loads(out)
        self.assertEqual(node['id'], 'build')
        self.assertEqual(node['section_no'], '1.1')
        self.assertNotIn('leaves', node)

    def test_export_import_preview_does_not_create_messages(self):
        code, out, err = self.command('export', '--file', 'workflow.tree.json')
        self.assertEqual(code, 0, err)
        exported = json.loads((self.workspace / 'workflow.tree.json').read_text(encoding='utf-8'))
        self.assertEqual(exported['format'], 'agentbus-task-tree')
        before = self.forest.path.read_bytes()
        code, out, err = self.command('import', '--file', 'workflow.tree.json')
        self.assertEqual(code, 0, err)
        self.assertIn('preview', json.loads(out))
        self.assertEqual(before, self.forest.path.read_bytes())
        self.assertEqual(list((self.workspace / '.agentbus/agents').glob('*/ready/*.json')), [])

    def test_confirm_import_preserves_previous_bindings(self):
        message = self.forest.dispatch_leaf('build', 'codex')
        before = (self.forest.bus.agents_dir / 'codex/ready' / (message.id + '.json')).read_bytes()
        code, _, err = self.command('export', '--file', 'workflow.tree.json')
        self.assertEqual(code, 0, err)
        code, out, err = self.command('import', '--file', 'workflow.tree.json')
        self.assertEqual(code, 0, err)
        preview = json.loads(out)
        digest = preview['source_sha256']
        self.assertEqual(len(digest), 64)
        code, out, err = self.command('import', '--file', 'workflow.tree.json', '--apply')
        self.assertNotEqual(code, 0)
        self.assertIn('expected-sha256', err)
        code, out, err = self.command('import', '--file', 'workflow.tree.json', '--apply',
                                      '--expected-sha256', digest)
        self.assertEqual(code, 0, err)
        leaves = self.forest.snapshot()['leaves']
        self.assertEqual(len(leaves), 4)
        self.assertEqual(next(n for n in leaves if n['id'] == 'build')['task_id'], message.id)
        self.assertEqual(before, (self.forest.bus.agents_dir / 'codex/ready' / (message.id + '.json')).read_bytes())

    def test_dispatch_by_number_and_prompt_do_not_claim(self):
        code, out, err = self.command('dispatch', '--leaf', '1.1', '--agent', 'codex')
        self.assertEqual(code, 0, err)
        identity = json.loads(out)['id']
        code, out, err = self.command('prompt', '--leaf', '1.1', '--agent', 'codex')
        self.assertEqual(code, 0, err)
        instruction = json.loads(out)['prompt']
        self.assertIn('1.1', instruction)
        self.assertIn(identity, instruction)
        self.assertIn(str(self.workspace), instruction)
        self.assertTrue((self.forest.bus.agents_dir / 'codex/ready' / (identity + '.json')).exists())
        self.assertFalse((self.forest.bus.agents_dir / 'codex/processing' / (identity + '.json')).exists())

    def test_unknown_section_does_not_mutate(self):
        before = self.forest.path.read_bytes()
        code, _, _ = self.command('show', '--leaf', '99.99')
        self.assertNotEqual(code, 0)
        self.assertEqual(before, self.forest.path.read_bytes())

    def test_import_traversal_is_rejected(self):
        code, _, _ = self.command('import', '--file', '../workflow.tree.json', '--apply')
        self.assertNotEqual(code, 0)

    def test_templates_are_enumerated_and_importable(self):
        code, out, err = self.command('templates')
        self.assertEqual(code, 0, err)
        items = json.loads(out)['templates']
        self.assertGreaterEqual(len(items), 2)
        code, out, err = self.command('import', '--template', items[0]['id'])
        self.assertEqual(code, 0, err)
        digest = json.loads(out)['source_sha256']
        code, _, err = self.command('import', '--template', items[0]['id'], '--apply',
                                    '--expected-sha256', digest)
        self.assertEqual(code, 0, err)
        self.assertGreater(len(self.forest.snapshot()['leaves']), 2)

    def test_cli_import_rejects_file_changed_since_reviewed_digest(self):
        self.command('export', '--file', 'workflow.tree.json')
        code, out, err = self.command('import', '--file', 'workflow.tree.json')
        self.assertEqual(code, 0, err)
        digest = json.loads(out)['source_sha256']
        path = self.workspace / 'workflow.tree.json'
        document = json.loads(path.read_text(encoding='utf-8'))
        document['title'] = '内容已被替换'
        path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        before = self.forest.path.read_bytes()
        code, _, err = self.command('import', '--file', 'workflow.tree.json', '--apply',
                                    '--expected-sha256', digest)
        self.assertNotEqual(code, 0)
        self.assertIn('已变化', err)
        self.assertEqual(before, self.forest.path.read_bytes())

    def test_import_execution_only_field_identifies_field_and_no_claim_advice(self):
        self.command('export', '--file', 'workflow.tree.json')
        path = self.workspace / 'workflow.tree.json'
        document = json.loads(path.read_text(encoding='utf-8'))
        before = self.forest.path.read_bytes()
        for field in ('result_to', 'lease'):
            with self.subTest(field=field):
                invalid = copy.deepcopy(document)
                invalid['leaves'][1][field] = 'synthetic execution state'
                path.write_text(json.dumps(invalid, ensure_ascii=False), encoding='utf-8')

                code, _, err = self.command('import', '--file', 'workflow.tree.json')

                self.assertEqual(code, 4)
                self.assertIn('叶子任务第 2 项', err)
                self.assertIn(field, err)
                self.assertIn('重新预览', err)
                self.assertNotIn('重新 claim', err)
                self.assertNotIn('lease list', err)
                self.assertEqual(self.forest.path.read_bytes(), before)

    def test_cli_import_rejects_ambiguous_file_and_builtin_template(self):
        self.command('export', '--file', 'workflow.tree.json')
        code, _, err = self.command('import', '--file', 'workflow.tree.json',
                                    '--template', 'software-project')
        self.assertNotEqual(code, 0)
        self.assertIn('不能同时', err)

    def test_cli_builtin_template_digest_change_has_accurate_error(self):
        from agentbus.tree_templates import builtin_templates
        original = builtin_templates
        old = original()
        changed = copy.deepcopy(old)
        changed[0]['document']['description'] += ' 新内容'
        with patch('agentbus.tree_templates.builtin_templates', side_effect=[old, changed]):
            code, out, err = self.command('import', '--template', old[0]['id'])
            self.assertEqual(code, 0, err)
            digest = json.loads(out)['source_sha256']
            code, _, err = self.command('import', '--template', old[0]['id'], '--apply',
                                        '--expected-sha256', digest)
        self.assertNotEqual(code, 0)
        self.assertIn('内置模板', err)

    def test_export_reports_cleaning_without_silently_losing_task_goal(self):
        structure = json.loads(self.forest.path.read_text(encoding='utf-8'))
        structure['leaves'][0]['goal'] = 'Read C:\\local\\input.txt and retain the implementation goal.'
        self.forest.replace_structure(structure)
        code, out, err = self.command('export', '--file', 'clean.tree.json')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertTrue(result['warnings'])
        self.assertTrue(result['description'])
        document = json.loads((self.workspace / 'clean.tree.json').read_text(encoding='utf-8'))
        self.assertIn('retain the implementation goal', document['leaves'][0]['goal'])


if __name__ == '__main__':
    unittest.main()
