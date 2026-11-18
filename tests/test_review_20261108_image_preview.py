"""Artifact images use bounded output-root reads and CSP-compatible data URLs."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agentbus.config import load, save
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9WQAAAAASUVORK5CYII=')

class ImagePreviewRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / 'control'
        self.output = self.root / '输出 空格'
        self.output.mkdir()
        cfg = load(self.workspace, create=True)
        cfg.data['output_root'] = str(self.output)
        self.forest = TaskForest(save(cfg))
        self.forest.seed_leaf({'id': 'leaf', 'title': 'Image output'}, branch_id='work')
        self.bridge = WebBridge(self.workspace)
        self.path = self.output / '图 表.png'
        self.path.write_bytes(PNG)

    def test_png_from_separate_output_root_is_data_url_and_not_text_or_git(self):
        with patch('agentbus.web_bridge.subprocess.run') as run:
            value = self.bridge._read_artifact_diff({'leaf_id': 'leaf', 'path': '图 表.png'})
        self.assertEqual(value['image_src'], 'data:image/png;base64,' + base64.b64encode(PNG).decode('ascii'))
        self.assertTrue(value['is_image'])
        self.assertEqual(value['content'], '')
        run.assert_not_called()

    def test_missing_or_traversing_files_and_unknown_task_are_rejected(self):
        for path in ('missing.png', '../outside.png', str(self.path), '.workbuddy/private.png'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.bridge._read_artifact_diff({'leaf_id': 'leaf', 'path': path})
        with self.assertRaises(ValueError):
            self.bridge._read_artifact_diff({'leaf_id': 'unknown', 'path': '图 表.png'})

    def test_wrong_signature_is_rejected_not_rendered(self):
        self.path.write_bytes(b'<html>Not a PNG</html>')
        with self.assertRaisesRegex(ValueError, '图片'):
            self.bridge._read_artifact_diff({'leaf_id': 'leaf', 'path': '图 表.png'})

    def test_oversized_preview_is_refused_before_loading(self):
        with self.path.open('r+b') as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            self.bridge._read_artifact_diff({'leaf_id': 'leaf', 'path': '图 表.png'})

    def test_svg_remains_escaped_text_and_cannot_load_external_resources(self):
        (self.output / 'diagram.svg').write_text('<svg><script>bad()</script></svg>', encoding='utf-8')
        value = self.bridge._read_artifact_diff({'leaf_id': 'leaf', 'path': 'diagram.svg'})
        self.assertFalse(value['is_image'])
        self.assertIn('<script>', value['content'])
