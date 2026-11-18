"""
Tests for STOP-BUTTON-SCOPE (P1, §7.34)
Source: Antigravity (2026-10-12)

Verifies:
1. STOP button and related markers accurately convey that STOP pauses new dispatches
   while explicitly preserving existing in-flight/processing work.
2. WebBridge `_stop` and `_resume` properly write/remove `.agentbus/STOP` and audit the event
   with existing_processing_preserved=True.
3. In web_assets app.js and index.html, the stop button copy and tooltips correctly state:
   - "暂停新派发" / "恢复派工"
   - Tooltip: "暂停新任务派发；保留在途任务继续执行，不终止外部正在运行的会话"
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus
from agentbus.config import load, save
from agentbus.web_bridge import WebBridge


class TestStopButtonScope(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.cfg = load(self.workspace, create=True)
        self.cfg.data['workers']['codex']['enabled'] = True
        self.cfg.data['workers']['codex']['executor_enabled'] = True
        save(self.cfg)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_web_bridge_stop_and_resume_marker_semantics(self):
        bridge = WebBridge(self.workspace)
        stop_marker = self.workspace / '.agentbus/STOP'

        # 1. Trigger stop
        res_stop = bridge.call('stop', {'caller': 'codex', 'reason': '测试暂停'})
        self.assertTrue(res_stop.get('ok'))
        self.assertTrue(stop_marker.is_file())
        stop_data = json.loads(stop_marker.read_text(encoding='utf-8'))
        self.assertEqual(stop_data['caller'], 'codex')
        self.assertEqual(stop_data['reason'], '测试暂停')

        # Check snapshot reflects stopped
        self.assertTrue(res_stop['data']['forest']['stopped'])

        # Check audit event
        events = self.bus.events()
        stop_events = [e for e in events if e.get('event') == 'stop']
        self.assertTrue(len(stop_events) >= 1)
        self.assertTrue(stop_events[-1].get('existing_processing_preserved'))

        # 2. Trigger resume
        res_resume = bridge.call('resume', {'caller': 'codex'})
        self.assertTrue(res_resume.get('ok'))
        self.assertFalse(stop_marker.exists())
        self.assertFalse(res_resume['data']['forest']['stopped'])

        # Check resume audit event
        events = self.bus.events()
        resume_events = [e for e in events if e.get('event') == 'resume']
        self.assertTrue(len(resume_events) >= 1)
        self.assertTrue(resume_events[-1].get('existing_processing_preserved'))

    def test_frontend_assets_stop_button_wording(self):
        app_js_path = Path(__file__).resolve().parents[1] / 'agentbus/web_assets/app.js'
        index_html_path = Path(__file__).resolve().parents[1] / 'agentbus/web_assets/index.html'

        app_js = app_js_path.read_text(encoding='utf-8')
        index_html = index_html_path.read_text(encoding='utf-8')

        # Assert app.js button text and tooltip explain scope accurately
        self.assertIn("f.stopped ? '恢复派工' : '暂停新派发'", app_js)
        self.assertIn("暂停新任务派发；保留在途任务继续执行，不终止外部正在运行的会话", app_js)
        self.assertNotIn("紧急制动：暂停所有在途派发与心跳流转", app_js)

        # Assert index.html does not claim stopping running processes
        self.assertIn("暂停自动派工（保留在途）", index_html)
        self.assertNotIn("触发全局安全熔断", index_html)


if __name__ == '__main__':
    unittest.main()
