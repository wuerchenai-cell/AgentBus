"""Codex internal independent audit integration corrections, 2026-10-05.

Author delivery bytes remain in author-corrections-snapshot. These public,
offline counterexamples exercise resource bounds and explicit validation facts.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus import skill_packages as scanner
from agentbus.skill_evidence import evaluate_evidence
from agentbus.skill_preview import build_sync_preview
from agentbus.skill_presentation import present_preview
from agentbus.workspace_files import WorkspaceFiles


class AuditBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='codex-public-audit-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pkg = self.root / 'pkg'
        self.pkg.mkdir()
        (self.pkg / 'SKILL.md').write_bytes(b'# public\n')

    def package(self):
        return scanner.inspect_package(self.root, 'pkg')

    def preview(self, package, **kwargs):
        return build_sync_preview(package, None, None, host=kwargs.get('host', 'codex'),
                                  scope='project', route='desktop')

    def test_verification_listing_stops_at_its_budget(self):
        real = scanner.os.scandir
        calls, consumed = 0, []
        class ChangedListing:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def __iter__(self):
                for n in range(33):
                    consumed.append(n)
                    yield types.SimpleNamespace(name='late-' + str(n))
                raise AssertionError('verification enumerated past its limit')
        def listing(path):
            nonlocal calls
            calls += 1
            return ChangedListing() if calls == 2 else real(path)
        with patch.object(scanner.os, 'scandir', side_effect=listing):
            with self.assertRaises(scanner.SkillPackageError) as error:
                scanner.inspect_package(self.root, 'pkg', max_dir_bytes=8)
        self.assertEqual(error.exception.code, 'TOO_LARGE')
        self.assertLessEqual(len(consumed), 9)

    def test_growth_before_first_hash_uses_file_budget(self):
        original = WorkspaceFiles._raw
        def grow(files, target, limit, **kwargs):
            target.write_bytes(b'x' * 256)
            return original(files, target, limit, **kwargs)
        with patch.object(WorkspaceFiles, '_raw', grow):
            with self.assertRaises(scanner.SkillPackageError) as error:
                scanner.inspect_package(self.root, 'pkg', max_file_bytes=32, max_total_bytes=32)
        self.assertEqual(error.exception.code, 'TOO_LARGE')

    def test_growth_before_final_hash_uses_file_budget(self):
        original, calls = WorkspaceFiles._raw, 0
        def grow(files, target, limit, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                target.write_bytes(b'x' * 256)
            return original(files, target, limit, **kwargs)
        with patch.object(WorkspaceFiles, '_raw', grow):
            with self.assertRaises(scanner.SkillPackageError) as error:
                scanner.inspect_package(self.root, 'pkg', max_file_bytes=32, max_total_bytes=32)
        self.assertEqual(error.exception.code, 'TOO_LARGE')

    def test_entry_metadata_read_uses_scan_budget(self):
        original, calls = WorkspaceFiles._raw, 0
        def grow(files, target, limit, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_bytes(b'x' * 256)
            return original(files, target, limit, **kwargs)
        with patch.object(WorkspaceFiles, '_raw', grow):
            with self.assertRaises(scanner.SkillPackageError) as error:
                scanner.inspect_package(self.root, 'pkg', max_file_bytes=32, max_total_bytes=32)
        self.assertEqual(error.exception.code, 'TOO_LARGE')

    def test_each_hash_uses_remaining_total_budget(self):
        (self.pkg / 'a.py').write_bytes(b'# public\n')
        original = WorkspaceFiles._raw
        def grow(files, target, limit, **kwargs):
            if target.name == 'a.py':
                target.write_bytes(b'x' * 12)
            return original(files, target, limit, **kwargs)
        with patch.object(WorkspaceFiles, '_raw', grow):
            with self.assertRaises(scanner.SkillPackageError) as error:
                scanner.inspect_package(self.root, 'pkg', max_file_bytes=32, max_total_bytes=20)
        self.assertEqual(error.exception.code, 'TOO_LARGE')

    def test_manifest_digest_rejects_terminal_newline(self):
        pkg = self.package()
        pkg['files'][0]['sha256'] += '\n'
        pkg['content_digest'] = hashlib.sha256(json.dumps(
            {'schema_version': 1, 'files': pkg['files']}, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertIn('INVALID_SOURCE', self.preview(pkg)['reason_codes'])

    def test_host_rejects_terminal_newline(self):
        self.assertIn('INVALID_HOST', self.preview(self.package(), host='codex\n')['reason_codes'])

    def test_package_schema_boolean_is_not_version_one(self):
        pkg = self.package(); pkg['schema_version'] = True
        self.assertIn('INVALID_SOURCE', self.preview(pkg)['reason_codes'])

    def test_record_schema_boolean_is_not_version_one(self):
        pkg = self.package()
        record = {'schema_version': True, 'owner': 'agentbus', 'host': 'codex',
                  'scope': 'project', 'route': 'desktop', 'before_content_digest': pkg['content_digest']}
        out = build_sync_preview(pkg, pkg, record, host='codex', scope='project', route='desktop')
        self.assertIn('INVALID_RECORD', out['reason_codes'])

    def test_huge_now_has_controlled_argument_error(self):
        with self.assertRaises(ValueError):
            evaluate_evidence({'observed_at_ts': 0}, {}, now_ts=10 ** 400, max_age_s=1.0)

    def test_digest_shape_alone_is_not_content_validation(self):
        self.assertFalse(present_preview({'content': {'source_digest': 'a' * 64}})['content_checked'])

    def test_validated_preview_can_display_checked_content(self):
        out = self.preview(self.package())
        self.assertIs(out['content'].get('source_verified'), True)
        self.assertTrue(present_preview(out)['content_checked'])

    def test_checked_content_requires_literal_true_and_strict_digest(self):
        for flag, digest in ((1, 'a' * 64), (True, 'a' * 64 + '\n')):
            with self.subTest(flag=flag, digest=digest):
                self.assertFalse(present_preview({'content': {'source_verified': flag,
                                                               'source_digest': digest}})['content_checked'])

    def test_waiting_timestamps_alone_are_not_current_validation(self):
        view = present_preview({'wait_evidence': {'waiting_tool': 'public-watch',
                                                 'observed_at_ts': -2, 'expires_at_ts': -1}})
        self.assertIn('人工接手', view['waiting_hint'])

    def test_waiting_accepts_only_explicit_verified_boolean(self):
        for verified in (True, 1, 'verified'):
            view = present_preview({'now_ts': 15, 'wait_evidence': {
                'waiting_tool': 'public-watch', 'observed_at_ts': 10,
                'expires_at_ts': 20, 'verified': verified}})
            self.assertEqual('人工接手' not in view['waiting_hint'], verified is True)

    def test_waiting_validation_cannot_override_expired_or_invalid_dates(self):
        for observed, expires, now in ((-2, -1, 0), (10, 20, 20), (16, 20, 15),
                                      (10, float('inf'), 15), (10, 20, True)):
            view = present_preview({'now_ts': now, 'wait_evidence': {
                'waiting_tool': 'public-watch', 'observed_at_ts': observed,
                'expires_at_ts': expires, 'verified': True}})
            self.assertIn('人工接手', view['waiting_hint'])

    def test_done_requires_explicit_artifact_validation(self):
        for artifacts, verified in (([None], True), ([{'path': 'a'}], True),
                                     ([{'path': 'a', 'bytes': 1, 'sha256': 'b' * 64}], 1),
                                     ([{'path': 'a', 'bytes': 1, 'sha256': 'b' * 64}], False)):
            view = present_preview({'source_status': 'done', 'result_outcome': 'success',
                                    'artifacts': artifacts, 'artifacts_verified': verified})
            self.assertNotIn('已完成', view['completion_label'])

    def test_done_accepts_validated_success_without_granting_permissions(self):
        view = present_preview({'source_status': 'done', 'result_outcome': 'success',
                                'artifacts_verified': True,
                                'artifacts': [{'path': 'a', 'bytes': 1, 'sha256': 'b' * 64}]})
        self.assertIn('已完成', view['completion_label'])
        self.assertIs(view['write_permitted'], False)


if __name__ == '__main__':
    unittest.main()
