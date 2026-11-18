import unittest
import subprocess
import sys
import os
from pathlib import Path


class TestPerfVerificationLayer(unittest.TestCase):
    def setUp(self):
        self.integ_dir = Path(__file__).resolve().parent.parent

    def test_tiered_testing_targeted(self):
        # 1. 定向档位 (秒级)
        proc = subprocess.run([
            sys.executable, "-m", "agentbus.cli", "test", "--tier", "targeted"
        ], cwd=str(self.integ_dir), capture_output=True, text=True, encoding='utf-8',
            env={**os.environ, 'PYTHONUTF8': '1'})
        self.assertEqual(proc.returncode, 0, f"Targeted tests failed: {proc.stderr}")
        self.assertIn('"tier": "targeted"', proc.stdout)
        self.assertIn('"success": true', proc.stdout)

    def test_tiered_testing_contract(self):
        # 2. 契约档位 (分钟级)
        proc = subprocess.run([
            sys.executable, "-m", "agentbus.cli", "test", "--tier", "contract"
        ], cwd=str(self.integ_dir), capture_output=True, text=True, encoding='utf-8',
            env={**os.environ, 'PYTHONUTF8': '1'})
        self.assertEqual(proc.returncode, 0, f"Contract tests failed: {proc.stderr}")
        self.assertIn('"tier": "contract"', proc.stdout)
        self.assertIn('"success": true', proc.stdout)

    def test_check_offline_script_tier_options(self):
        tools_script = self.integ_dir / "tools" / "check_offline.py"
        proc = subprocess.run([
            sys.executable, str(tools_script), "--help"
        ], capture_output=True, text=True, encoding='utf-8',
            env={**os.environ, 'PYTHONUTF8': '1'})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--tier", proc.stdout)
        self.assertIn("targeted", proc.stdout)
        self.assertIn("contract", proc.stdout)
        self.assertIn("full", proc.stdout)


if __name__ == "__main__":
    unittest.main()
