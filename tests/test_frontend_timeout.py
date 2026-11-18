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


class FrontendTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.js = (ASSETS / "app.js").read_text(encoding="utf-8")

    def test_default_call_timeout_declared(self):
        """测试 app.js 正确声明了 30 秒默认调用超时常量 DEFAULT_CALL_TIMEOUT_MS。"""
        self.assertIn("DEFAULT_CALL_TIMEOUT_MS = 30000", self.js)
        self.assertIn("let lastCallTimeout = false", self.js)

    def test_call_implements_promise_race_circuit_breaker(self):
        """测试 call() 函数采用 Promise.race 熔断机制防止 pywebview 调用无限挂起。"""
        self.assertIn("Promise.race([", self.js)
        self.assertIn("window.pywebview.api.call(action, payload)", self.js)
        self.assertIn("timeoutPromise", self.js)
        self.assertIn("lastCallTimeout = true", self.js)
        self.assertIn("err.code = 'TIMEOUT'", self.js)

    def test_state_recovery_on_timeout(self):
        """测试调用超时后，自动重置 refreshInFlight 状态并清除刷新按钮 processing 样式。"""
        self.assertIn("if (error && error.code === 'TIMEOUT')", self.js)
        self.assertIn("refreshInFlight = false", self.js)
        self.assertIn("classList.remove('processing')", self.js)

    def test_extended_timeout_for_tree_import_export(self):
        """测试针对任务树导入导出重量级操作赋予了 60 秒宽松超时窗口。"""
        self.assertIn("action === 'export_tree' || action === 'import_tree' ? 60000 : DEFAULT_CALL_TIMEOUT_MS", self.js)

    def test_inspect_and_ui_exports_timeout_info(self):
        """测试 inspect() 状态树与 window.AgentBusUI 成功暴露调用超时诊断指标。"""
        self.assertIn("callTimeoutMs:DEFAULT_CALL_TIMEOUT_MS", self.js)
        self.assertIn("lastCallTimeout", self.js)
        self.assertIn("callTimeoutMs:DEFAULT_CALL_TIMEOUT_MS", self.js)


if __name__ == "__main__":
    unittest.main()
