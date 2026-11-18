"""Credential-free regression entry. Source: Codex, 2026-10-03.

Tests use temporary workspaces and mocked model transports. The optional
HTTP-provider discovery is replaced during discovery AND execution so a
developer/CI run never needs the user's private provider configuration.
This is a test entrypoint, not a general operating-system network sandbox.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
HOST_TEST_IDS = (
    'test_agent_rigid_process_guard.TestAgentRigidProcessGuard.test_legacy_binaries_physical_deprecation',
    'test_agent_rigid_process_guard.TestAgentRigidProcessGuard.test_preflight_cli_passes_on_current_workspace',
    'test_audit_formal_binary_sync_gate.TestFormalBinarySyncGate.test_current_manifest_is_synchronized',
    'test_audit_formal_binary_sync_gate.TestFormalBinarySyncGate.test_check_formal_sync_cli_strict',
    'test_audit_formal_binary_sync_gate.TestFormalBinarySyncGate.test_workflow_json_launcher_bytes_field',
    'test_formal_sync.TestFormalSync.test_formal_sync_on_live_manifest',
    'test_root_hygiene.RealProjectRootHygieneTests',
    'test_launcher_truth_check.RealWorkspaceTests',
    'test_host_file_registration_check.RealWorkspaceTests',
    'test_tree_capability_audit.CapabilityAuditTests.test_real_workspace_smoke',
    'test_tree_structure_preflight.RealWorkspaceTests',
    'test_skill_manifest_audit.ParseTests.test_real_project_skill_has_all_required_fields',
    'test_skill_manifest_audit.ParseTests.test_real_project_skill_license_stays_unapproved_until_owner_decides',
    'test_tools_quality_gate.TestToolsQualityGate.test_host_full_tool_quality',
    'test_tools_quality_gate.TestToolsQualityGate.test_host_tools_help_invocation',
    'test_tools_quality_gate.TestToolsQualityGate.test_host_inject_jsdocs_module_callable',
    'test_tools_quality_gate.TestToolsQualityGate.test_auxiliary_and_deprecated_tools_safety',
)



@contextmanager
def background_thread_failures(current_test=None):
    """Make uncaught child-thread errors visible to the test run outcome."""
    previous = threading.excepthook
    # A nested test run owns its expected failures. Preserve traceback printing
    # without counting a deliberately failing inner run as an outer failure.
    delegate = getattr(previous, '_agentbus_original_hook', previous)
    failures = []
    lock = threading.Lock()

    def record(args):
        with lock:
            failures.append({'thread': args.thread.name if args.thread else '',
                             'exception': args.exc_type.__name__,
                             'test_active_at_exception': (current_test or ['discovery'])[0]})
        delegate(args)

    record._agentbus_original_hook = delegate
    threading.excepthook = record
    try:
        yield failures
    finally:
        threading.excepthook = previous


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default=None, help='测试匹配模式（如 test_*.py）')
    parser.add_argument('--scope', choices=['portable', 'host'], default='portable',
                        help='Portable source tests or explicit host release audits; a normal release requires both.')
    parser.add_argument('--tier', choices=['targeted', 'smoke', 'contract', 'release', 'full'], default='full',
                        help='验证分层档位（§7.21/§10.12）：smoke/targeted（定向秒级）、contract（契约分钟级）、release（发布打包验证）、full（全量回归）')
    args = parser.parse_args(argv)
    if args.scope == 'host' and args.pattern is not None:
        parser.error('Host release audits cannot be narrowed with --pattern')
    if args.scope == 'portable':
        os.environ.pop('AGENTBUS_TEST_PROJECT_ROOT', None)
        os.environ.pop('AGENTBUS_TEST_SOURCE_ROOT', None)
    sys.path.insert(0, str(ROOT / 'tests'))
    if args.scope == 'host':
        from project_paths import host_project_root, host_source_root
        if host_project_root() is None or host_source_root() is None:
            parser.error('Host audits require explicit AGENTBUS_TEST_PROJECT_ROOT and AGENTBUS_TEST_SOURCE_ROOT')
    # Python -X flags only apply to the current interpreter. The tests also
    # launch Python scripts with redirected output containing Chinese text.
    os.environ.setdefault('PYTHONUTF8', '1')
    if os.name == 'nt':
        temporary = tempfile.gettempdir()
        canonical = str(Path(temporary).resolve(strict=True))
        if canonical != temporary:
            # Keep subprocess fixtures and the parent on the same spelling
            # when Windows supplies an 8.3 alias for the user's temp path.
            tempfile.tempdir = canonical
            for name in ('TMPDIR', 'TEMP', 'TMP'):
                os.environ[name] = canonical
    # Subprocess tests inherit cwd, not this interpreter's sys.path. Use the
    # same checkout for parent discovery and python -m agentbus children.
    os.chdir(ROOT)
    from agentbus.detect import Probe
    start = time.monotonic()
    stub = Probe('anthropic-api', 'http-llm', note='credential-free test metadata')

    pattern = args.pattern
    if pattern is None:
        if args.tier in ('targeted', 'smoke'):
            pattern = 'test_ux_*.py'
        elif args.tier == 'contract':
            pattern = 'test_*contract*.py'
        elif args.tier == 'release':
            pattern = 'test_release_tools.py'
        else:
            pattern = 'test*.py'

    current_test = ['discovery']

    class TrackedResult(unittest.TextTestResult):
        def startTest(self, test):
            current_test[0] = test.id()
            super().startTest(test)

        def stopTest(self, test):
            super().stopTest(test)
            current_test[0] = 'between_tests'

    with background_thread_failures(current_test) as thread_failures, patch('agentbus.detect.probe_http_llm', return_value=stub):
        suite = (unittest.defaultTestLoader.loadTestsFromNames(HOST_TEST_IDS)
                 if args.scope == 'host' else
                 unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=pattern))
        count = suite.countTestCases()
        if count == 0:
            print(f'No tests discovered for pattern {pattern}; refuse an empty green run.', file=sys.stderr)
            return 2
        result = unittest.TextTestRunner(verbosity=1, resultclass=TrackedResult).run(suite)
    report = {'tests': result.testsRun,
              'passed': result.testsRun - len(result.skipped) - len(result.errors) - len(result.failures),
              'skipped': len(result.skipped), 'failures': len(result.failures),
              'errors': len(result.errors), 'elapsed_s': round(time.monotonic() - start, 3),
              'credential_probe_stubbed': True, 'model_calls': 0,
              'tier': args.tier,
              'scope': args.scope,
              'host_validation_performed': args.scope == 'host',
              'normal_release_requires': ['portable', 'host'],
              'pattern': pattern,
              'unhandled_thread_exceptions': len(thread_failures),
              'thread_exception_details': thread_failures[:20],
              'success': result.wasSuccessful() and not thread_failures
                         and (args.scope != 'host' or not result.skipped)}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
