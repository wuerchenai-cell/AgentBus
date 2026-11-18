from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


from pathlib import Path
import unittest

ASSETS = Path(__file__).resolve().parents[1] / "agentbus" / "web_assets"


class BranchVisualRefactorTests(unittest.TestCase):
    def test_css_contains_aesthetic_and_anti_aliasing_rules(self):
        css = (ASSETS / "app.css").read_text(encoding="utf-8")
        self.assertIn(".branch-node{", css)
        self.assertIn(".leaf-card{", css)
        self.assertIn("vector-effect:non-scaling-stroke", css)
        self.assertIn("shape-rendering:geometricPrecision", css)
        self.assertIn("stroke-linecap:round", css)
        self.assertIn("stroke-linejoin:round", css)
        # 验证全套动态小巧思与无极拉伸关键规则
        self.assertIn("ltr-flow-ants", css)
        self.assertIn("ltr-reverse-pulse", css)
        self.assertIn("card-aura", css)
        self.assertIn("path-highlight", css)
        self.assertIn(".app-shell.sidebar-resizing", css)
        # 验证侧栏收起彻底杜绝残影
        self.assertIn(".app-shell.sidebar-collapsed .sidebar", css)
        self.assertIn("visibility:hidden !important", css)
        # 验证动态小机器人物理外观与眨眼微动效
        self.assertIn(".activity-bot", css)
        self.assertIn(".bot-face", css)
        self.assertIn(".bot-antenna", css)
        self.assertIn(".bot-eye", css)
        self.assertIn("antenna-bob", css)
        self.assertIn("eye-blink", css)
        # 验证快捷动作工具栏样式
        self.assertIn(".quick-action-bar", css)
        self.assertIn(".quick-btn", css)
        # 验证折叠按钮现代圆形胶囊样式
        self.assertIn(".branch-collapse-button .fold-icon", css)

    def test_js_renders_smooth_adaptive_cubic_bezier_curves(self):
        js = (ASSETS / "app.js").read_text(encoding="utf-8")
        # 验证根节点到分支的纯水平三次贝塞尔 S 曲线 (LTR 横向自左向右)
        self.assertIn("M ${rootX+rootW} ${rootCenterY} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${branchX} ${cg.centerY}", js)
        # 验证每层分支到叶子的连线按当前父节点与卡片位置自适应计算。
        # 递归任务树不再使用固定的 branchX/leafX 三列坐标。
        self.assertIn("const span=(cardX-(curBranchX+branchW))*0.5;", js)
        self.assertIn("const c1x=curBranchX+branchW+span, c1y=curBranchCenterY;", js)
        self.assertIn("const c2x=cardX-span, c2y=leafCenterY;", js)
        self.assertIn("M ${curBranchX+branchW} ${curBranchCenterY} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${cardX} ${leafCenterY}", js)
        self.assertIn("data-collapsible-edge", js)
        # 验证修复了 visualState 未定义致命白屏 Bug
        self.assertIn("String(visual)+(motion?' '+motion:'')", js)
        self.assertNotIn("String(visualState)", js)
        # 验证折叠按钮使用 SVG 图标而非土味纯文本 [+]
        self.assertIn("fold-icon", js)
        self.assertNotIn("branch-fold-label", js)
        # 验证侧边栏真·无极拉伸 (clientX 绝对映射与 sidebar-resizing)
        self.assertIn("setSidebarWidth(event.clientX,false)", js)
        self.assertIn("classList.add('sidebar-resizing')", js)
        # 验证快捷工具栏事件绑定
        self.assertIn("quickAppendTaskBtn", js)
        self.assertIn("quickDispatchBtn", js)
        self.assertIn("quickRefreshBtn", js)
        self.assertIn("quickFitBtn", js)
        # 验证彻底清除了玩具式一句话目标输入框
        self.assertNotIn("quickGoalInput", js)
        self.assertNotIn("quickGoalSubmit", js)

    def test_html_purified_top_bar(self):
        html = (ASSETS / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("goalComposer", html)
        self.assertNotIn("quickGoalInput", html)
        # 验证顶栏高频快捷动作工具栏
        self.assertIn('id="quickActionBar"', html)
        self.assertIn('id="quickAppendTaskBtn"', html)
        self.assertIn('id="quickDispatchBtn"', html)
        self.assertIn('id="quickRefreshBtn"', html)
        self.assertIn('id="quickFitBtn"', html)


if __name__ == "__main__":
    unittest.main()
