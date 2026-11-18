"""Tests for AgentBus BUILD-MANIFEST generation, integrity, and source-tree drift audit."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = next((p for p in Path(__file__).resolve().parents if (p / ".agentbus").is_dir()), Path(__file__).resolve().parents[3])
PACKAGING_MANIFEST = ROOT / "packaging/BUILD-MANIFEST.json"


class TestBuildManifest(unittest.TestCase):

    def setUp(self):
        if not PACKAGING_MANIFEST.is_file():
            self.skipTest(f"BUILD-MANIFEST 未包含在当前源码发布快照中: {PACKAGING_MANIFEST}")
        with open(PACKAGING_MANIFEST, "r", encoding="utf-8") as f:
            self.data = json.load(f)

    def test_build_manifest_structure(self):
        self.assertEqual(self.data.get("manifest_version"), "1.0.0")
        self.assertIn("target_binary", self.data)
        self.assertIn("formal_source_tree_audit", self.data)
        self.assertIn("package_modules", self.data)
        self.assertIn("package_data_assets", self.data)
        self.assertIn("manifest_summary", self.data)

        summary = self.data["manifest_summary"]
        self.assertGreater(summary["total_python_modules"], 80)
        self.assertGreater(summary["total_data_assets"], 20)

    def test_manifest_covers_all_critical_modules(self):
        modules = self.data["package_modules"]
        critical_modules = [
            "agentbus/task_tree.py",
            "agentbus/web_bridge.py",
            "agentbus/delivery_verify.py",
            "agentbus/timeline.py",
            "agentbus/attestation.py",
            "agentbus/tree_templates.py",
            "agentbus/claim_fencing.py",
            "agentbus/task_assignment.py",
            "agentbus/heartbeat.py",
            "agentbus/exclusive_lock.py",
            "entry.py"
        ]
        for mod in critical_modules:
            self.assertIn(mod, modules, f"Critical module {mod} must be present in BUILD-MANIFEST")
            self.assertTrue(modules[mod]["sha256"], f"Module {mod} missing sha256")
            self.assertGreater(modules[mod]["bytes"], 0, f"Module {mod} bytes must be > 0")

    def test_manifest_sha256_matches_source_tree(self):
        import hashlib
        modules = self.data["package_modules"]
        for rel_path, item in modules.items():
            file_path = ROOT / rel_path
            self.assertTrue(file_path.is_file(), f"Source file does not exist: {file_path}")
            real_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
            self.assertEqual(real_sha, item["sha256"], f"SHA256 mismatch for {rel_path}")

        assets = self.data["package_data_assets"]
        for rel_path, item in assets.items():
            file_path = ROOT / rel_path
            self.assertTrue(file_path.is_file(), f"Asset file does not exist: {file_path}")
            real_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
            self.assertEqual(real_sha, item["sha256"], f"SHA256 mismatch for data asset {rel_path}")

    def test_formal_tree_audit_records_drift_truth(self):
        from tools.generate_build_manifest import build_manifest_data

        audit = self.data["formal_source_tree_audit"]
        live = build_manifest_data(ROOT, REPO_ROOT / "成果/agentbus/agentbus")["formal_source_tree_audit"]
        self.assertEqual(audit, live, "Manifest must describe the current formal-source drift, not a stale snapshot")
        self.assertEqual(audit["synchronized"], not (
            audit["summary"]["missing_in_formal_count"] or audit["summary"]["drift_in_formal_count"]
        ))
        if audit["synchronized"]:
            self.assertIn("完全同步一致", audit["status_statement"])
            self.assertEqual(audit["summary"]["missing_in_formal_count"], 0)
            self.assertEqual(audit["summary"]["drift_in_formal_count"], 0)
        else:
            self.assertFalse(audit["synchronized"], "Formal tree must be marked as not synchronized")
            self.assertIn("尚未同步", audit["status_statement"])
            summary = audit["summary"]
            self.assertGreater(summary["missing_in_formal_count"] + summary["drift_in_formal_count"], 0)

            missing_modules = [m["path"] for m in audit["missing_in_formal"]]
            drifted_modules = [m["path"] for m in audit["drift_in_formal"]]
            audited_modules = set(missing_modules) | set(drifted_modules)
            self.assertTrue(
                {"agentbus/task_tree.py", "agentbus/web_bridge.py", "agentbus/delivery_verify.py"} & audited_modules,
                "At least one critical integration module must be represented in the live drift audit",
            )

    def test_verify_manifest_cli_passes(self):
        cmd = [sys.executable, str(ROOT / "tools/generate_build_manifest.py"), "--check"]
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"CLI check failed: {proc.stdout}\n{proc.stderr}")
        self.assertIn("[OK] Build manifest verification succeeded", proc.stdout)


class PortableExportResourceManifestTests(unittest.TestCase):
    def test_new_frozen_export_validator_and_public_files_are_tracked(self):
        import tempfile
        from tools.generate_build_manifest import build_manifest_data
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('agentbus/__init__.py','tools/export_source.py','LICENSE',
                         'SOURCE_LICENSE_STATUS.md','docs/public/README.md',
                         'docs/public/RELEASE.md','docs/public/ECOSYSTEM.md','docs/public/SECURITY.md'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture',encoding='utf-8')
            manifest=build_manifest_data(root,root/'agentbus')
            self.assertIn('tools/export_source.py',manifest['package_modules'])
            self.assertEqual(manifest['package_modules']['tools/export_source.py']['module'],'tools.export_source')
            for name in ('LICENSE','SOURCE_LICENSE_STATUS.md','docs/public/README.md',
                         'docs/public/RELEASE.md','docs/public/ECOSYSTEM.md','docs/public/SECURITY.md'):
                self.assertIn(name,manifest['package_data_assets'])


if __name__ == "__main__":
    unittest.main()
