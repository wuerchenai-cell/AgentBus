import base64
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from agentbus.bus import Bus
from agentbus.config import load, save
from agentbus.web_bridge import WebBridge


class TestUxExportNotify(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.cfg = load(self.workspace, create=True)
        self.cfg.data['workers']['antigravity']['enabled'] = True
        save(self.cfg)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()
        self.bridge = WebBridge(self.workspace)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_export_canvas_image_png_and_svg(self):
        # 1. 测试 PNG 导出
        sample_png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        b64_png = base64.b64encode(sample_png_bytes).decode("ascii")
        target_png = self.workspace / "test_4k.png"

        res_png = self.bridge.call("export_canvas_image", {
            "format": "png",
            "data_base64": b64_png,
            "path": str(target_png),
            "save": True,
            "width": 3840,
            "height": 2160
        })

        self.assertTrue(res_png["ok"], res_png.get("error"))
        data_png = res_png["data"]
        self.assertEqual(data_png["format"], "png")
        self.assertEqual(data_png["bytes"], len(sample_png_bytes))
        self.assertTrue(Path(data_png["path"]).exists())
        self.assertEqual(Path(data_png["path"]).read_bytes(), sample_png_bytes)

        # 2. 测试 SVG 导出
        sample_svg = "<svg xmlns='http://www.w3.org/2000/svg' width='3840' height='2160'><rect width='100%' height='100%' fill='blue'/></svg>"
        b64_svg = base64.b64encode(sample_svg.encode("utf-8")).decode("ascii")
        target_svg = self.workspace / "test_4k.svg"

        res_svg = self.bridge.call("export_canvas_image", {
            "format": "svg",
            "data_base64": b64_svg,
            "path": str(target_svg),
            "save": True,
            "width": 3840,
            "height": 2160
        })

        self.assertTrue(res_svg["ok"], res_svg.get("error"))
        data_svg = res_svg["data"]
        self.assertEqual(data_svg["format"], "svg")
        self.assertTrue(Path(data_svg["path"]).exists())
        self.assertEqual(Path(data_svg["path"]).read_text(encoding="utf-8"), sample_svg)

    @patch('agentbus.desktop_wakeup.DesktopWakeupBridge.show_toast', return_value=(True, ''))
    def test_show_desktop_notification_offline(self, native_notification):
        res = self.bridge.call("show_desktop_notification", {
            "title": "任务交付完成",
            "message": "§6.6 UX-EXPORT-NOTIFY 验收通过",
            "sound": False,
            "task_id": "m-tree-d2ac53e1438fe3b3949c12ea"
        })

        self.assertTrue(res["ok"], res.get("error"))
        data = res["data"]
        self.assertEqual(data["title"], "任务交付完成")
        self.assertEqual(data["message"], "§6.6 UX-EXPORT-NOTIFY 验收通过")
        self.assertEqual(data["task_id"], "m-tree-d2ac53e1438fe3b3949c12ea")
        self.assertFalse(data["sound_played"])

    def test_web_assets_contain_export_notify_hooks(self):
        assets_dir = Path(__file__).resolve().parent.parent / "agentbus" / "web_assets"
        app_js = (assets_dir / "app.js").read_text(encoding="utf-8")
        app_css = (assets_dir / "app.css").read_text(encoding="utf-8")
        index_html = (assets_dir / "index.html").read_text(encoding="utf-8")

        # 4K 导出
        self.assertIn("exportCanvasImage", app_js)
        self.assertIn("exportCanvasImageButton", index_html)
        self.assertIn("export_canvas_image", app_js)

        # 交付提示音与桌面通知
        self.assertIn("playCrispDeliveryTone", app_js)
        self.assertIn("show_desktop_notification", app_js)
        self.assertIn("triggerDeliveryNotice", app_js)

        # F11 全屏沉浸模式
        self.assertIn("toggleFullscreenCanvas", app_js)
        self.assertIn("fullscreen-mode", app_css)
        self.assertIn("menuFullscreenAction", index_html)

        # 新项目初始化上手向导
        self.assertIn("showQuickstartTour", app_js)
        self.assertIn("quickstart-tour-steps", app_css)
        self.assertIn("menuHelpQuickstart", index_html)


if __name__ == "__main__":
    unittest.main()
