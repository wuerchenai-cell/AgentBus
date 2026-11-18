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


class RefreshPreserveEditTests(unittest.TestCase):
    def setUp(self):
        self.js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.html = (ASSETS / "index.html").read_text(encoding="utf-8")
        self.css = (ASSETS / "app.css").read_text(encoding="utf-8")

    def test_refresh_guards_active_interactions(self):
        """测试 refresh() 包含针对输入、拖拽及弹窗的全量防打断守卫。"""
        self.assertIn("const isEditingText", self.js)
        self.assertIn("const isInteracting", self.js)
        self.assertIn("cardDrag?.active", self.js)
        self.assertIn("branchDrag?.active", self.js)
        self.assertIn("wireDrag?.active", self.js)
        self.assertIn("stickyDrag?.active", self.js)
        self.assertIn("if(isInteracting) return;", self.js)

    def test_render_forest_implements_dirty_check(self):
        """测试 renderForest 包含 forestRenderKey 脏检查，无实质变更时不粗暴销毁活动 DOM。"""
        self.assertIn("let forestRenderKey = '';", self.js)
        self.assertIn("function renderForest(force = false)", self.js)
        self.assertIn("currentForestKey === forestRenderKey", self.js)

    def test_inplace_create_mounted_to_stage(self):
        """测试就地建卡气泡挂载在 forestStage 上，避免被 nodes.replaceChildren 清除。"""
        self.assertIn("$('forestStage').appendChild(pop)", self.js)
        self.assertIn("pop.style.zIndex", self.js)

    def test_branch_node_header_layout_prevents_overlap(self):
        """测试分支节点采用 branch-header 弹性排版与 branch-badge，杜绝数字与标题重叠。"""
        self.assertIn(".branch-header", self.css)
        self.assertIn(".branch-badge", self.css)
        self.assertIn(".branch-title", self.css)
        self.assertIn("bHeader.appendChild", self.js)
        self.assertIn("branch-badge", self.js)

    def test_canvas_panning_excludes_interactive_nodes(self):
        """测试画布平移 pointerdown 拦截排除列表包含分支节点与各类卡片，防止拖拽被劫持。"""
        self.assertIn(".branch-node", self.js)
        self.assertIn(".leaf-card", self.js)
        self.assertIn(".forest-node", self.js)
        self.assertIn(".canvas-sticky-note", self.js)

    def test_wiring_implements_smart_candidate_detection(self):
        """测试连线交互包含 wouldCreateCycle 拓扑环路检测，并清晰区分合法高亮与非法灰度目标。"""
        self.assertIn("function wouldCreateCycle", self.js)
        self.assertIn("is-wire-target-candidate", self.js)
        self.assertIn("is-wire-invalid-candidate", self.js)
        self.assertIn(".is-wire-invalid-candidate", self.css)

    def test_quick_add_task_button_mounted_and_bound(self):
        """测试画布控制栏挂载了快捷加任务按钮并在前端进行了事件绑定。"""
        self.assertIn('id="quickAddTaskBtn"', self.html)
        self.assertIn("quickAddTaskBtn", self.js)
        self.assertIn(".quick-add-btn", self.css)


if __name__ == "__main__":
    unittest.main()
