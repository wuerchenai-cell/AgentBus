import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

class BackgroundThreadGateTests(unittest.TestCase):
    def run_case(self, name, source):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("offline_gate_subject", root / "tools/check_offline.py")
        api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
        previous = Path.cwd(); hook = threading.excepthook
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); (base / "tests").mkdir()
            (base / "tests" / (name + ".py")).write_text(source, encoding="utf-8")
            output, error = io.StringIO(), io.StringIO()
            try:
                with patch.object(api, "ROOT", base), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    code = api.main(["--pattern", name + ".py"])
            finally:
                os.chdir(previous)
        self.assertIs(threading.excepthook, hook)
        return code, json.loads(output.getvalue().strip().splitlines()[-1])

    def test_unhandled_thread_failure_is_not_a_green_run(self):
        code, report = self.run_case("test_synthetic_background_failure",
            "import unittest,threading\nclass Case(unittest.TestCase):\n def test_background(self):\n  def fail(): raise UnicodeDecodeError('utf-8',b'\\xb4',0,1,'bad native output')\n  t=threading.Thread(target=fail);t.start();t.join()\n")
        self.assertEqual(code, 1)
        self.assertFalse(report["success"])
        self.assertEqual(report["unhandled_thread_exceptions"], 1)
        self.assertEqual(report["thread_exception_details"][0]["exception"], "UnicodeDecodeError")
        self.assertIn("test_background", report["thread_exception_details"][0]["test_active_at_exception"])

    def test_normal_background_completion_remains_green(self):
        code, report = self.run_case("test_synthetic_background_success",
            "import unittest,threading\nclass Case(unittest.TestCase):\n def test_background(self):\n  t=threading.Thread(target=lambda:None);t.start();t.join()\n")
        self.assertEqual(code, 0)
        self.assertTrue(report["success"])
        self.assertEqual(report["unhandled_thread_exceptions"], 0)

    def test_child_python_inherits_utf8_mode(self):
        with patch.dict(os.environ):
            os.environ.pop("PYTHONUTF8", None)
            code, report = self.run_case(
                "test_synthetic_child_encoding",
                "import subprocess,sys,unittest\n"
                "class Case(unittest.TestCase):\n"
                " def test_encoding(self):\n"
                "  result=subprocess.run([sys.executable,'-c','import sys;print(sys.flags.utf8_mode)'],"
                "capture_output=True,check=True)\n"
                "  self.assertEqual(result.stdout.strip(),b'1')\n",
            )
        self.assertEqual(code, 0)
        self.assertTrue(report["success"])

    @unittest.skipUnless(os.name == "nt", "Windows temporary directory spelling")
    def test_windows_child_and_parent_use_canonical_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            child = base / "alias"
            child.mkdir()
            with patch.dict(os.environ), patch.object(tempfile, "tempdir", str(child / "..")):
                code, report = self.run_case(
                    "test_synthetic_temp_path",
                    "import os,subprocess,sys,tempfile,unittest\n"
                    "class Case(unittest.TestCase):\n"
                    " def test_temp_path(self):\n"
                    "  result=subprocess.run([sys.executable,'-c','import os,tempfile;"
                    " print(os.environ.get(\"TMPDIR\"));print(tempfile.gettempdir())'],"
                    "capture_output=True,check=True,text=True,encoding='utf-8')\n"
                    f"  expected={str(base)!r}\n"
                    "  self.assertEqual(result.stdout.splitlines(), [expected, expected])\n"
                    "  self.assertEqual(tempfile.gettempdir(), expected)\n",
                )
        self.assertEqual(code, 0)
        self.assertTrue(report["success"])

    @unittest.skipUnless(os.name == "nt", "Windows subprocess pipe-reader regression")
    def test_invalid_subprocess_bytes_are_not_reported_as_success(self):
        code, report = self.run_case("test_synthetic_native_decode",
            "import unittest,subprocess,sys\nclass Case(unittest.TestCase):\n def test_native(self):\n  p=subprocess.run([sys.executable,'-c',\"import os;os.write(1,bytes([180]))\"],capture_output=True,text=True,encoding='utf-8',timeout=10)\n  self.assertEqual(p.returncode,0)\n")
        self.assertEqual(code, 1)
        self.assertFalse(report["success"])
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["unhandled_thread_exceptions"], 1)

    def test_nested_failure_does_not_contaminate_outer_runner(self):
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location("outer_gate_subject", root / "tools/check_offline.py")
        outer=importlib.util.module_from_spec(spec);spec.loader.exec_module(outer)
        with outer.background_thread_failures() as failures:
            code, report=self.run_case("test_synthetic_nested_failure",
                "import unittest,threading\nclass Case(unittest.TestCase):\n def test_inner(self):\n  def fail(): raise RuntimeError('expected inner failure')\n  t=threading.Thread(target=fail);t.start();t.join()\n")
        self.assertEqual(code,1)
        self.assertEqual(report["unhandled_thread_exceptions"],1)
        self.assertEqual(failures,[])
