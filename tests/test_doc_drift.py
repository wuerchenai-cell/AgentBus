"""Tests for Document Drift audit and project alignment."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import json
import subprocess
import unittest
from datetime import date

_HERE = Path(__file__).resolve()
ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parent
INTEG = _HERE.parents[1]


class TestDocumentDrift(unittest.TestCase):

    def setUp(self):
        if not (ROOT / ".agentbus/workflow.json").is_file() or not (INTEG / "tools/check_doc_drift.py").is_file():
            self.skipTest("test_doc_drift 仅在完整宿主工程环境执行，快照环境跳过")

        exe_path = ROOT / "成果/AgentBus.exe"
        wf_path = ROOT / ".agentbus/workflow.json"
        self.assertTrue(exe_path.is_file(), f"Missing {exe_path}")
        self.assertTrue(wf_path.is_file(), f"Missing {wf_path}")

        exe_bytes = exe_path.read_bytes()
        exe_sha = hashlib.sha256(exe_bytes).hexdigest()

        wf = json.loads(wf_path.read_text(encoding='utf-8'))
        wf_sha = wf.get("launcher", {}).get("sha256", "")
        self.assertEqual(exe_sha, wf_sha, "Executable sha256 must match workflow.json launcher")

    def test_project_md_and_state_md_contain_executable_sha(self):
        exe_path = ROOT / "成果/AgentBus.exe"
        exe_sha = hashlib.sha256(exe_path.read_bytes()).hexdigest()

        project_md = (ROOT / "PROJECT.md").read_text(encoding="utf-8")
        state_md = (ROOT / "STATE.md").read_text(encoding="utf-8")

        self.assertIn(exe_sha, project_md, "PROJECT.md must include the real executable sha256")
        self.assertIn(exe_sha, state_md, "STATE.md must include the real executable sha256")
        self.assertIn("以 `STATE.md` 与 `.agentbus/workflow.json` 为准", project_md)

    def test_current_json_is_valid_pointer(self):
        current_path = ROOT / ".agentbus/project-plan/current.json"
        self.assertTrue(current_path.is_file())
        cur_data = json.loads(current_path.read_text(encoding="utf-8"))
        self.assertIn("revision_id", cur_data)
        self.assertTrue(cur_data["revision_id"].startswith("r-"))

    def test_claims_md_updated_to_20261011(self):
        claims_md = (ROOT / "CLAIMS.md").read_text(encoding="utf-8")
        self.assertIn("2026-10-11", claims_md)

    def test_taskbook_update_date_accepts_newer_project_date(self):
        from tools.check_doc_drift import _extract_update_date

        text = (ROOT / "TASKBOOK.md").read_text(encoding="utf-8")
        self.assertEqual(_extract_update_date(text), date(2026, 10, 13))

    def test_check_doc_drift_cli_passes(self):
        cmd = [sys.executable, str(INTEG / "tools/check_doc_drift.py")]
        proc = subprocess.run(cmd, cwd=str(INTEG), capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"CLI output: {proc.stdout}\n{proc.stderr}")
        self.assertIn("[SUCCESS] All key project documents and executable alignments verified", proc.stdout)


if __name__ == "__main__":
    unittest.main()
