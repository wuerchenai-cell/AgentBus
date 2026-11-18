from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import unittest
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "agentbus" / "web_assets"


class MinimapTests(unittest.TestCase):
    def test_html_contains_minimap_elements(self):
        """测试 index.html 成功挂载鹰眼小地图 DOM 容器与视口指示器结构。"""
        html = (ASSETS / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="canvasMinimap"', html)
        self.assertIn('class="canvas-minimap"', html)
        self.assertIn('id="minimapHeader"', html)
        self.assertIn('id="minimapToggle"', html)
        self.assertIn('id="minimapBody"', html)
        self.assertIn('id="minimapCanvas"', html)
        self.assertIn('id="minimapViewport"', html)
        self.assertIn('width="200"', html)
        self.assertIn('height="130"', html)

    def test_css_contains_minimap_styling(self):
        """测试 app.css 包含小地图玻璃拟态、视口高亮指示框与折叠动效样式。"""
        css = (ASSETS / "app.css").read_text(encoding="utf-8")
        self.assertIn(".canvas-minimap", css)
        self.assertIn(".minimap-header", css)
        self.assertIn(".minimap-toggle", css)
        self.assertIn(".minimap-body", css)
        self.assertIn("#minimapCanvas", css)
        self.assertIn(".minimap-viewport", css)
        self.assertIn(".canvas-minimap.collapsed", css)
        self.assertIn("backdrop-filter", css)

    def test_js_minimap_logic_and_api(self):
        """测试 app.js 包含小地图世界坐标换算、视口计算与对外暴露 API。"""
        js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.assertIn("MINIMAP_WIDTH = 200", js)
        self.assertIn("MINIMAP_HEIGHT = 130", js)
        self.assertIn("function getMinimapWorldBounds()", js)
        self.assertIn("function updateMinimap()", js)
        self.assertIn("function navigateFromMinimap(e)", js)
        self.assertIn("function initMinimap()", js)
        self.assertIn("minimapVisible", js)
        self.assertIn("minimap:{update:updateMinimap", js)

    def test_transform_triggers_minimap(self):
        """测试主画布缩放和平移 transform() 发生时无缝触发小地图视口重绘联动。"""
        js = (ASSETS / "app.js").read_text(encoding="utf-8")
        # 验证 transform 函数体内包含了 updateMinimap 调用
        self.assertIn("function transform()", js)
        transform_block = js.split("function transform()")[1].split("function zoom")[0]
        self.assertIn("updateMinimap()", transform_block)

    def test_minimap_node_color_mapping(self):
        """测试小地图绘制逻辑中覆盖了主干、分支、状态叶子与便签色彩映射。"""
        js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.assertIn("trunk-node", js)
        self.assertIn("branch-node", js)
        self.assertIn("leaf-card", js)
        self.assertIn("#10b981", js)  # delivered 绿
        self.assertIn("#0284c7", js)  # running 蓝
        self.assertIn("#f59e0b", js)  # claimed 橙
        self.assertIn("#ef4444", js)  # blocked 红
        self.assertIn("sticky-note", js)


if __name__ == "__main__":
    unittest.main()
