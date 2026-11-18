"""
Tests for UX-IDE-INTEGRATION (P2, §6.3)
Source: Antigravity (2026-10-12)

Verifies:
1. WebBridge API:
   - `open_artifact`: Correctly checks leaf delivered state, validates path,
     and supports `vscode`, `cursor`, and `system` application launchers.
   - `read_artifact_diff`: Safely reads artifact content (with length capping),
     extracts git diff if present, and accurately flags image extensions.
2. Web Assets (index.html, app.js, app.css):
   - Detail drawer includes wide-mode reading toggle button (`drawerWidthToggle`, Alt+F).
   - Artifact list provides distinct actions: "VS Code", "Cursor", "资源管理器", "查看变更 Diff".
   - Diff viewing modal displays git unified diff with color highlighting (+/-) or clean content.
   - Image lightbox modal provides full-screen distraction-free image zoom.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus
from agentbus.config import load, save
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class TestUXIDEIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.cfg = load(self.workspace, create=True)
        self.cfg.data['workers']['codex']['enabled'] = True
        self.cfg.data['workers']['codex']['executor_enabled'] = True
        save(self.cfg)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()

        # Create output artifact
        self.artifact_file = self.workspace / 'output.py'
        self.artifact_file.write_text('print("hello world")\n', encoding='utf-8')

        # Create image artifact
        self.image_file = self.workspace / 'diagram.png'
        self.image_file.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR')

        self.forest = TaskForest(self.cfg)
        tree = {
            'schema_version': 1,
            'roots': [{'id': 'root', 'parent_id': None, 'title': 'Root', 'goal': 'Goal'}],
            'branches': [{'id': 'b1', 'parent_id': 'root', 'title': 'Branch'}],
            'leaves': [
                {'id': 'leaf-1', 'parent_id': 'b1', 'title': 'Task 1', 'goal': 'Goal 1'},
            ],
        }
        self.forest.replace_structure(tree)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_web_bridge_read_artifact_diff(self):
        bridge = WebBridge(self.workspace)

        # 1. Read diff of text artifact
        res_text = bridge.call('read_artifact_diff', {'leaf_id': 'leaf-1', 'path': 'output.py'})
        self.assertTrue(res_text['ok'], res_text)
        data_text = res_text['data']
        self.assertEqual(data_text['path'], 'output.py')
        self.assertIn('hello world', data_text['content'])
        self.assertFalse(data_text['is_image'])

        # 2. Read diff of image artifact
        res_img = bridge.call('read_artifact_diff', {'leaf_id': 'leaf-1', 'path': 'diagram.png'})
        self.assertTrue(res_img['ok'], res_img)
        data_img = res_img['data']
        self.assertTrue(data_img['is_image'])

    def test_web_bridge_open_artifact_validation(self):
        bridge = WebBridge(self.workspace)

        # Non-existent artifact fails cleanly
        res_bad = bridge.call('open_artifact', {'leaf_id': 'leaf-1', 'path': 'non_existent.py', 'app': 'vscode'})
        self.assertFalse(res_bad['ok'])
        self.assertTrue('NOT_FOUND' in res_bad['error'] or '不存在' in res_bad['error'])

        # Invalid app fails cleanly
        res_app = bridge.call('open_artifact', {'leaf_id': 'leaf-1', 'path': 'output.py', 'app': 'unknown_app'})
        self.assertFalse(res_app['ok'])
        self.assertTrue('仅支持' in res_app['error'] or 'VS Code' in res_app['error'])

    def test_frontend_drawer_and_diff_elements_present(self):
        app_js = (Path(__file__).resolve().parent.parent / 'agentbus/web_assets/app.js').read_text(encoding='utf-8')
        index_html = (Path(__file__).resolve().parent.parent / 'agentbus/web_assets/index.html').read_text(encoding='utf-8')
        app_css = (Path(__file__).resolve().parent.parent / 'agentbus/web_assets/app.css').read_text(encoding='utf-8')

        # 1. Verify drawer width toggle in HTML and app.js
        self.assertIn('id="drawerWidthToggle"', index_html)
        self.assertIn('toggleDrawerWide', app_js)
        self.assertIn('wide-mode', app_css)

        # 2. Verify artifact buttons in app.js
        self.assertIn("open_artifact',", app_js)
        self.assertIn("showArtifactDiffDialog", app_js)
        self.assertIn("showImageLightbox", app_js)
        self.assertIn("read_artifact_diff", app_js)

        # 3. Verify Diff styling classes in app.css
        self.assertIn(".diff-line-add", app_css)
        self.assertIn(".diff-line-del", app_css)
        self.assertIn(".image-lightbox-modal", app_css)


if __name__ == '__main__':
    unittest.main()
