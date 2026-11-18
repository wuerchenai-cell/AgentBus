# -*- coding: utf-8 -*-
"""§10.19 交付自证门禁测试：test_delivery_self_proof.py。

覆盖：
1. 实例 ①②③ 回放精准判红（API 缺失、契约元组断言不随动、空测试）；
2. 无测试声明判红与显式声明放行；
3. 全绿测试正常放行；
4. D4 假阳性防护：测试内部打印含有 ERROR 或 FAILED 字符串但结果 OK，决不误判为红灯；
5. D5 辅助模块过滤：tests/ 目录下的非 test_*.py 辅助脚本不会被误当做单测执行；
6. D3 豁免校验与事实保留：过期豁免或缺少元数据的豁免坚决拦截，有效豁免标记 pass_with_waiver 且保留客观失败记录。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "tools" / "delivery_self_proof.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("delivery_self_proof", _TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


dsp = _load_tool()


class DeliverySelfProofTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmppath = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_case_1_api_missing_attribute_error_must_fail(self):
        """回放实例 ①：测试针对不存在的 API (AttributeError)，门禁必须判红并指名错误。"""
        code = """
import unittest

class FakeEngine:
    pass

class TestApiMissing(unittest.TestCase):
    def test_spawn(self):
        engine = FakeEngine()
        engine.auto_spawn_ready()

if __name__ == '__main__':
    unittest.main()
"""
        test_py = self.tmppath / "test_headless_spawn_replay.py"
        test_py.write_text(code, encoding="utf-8")

        delivery = {
            "artifacts": [{"path": str(test_py)}],
            "body": "§12.2 交付"
        }
        report = dsp.verify_delivery_self_proof(delivery, self.tmppath)
        self.assertEqual(report["verdict"], "fail")
        self.assertFalse(report["results"][0]["passed"])
        self.assertIn("AttributeError", report["results"][0]["raw_output"])
        self.assertIn("auto_spawn_ready", report["results"][0]["raw_output"])

    def test_case_2_contract_tuple_mismatch_assertion_error_must_fail(self):
        """回放实例 ②：返回值随动为元组而测试仍断言 bool，门禁必须判红。"""
        code = """
import unittest

def do_wakeup():
    return (True, "桌面通知已唤醒")

class TestDesktopWakeup(unittest.TestCase):
    def test_wakeup_returns_bool(self):
        res = do_wakeup()
        self.assertIsInstance(res, bool)

if __name__ == '__main__':
    unittest.main()
"""
        test_py = self.tmppath / "test_desktop_wakeup_replay.py"
        test_py.write_text(code, encoding="utf-8")

        delivery = {
            "artifacts": [{"path": str(test_py)}],
            "body": "§12.3 交付"
        }
        report = dsp.verify_delivery_self_proof(delivery, self.tmppath)
        self.assertEqual(report["verdict"], "fail")
        self.assertFalse(report["results"][0]["passed"])
        self.assertIn("AssertionError", report["results"][0]["raw_output"])

    def test_case_3_ran_zero_tests_must_fail(self):
        """回放实例 ③：未执行任何测试 (Ran 0 tests)，门禁必须判红。"""
        code = """
# 空测试文件，没有继承 unittest.TestCase
"""
        test_py = self.tmppath / "test_empty_zero.py"
        test_py.write_text(code, encoding="utf-8")

        delivery = {
            "artifacts": [{"path": str(test_py)}],
            "body": "§13.2 交付"
        }
        report = dsp.verify_delivery_self_proof(delivery, self.tmppath)
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["results"][0]["ran_count"], 0)

    def test_no_test_artifacts_without_declaration_must_fail(self):
        """交付物不含测试且未显式注明『本交付不附带可执行测试』必须判红。"""
        delivery = {
            "artifacts": [{"path": "docs/README.md"}],
            "body": "仅包含文档"
        }
        report = dsp.verify_delivery_self_proof(delivery, self.tmppath)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn("未在说明中显式注明", report["reasons"][0])

    def test_no_test_artifacts_with_explicit_declaration_passes(self):
        """交付物不含测试，但显式声明『本交付不附带可执行测试』，门禁放行。"""
        delivery = {
            "artifacts": [{"path": "docs/README.md"}],
            "body": "本交付纯属架构文档；本交付不附带可执行测试"
        }
        report = dsp.verify_delivery_self_proof(delivery, self.tmppath)
        self.assertEqual(report["verdict"], "pass")
        self.assertTrue(report["no_tests"])

    def test_clean_green_test_passes(self):
        """全绿测试模块正常通过。"""
        code = """
import unittest

class CleanTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(1 + 1, 2)

if __name__ == '__main__':
    unittest.main()
