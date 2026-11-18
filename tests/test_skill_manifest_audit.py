"""§7.24-② 技能清单字段对齐的契约测试（`tools/skill_manifest_audit.py`）。

守两条性质：
  1. **字段齐备可检出**：缺字段 / 空字段 / 无 frontmatter 都要有对应判定；
  2. **不把"未登记"蒙混成合格**：license 是占位说明时必须判 `needs_license_audit`，
     `--strict` 必须退出 1——否则"对齐开放标准"会退化成"填了字就算对齐"。
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOL = ROOT / "tools" / "skill_manifest_audit.py"
from tools import skill_manifest_audit as sma


class ParseTests(unittest.TestCase):
    def test_required_fields_include_the_three_named_by_the_ticket(self):
        for name in ("license", "compatibility", "allowed-tools"):
            self.assertIn(name, sma.REQUIRED_FIELDS)

    def test_real_project_skill_has_all_required_fields(self):
        report = sma.audit(sma.SKILLS_ROOT)
        self.assertGreaterEqual(report["count"], 1, "项目内至少应有一个技能")
        for row in report["skills"]:
            self.assertEqual([], row["missing"], "%s 缺字段" % row["skill"])
            self.assertEqual([], row["empty"], "%s 有空字段" % row["skill"])
            self.assertEqual("", row["error"])

    def test_real_project_skill_license_stays_unapproved_until_owner_decides(self):
        report = sma.audit(sma.SKILLS_ROOT)
        skill = next(row for row in report["skills"]
                     if row["skill"] == "agentbus-team")
        self.assertTrue(skill["needs_license_audit"])
        self.assertEqual("needs_license_audit", skill["verdict"])

    def test_list_values_are_parsed(self):
        fields, error = sma.parse_frontmatter(
            "---\nname: x\nallowed-tools: [Bash, Read]\ncompatibility: [a, b]\n---\n")
        self.assertEqual("", error)
        self.assertEqual(["Bash", "Read"], fields["allowed-tools"])
        self.assertEqual(["a", "b"], fields["compatibility"])

    def test_multiline_description_is_kept(self):
        fields, _ = sma.parse_frontmatter(
            "---\nname: x\ndescription: first line\n  second line\n---\n")
        self.assertIn("first line", fields["description"])
        self.assertIn("second line", fields["description"])


class VerdictTests(unittest.TestCase):
    def _skill(self, frontmatter: str) -> dict:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "demo" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("---\n%s---\n\n# body\n" % frontmatter, encoding="utf-8")
            return sma.audit_skill(path)

    def test_missing_field_is_reported(self):
        row = self._skill("name: x\ndescription: d\nlicense: MIT\n")
        self.assertEqual("missing_fields", row["verdict"])
        self.assertIn("allowed-tools", row["missing"])
        self.assertIn("compatibility", row["missing"])

    def test_empty_value_counts_as_missing(self):
        row = self._skill("name: x\ndescription: d\nlicense: MIT\n"
                          "allowed-tools: []\ncompatibility: [a]\n")
        self.assertEqual("missing_fields", row["verdict"])
        self.assertEqual(["allowed-tools"], row["empty"])

    def test_no_frontmatter_is_malformed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "demo" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("# just a heading\n", encoding="utf-8")
            row = sma.audit_skill(path)
        self.assertEqual("malformed", row["verdict"])
        self.assertTrue(row["error"])

    def test_placeholder_license_is_not_ok(self):
        """核心纪律：占位说明不得判为合格。"""
        row = self._skill("name: x\ndescription: d\nlicense: 未登记\n"
                          "allowed-tools: [Read]\ncompatibility: [a]\n")
        self.assertEqual("needs_license_audit", row["verdict"])
        self.assertTrue(row["needs_license_audit"])

    def test_real_license_is_ok(self):
        row = self._skill("name: x\ndescription: d\nlicense: Apache-2.0\n"
                          "allowed-tools: [Read]\ncompatibility: [a]\n")
        self.assertEqual("ok", row["verdict"])
        self.assertFalse(row["needs_license_audit"])


class CliTests(unittest.TestCase):
    def test_strict_exits_nonzero_when_a_skill_is_not_ok(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "skills"
            (root / "demo").mkdir(parents=True)
            (root / "demo" / "SKILL.md").write_text(
                "---\nname: demo\ndescription: d\n---\n", encoding="utf-8")
            proc = subprocess.run([sys.executable, str(TOOL),
                                   "--skills-root", str(root), "--strict"],
                                  capture_output=True, text=True, encoding='utf-8',
                                  env={**os.environ, 'PYTHONUTF8': '1'})
            self.assertEqual(1, proc.returncode, "有未对齐项时 --strict 必须退出 1")

    def test_strict_exits_zero_when_all_ok(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "skills"
            (root / "demo").mkdir(parents=True)
            (root / "demo" / "SKILL.md").write_text(
                "---\nname: demo\ndescription: d\nlicense: MIT\n"
                "allowed-tools: [Read]\ncompatibility: [workbuddy]\n---\n",
                encoding="utf-8")
            proc = subprocess.run([sys.executable, str(TOOL),
                                   "--skills-root", str(root), "--strict"],
                                  capture_output=True, text=True, encoding='utf-8',
                                  env={**os.environ, 'PYTHONUTF8': '1'})
            self.assertEqual(0, proc.returncode, proc.stderr)

    def test_json_output_is_machine_readable(self):
        proc = subprocess.run([sys.executable, str(TOOL), "--json"],
                              capture_output=True, text=True, encoding='utf-8',
                              env={**os.environ, 'PYTHONUTF8': '1'})
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        for key in ("required_fields", "skills", "verdicts", "read_only"):
            self.assertIn(key, payload)
        self.assertTrue(payload["read_only"])

    def test_strict_output_is_utf8_under_legacy_console_encoding(self):
        env = {**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "gbk"}
        proc = subprocess.run([sys.executable, str(TOOL), "--strict"],
                              capture_output=True, env=env)
        self.assertEqual(1, proc.returncode)
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        self.assertIn("needs_license_audit", stdout)
        self.assertNotIn("UnicodeEncodeError", stderr)

    def test_empty_skills_root_does_not_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            report = sma.audit(Path(temp) / "nope")
            self.assertEqual(0, report["count"])
            self.assertEqual({}, report["verdicts"])


class ReadOnlyTests(unittest.TestCase):
    def test_tool_never_writes(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        banned = {"write_text", "write_bytes", "mkdir", "unlink", "rmtree", "rename"}
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in banned:
                    offenders.append(name)
        self.assertEqual([], offenders, "审计工具必须只读")


if __name__ == "__main__":
    unittest.main()
