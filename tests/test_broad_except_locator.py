# -*- coding: utf-8 -*-
"""§10.20 宽泛异常定点定位与基线差分工具测试：test_broad_except_locator.py。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "tools" / "broad_except_locator.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("broad_except_locator", _TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bel = _load_tool()


class BroadExceptLocatorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmppath = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_extract_various_except_handlers(self):
        code = """
try:
    pass
except Exception:
    pass
except OSError as e:
    pass
except (ValueError, TypeError):
    pass
except (IOError, BaseException):
    pass
except:
    pass
"""
        test_py = self.tmppath / "test_sample.py"
        test_py.write_text(code, encoding="utf-8")

        findings = bel.scan_file(test_py, self.tmppath)
        exceptions = [f["exc_str"] for f in findings]
        self.assertIn("Exception", exceptions)
        self.assertIn("OSError", exceptions)
        self.assertIn("bare except", exceptions)
        self.assertTrue(any("BaseException" in e for e in exceptions))
        self.assertFalse(any(e == "ValueError, TypeError" for e in exceptions))
        self.assertEqual(len(findings), 4)

    def test_diff_with_baseline_added_and_removed(self):
        baseline = [
            {"file": "a.py", "lineno": 10, "context": "fn1", "kind": "Exception"},
            {"file": "b.py", "lineno": 20, "context": "fn2", "kind": "OSError"},
        ]
        current = [
            {"file": "a.py", "lineno": 10, "context": "fn1", "kind": "Exception"},
            {"file": "c.py", "lineno": 30, "context": "fn3", "kind": "Exception"},
        ]
        diff = bel.diff_with_baseline(current, baseline)
        self.assertEqual(diff["current_total"], 2)
        self.assertEqual(diff["baseline_total"], 2)
        self.assertEqual(diff["delta"], 0)
        self.assertEqual(diff["added_count"], 1)
        self.assertEqual(diff["removed_count"], 1)
        self.assertEqual(diff["added_details"][0]["file"], "c.py")
        self.assertEqual(diff["removed_details"][0]["file"], "b.py")

    def test_baseline_save_and_load_with_metadata(self):
        baseline_file = self.tmppath / "baseline.json"
        items = [{"file": "m.py", "lineno": 5, "context": "<module>", "kind": "Exception"}]
        meta = bel.save_baseline_snapshot(baseline_file, items, author="codex", reason="初始设立基线")

        self.assertTrue(baseline_file.exists())
        self.assertEqual(meta["author"], "codex")
        self.assertEqual(meta["total"], 1)

        loaded_meta, loaded_items = bel.load_baseline_snapshot(baseline_file)
        self.assertIsNotNone(loaded_meta)
        self.assertEqual(len(loaded_items), 1)
        self.assertEqual(loaded_items[0]["file"], "m.py")
        self.assertEqual(loaded_meta["reason"], "初始设立基线")

    def test_update_baseline_requires_author_and_reason(self):
        baseline_file = self.tmppath / "baseline.json"
        # 缺少 author 与 reason 必须拒绝并返回 2
        ret = bel.main([
            "--target", str(self.tmppath),
            "--baseline", str(baseline_file),
            "--update-baseline"
        ])
        self.assertEqual(ret, 2)
        self.assertFalse(baseline_file.exists())

        # 提供 author 和 reason 正常写入
        ret = bel.main([
            "--target", str(self.tmppath),
            "--baseline", str(baseline_file),
            "--update-baseline",
            "--author", "antigravity",
            "--reason", "测试基线更新"
        ])
        self.assertEqual(ret, 0)
        self.assertTrue(baseline_file.exists())

    def test_save_baseline_also_requires_author_and_reason(self):
        baseline_file = self.tmppath / "save_baseline.json"
        # 缺少 author 与 reason 必须拒绝并返回 2
        ret = bel.main([
            "--target", str(self.tmppath),
            "--baseline", str(baseline_file),
            "--save-baseline"
        ])
        self.assertEqual(ret, 2)
        self.assertFalse(baseline_file.exists())

    def test_check_mode_exit_codes(self):
        sample_py = self.tmppath / "app.py"
        sample_py.write_text("try:\n    pass\nexcept Exception:\n    pass\n", encoding="utf-8")
        baseline_file = self.tmppath / "baseline.json"

        # 首先记录基线 (1条)
        bel.main([
            "--target", str(self.tmppath),
            "--baseline", str(baseline_file),
            "--save-baseline",
            "--author", "tester",
            "--reason", "init baseline"
        ])

        # 没有变化时 --check 返回 0
        ret = bel.main([
            "--target", str(self.tmppath),
            "--baseline", str(baseline_file),
            "--check"
        ])
        self.assertEqual(ret, 0)

        # 增加一条宽泛异常，--check 拦截返回 1
        sample_py.write_text("try:\n    pass\nexcept Exception:\n    pass\nexcept OSError:\n    pass\n", encoding="utf-8")
        ret = bel.main([
            "--target", str(self.tmppath),
            "--baseline", str(baseline_file),
            "--check"
        ])
        self.assertEqual(ret, 1)


if __name__ == "__main__":
    unittest.main()
