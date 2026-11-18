"""Tool checks must use portable inputs or an explicitly selected host. 2026-10-18."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import project_paths
import test_tools_quality_gate as gate


class ToolScopeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = self.root / 'tools'
        self.tools.mkdir()

    def case(self, name):
        self.assertTrue(hasattr(gate.TestToolsQualityGate, name), name + ' must remain a separate check')
        case = gate.TestToolsQualityGate(name)
        case.setUp()
        case.tools_dir = self.tools
        return case

    def portable_tools(self):
        names = ('export_source.py', 'check_offline.py', 'build_release.py',
                 'smoke_frozen.py', 'handover_screen.py', 'skill_manifest_audit.py',
                 'check_formal_sync.py', 'generate_build_manifest.py',
                 'delivery_self_proof.py', 'archived_binary_inventory.py')
        for name in names:
            (self.tools / name).write_text('import argparse\nargparse.ArgumentParser().parse_args()\n', encoding='utf-8')
        (self.tools / 'live_codex_check.py').write_text('raise RuntimeError("host tool was executed")\n', encoding='utf-8')

    def test_portable_help_needs_no_host_or_live_tool(self):
        self.portable_tools()
        self.case('test_tools_help_invocation').test_tools_help_invocation()

    def test_portable_missing_core_tool_is_failure(self):
        self.portable_tools()
        (self.tools / 'smoke_frozen.py').unlink()
        with self.assertRaisesRegex(AssertionError, 'smoke_frozen.py'):
            self.case('test_tools_help_invocation').test_tools_help_invocation()

    def test_no_host_context_reports_each_host_check_unverified(self):
        for name in ('test_host_tools_help_invocation',
                     'test_host_inject_jsdocs_module_callable', 'test_host_full_tool_quality'):
            with self.subTest(name=name):
                case = self.case(name)
                with patch.object(project_paths, 'host_source_root', return_value=None, create=True):
                    with self.assertRaisesRegex(unittest.SkipTest, 'AGENTBUS_TEST_SOURCE_ROOT'):
                        getattr(case, name)()

    def test_host_help_does_not_fall_back_to_snapshot(self):
        self.portable_tools()
        with patch.object(project_paths, 'host_source_root', return_value=self.root, create=True):
            with self.assertRaisesRegex(AssertionError, 'benchmark_canvas_interactions.py'):
                self.case('test_host_tools_help_invocation').test_host_tools_help_invocation()

    def test_host_inject_callable_comes_from_selected_source(self):
        (self.tools / 'inject_jsdocs.py').write_text('main = None\n', encoding='utf-8')
        with patch.object(project_paths, 'host_source_root', return_value=self.root, create=True):
            with self.assertRaises(AssertionError):
                self.case('test_host_inject_jsdocs_module_callable').test_host_inject_jsdocs_module_callable()
        (self.tools / 'inject_jsdocs.py').write_text('def main():\n    return None\n', encoding='utf-8')
        with patch.object(project_paths, 'host_source_root', return_value=self.root, create=True):
            self.case('test_host_inject_jsdocs_module_callable').test_host_inject_jsdocs_module_callable()

    def test_host_quality_uses_host_auditor_and_preserves_failure(self):
        (self.tools / 'check_tools_quality.py').write_text(
            'def audit_tools(tools, tests):\n'
            '    return {"missing_docs": ["host-only-undocumented.py"], '
            '"missing_tests": [], "all_passed": False}\n', encoding='utf-8')
        with patch.object(project_paths, 'host_source_root', return_value=self.root, create=True):
            with self.assertRaisesRegex(AssertionError, 'host-only-undocumented.py'):
                self.case('test_host_full_tool_quality').test_host_full_tool_quality()

    def test_invalid_explicit_host_context_is_error_not_skip(self):
        case = self.case('test_host_full_tool_quality')
        with patch.object(project_paths, 'host_source_root', side_effect=ValueError('invalid explicit host'), create=True):
            with self.assertRaisesRegex(ValueError, 'invalid explicit host'):
                case.test_host_full_tool_quality()