"""
        test_py = self.tmppath / "test_clean_success.py"
        test_py.write_text(code, encoding="utf-8")

        delivery = {
            "artifacts": [{"path": str(test_py)}],
            "body": "正常交付"
        }
        report = dsp.verify_delivery_self_proof(delivery, self.tmppath)
        self.assertEqual(report["verdict"], "pass")
        self.assertTrue(report["results"][0]["passed"])
        self.assertEqual(report["results"][0]["ran_count"], 1)

    def test_print_error_or_fail_string_in_green_test_no_false_positive(self):
        """D4 防护：单测内部打印含有 ERROR 或 FAILED 字符串但 unittest 结果 OK，决不误判为红灯。"""
        code = """
import unittest

class PrintNoiseTest(unittest.TestCase):
    def test_with_print(self):
        print("self-check: no ERROR found, 0 FAILED")
        self.assertEqual(10 * 10, 100)

if __name__ == '__main__':
    unittest.main()
"""
        test_py = self.tmppath / "test_print_noise.py"
        test_py.write_text(code, encoding="utf-8")

        delivery = {
            "artifacts": [{"path": str(test_py)}],
            "body": "包含日志输出的单测"
        }
        report = dsp.verify_delivery_self_proof(delivery, self.tmppath)
        self.assertEqual(report["verdict"], "pass", f"存在假阳性拦截: {report['reasons']}")
        self.assertTrue(report["results"][0]["passed"])
        self.assertEqual(report["results"][0]["ran_count"], 1)

    def test_helper_files_in_tests_dir_not_treated_as_tests(self):
        """D5 防护：tests/ 目录下的非 test_*.py 辅助脚本不会被提取为单测执行。"""
        artifacts = [
            {"path": "tests/project_paths.py"},
            {"path": "tests/receipt_helpers.py"},
            {"path": "tests/test_real_suite.py"},
            {"path": "agentbus/bus.py"}
        ]
        extracted = dsp.extract_test_artifacts(artifacts)
        self.assertEqual(extracted, ["tests/test_real_suite.py"])

    def test_expired_or_invalid_waiver_must_be_rejected(self):
        """D3 防护：过期豁免、空 approver 或缺少字段的豁免必须被驳回并判红。"""
        code = """
import unittest
class FailTest(unittest.TestCase):
    def test_must_fail(self):
        self.assertTrue(False)
if __name__ == '__main__':
    unittest.main()
"""
        test_py = self.tmppath / "test_must_fail_invalid_waiver.py"
        test_py.write_text(code, encoding="utf-8")
        rel_path = str(test_py).replace("\\", "/")

        # 1. 尝试 1970 年已过期豁免
        expired_exemptions = {
            rel_path: {
                "approver": "Auditor",
                "reason": "已过期豁免",
                "expires_at": "1970-01-01T00:00:00Z"
            }
        }
        delivery = {"artifacts": [{"path": rel_path}], "body": "过期豁免交付"}
        report = dsp.verify_delivery_self_proof(delivery, self.tmppath, exemptions=expired_exemptions)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn("已过期", report["results"][0].get("waiver_rejection", ""))

        # 2. 尝试缺少 approver 的豁免
        bad_meta_exemptions = {
            rel_path: {
                "approver": "",
                "reason": "无责任人",
                "expires_at": "2099-01-01T00:00:00Z"
            }
        }
        report2 = dsp.verify_delivery_self_proof(delivery, self.tmppath, exemptions=bad_meta_exemptions)
        self.assertEqual(report2["verdict"], "fail")
        self.assertIn("缺少批准人", report2["results"][0].get("waiver_rejection", ""))

    def test_valid_waiver_verdict_is_pass_with_waiver_and_preserves_fact(self):
        """D3 防护：有效豁免通过时，verdict 标记为 pass_with_waiver，客观测试记录保留 passed=False。"""
        code = """
import unittest
class FailTest(unittest.TestCase):
    def test_must_fail(self):
        self.assertTrue(False)
if __name__ == '__main__':
    unittest.main()
"""
        test_py = self.tmppath / "test_failed_valid_waiver.py"
        test_py.write_text(code, encoding="utf-8")
        rel_path = str(test_py).replace("\\", "/")

        valid_exemptions = {
            rel_path: {
                "approver": "TechOwner",
                "reason": "底层网络不可控，特许放行",
                "expires_at": "2099-12-31T23:59:59Z"
            }
        }
        delivery = {"artifacts": [{"path": rel_path}], "body": "有效豁免交付"}
        report = dsp.verify_delivery_self_proof(delivery, self.tmppath, exemptions=valid_exemptions)
        self.assertEqual(report["verdict"], "pass_with_waiver")
        self.assertTrue(report["has_waived_items"])
        self.assertFalse(report["results"][0]["passed"])
        self.assertTrue(report["results"][0]["waived"])
        self.assertEqual(report["results"][0]["waiver_details"]["approver"], "TechOwner")


if __name__ == "__main__":
    unittest.main()
