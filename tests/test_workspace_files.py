"""Source: Codex candidate contract, 2026-10-04; isolated local tests only."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class WorkspaceFilesTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('agentbus.workspace_files'),
                             'the candidate workspace file contract is missing')
        self.module = importlib.import_module('agentbus.workspace_files')
        self.temp = tempfile.TemporaryDirectory(prefix='agentbus-workspace-files-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = self.module.WorkspaceFiles(self.root)

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def reject(self, code, function, *args, **kwargs):
        with self.assertRaises(self.module.WorkspaceFileError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def digest(self, data):
        return hashlib.sha256(data).hexdigest()

    def test_utf8_bom_crlf_and_unterminated_line_are_preserved(self):
        data = b'\xef\xbb\xbf' + '中文😀\r\nsecond\r\nlast'.encode('utf-8')
        self.put('成果/报告.md', data)
        first = self.files.read_text('成果\\报告.md', max_lines=2)
        self.assertEqual(first['path'], '成果/报告.md')
        self.assertEqual(first['text'], '中文😀\r\nsecond\r\n')
        self.assertEqual(first['total_lines'], 3)
        self.assertEqual(first['next_line'], 3)
        self.assertTrue(first['bom'])
        self.assertEqual(first['encoding'], 'utf-8')
        self.assertEqual(first['newline_style'], 'CRLF')
        self.assertEqual(first['bytes'], len(data))
        self.assertEqual(first['sha256'], self.digest(data))
        last = self.files.read_text('成果/报告.md', start_line=3,
                                    expected_sha256=first['sha256'])
        self.assertEqual(last['text'], 'last')
        self.assertIsNone(last['next_line'])
        self.assertFalse((self.root / '.agentbus').exists())

    def test_empty_text_and_page_after_end_return_no_next_line(self):
        self.put('empty.txt', b'')
        page = self.files.read_text('empty.txt')
        self.assertEqual((page['text'], page['total_lines'], page['next_line']), ('', 0, None))
        self.assertEqual(page['newline_style'], 'none')
        self.put('one.txt', b'one\n')
        self.assertEqual(self.files.read_text('one.txt', start_line=4)['text'], '')

    def test_newline_metadata_reports_lf_cr_and_mixed_without_conversion(self):
        for name, raw, style in [('lf', b'a\nb', 'LF'), ('cr', b'a\rb', 'CR'),
                                 ('mixed', b'a\r\nb\nc\rd', 'mixed')]:
            self.put(name + '.txt', raw)
            result = self.files.read_text(name + '.txt')
            self.assertEqual(result['newline_style'], style)
            self.assertEqual(result['text'].encode('utf-8'), raw)

    def test_unicode_line_separator_does_not_split_lf_line_numbers(self):
        self.put('unicode.txt', 'a\u2028b\nnext'.encode('utf-8'))
        page = self.files.read_text('unicode.txt', max_lines=1)
        self.assertEqual((page['text'], page['total_lines'], page['next_line']), ('a\u2028b\n', 2, 2))

    def test_invalid_utf8_and_unpaired_surrogate_are_rejected(self):
        self.put('bad.txt', b'good\n\xff')
        self.reject('INVALID_UTF8', self.files.read_text, 'bad.txt')
        self.reject('INVALID_UTF8', self.files.write_text, 'out.txt', '\ud800',
                    owner='codex', expected_sha256='MISSING')
        self.assertFalse((self.root / 'out.txt').exists())

    def test_page_snapshot_hash_must_match_current_bytes(self):
        self.put('page.txt', b'first\nsecond')
        old = self.files.read_text('page.txt', max_lines=1)
        self.put('page.txt', b'changed\nsecond')
        self.reject('CONFLICT', self.files.read_text, 'page.txt', start_line=2,
                    expected_sha256=old['sha256'])

    def test_unsafe_path_spellings_are_rejected_before_file_access(self):
        paths = ['', '/tmp/a', '\\rooted', 'C:relative.txt', 'C:/absolute.txt',
                 '//server/share/a', '\\\\?\\C:\\x', '../a', 'a/../b', './a',
                 'a/./b', 'a//b', 'a/', 'a:stream', 'NUL.txt', 'COM¹.txt',
                 'LPT9.log', 'trailing.', 'trailing ', 'a/\x00b', 'a/\x1fb',
                 'a/.workbuddy/x', '.git/config', '.agentbus/config.json',
                 '.codex/auth.json', '.ssh/id_rsa', 'a/<bad>', 'a?b']
        for name in paths:
            with self.subTest(path=name):
                self.reject('UNSAFE_PATH', self.files.read_text, name)
        self.assertFalse((self.root / '.agentbus').exists())

    def test_directory_and_missing_files_do_not_become_artifacts(self):
        (self.root / 'folder').mkdir()
        self.reject('NOT_FILE', self.files.read_text, 'folder')
        self.reject('NOT_FOUND', self.files.describe_artifact, 'absent.bin')

    def test_invalid_hashes_and_pagination_are_rejected(self):
        self.put('a.txt', b'a')
        for value in ['', 'MISSING', 'x' * 64, 'A' * 64, 12]:
            with self.subTest(value=value):
                self.reject('INVALID_HASH', self.files.read_text, 'a.txt', expected_sha256=value)
        for params in ({'start_line': 0}, {'start_line': True}, {'max_lines': 0}, {'max_lines': 201}):
            self.reject('INVALID_ARGUMENT', self.files.read_text, 'a.txt', **params)
        self.reject('INVALID_ARGUMENT', self.files.write_text, 'a.txt', 'b',
                    owner='', expected_sha256=self.digest(b'a'))

    def test_text_and_output_limits_are_enforced(self):
        self.put('huge.txt', b'x' * (8 * 1024 * 1024 + 1))
        self.reject('TOO_LARGE', self.files.read_text, 'huge.txt')
        self.reject('TOO_LARGE', self.files.write_text, 'out.txt', 'x' * (8 * 1024 * 1024 + 1),
                    owner='codex', expected_sha256='MISSING')
        self.put('long-line.txt', b'x' * (64 * 1024 + 1))
        self.reject('PAGE_TOO_LARGE', self.files.read_text, 'long-line.txt')
        self.put('many.txt', (b'a' * 1000 + b'\n') * 200)
        page = self.files.read_text('many.txt')
        self.assertLessEqual(len(page['text'].encode('utf-8')), 64 * 1024)
        self.assertTrue(1 < page['next_line'] < 201)

    def test_artifact_size_limit_is_checked_without_loading_whole_file(self):
        with (self.root / 'large.bin').open('wb') as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
        self.reject('TOO_LARGE', self.files.describe_artifact, 'large.bin')

    def test_create_and_cas_update_are_atomic_and_release_lease(self):
        created = self.files.write_text('nested/文件.txt', 'old\r\n', owner='codex', expected_sha256='MISSING')
        self.assertEqual(created['sha256'], self.digest(b'old\r\n'))
        changed = self.files.write_text('nested/文件.txt', 'new😀', owner='codex', expected_sha256=created['sha256'])
        self.assertTrue(changed['committed'])
        self.assertEqual((self.root / 'nested/文件.txt').read_bytes(), 'new😀'.encode('utf-8'))
        self.assertEqual(list((self.root / '.agentbus/leases').glob('*.json')), [])

    def test_stale_cas_and_missing_sentinel_never_overwrite(self):
        path = self.put('keep.txt', b'current')
        self.reject('CONFLICT', self.files.write_text, 'keep.txt', 'replacement', owner='codex',
                    expected_sha256=self.digest(b'old'))
        self.reject('CONFLICT', self.files.write_text, 'keep.txt', 'replacement', owner='codex',
                    expected_sha256='MISSING')
        self.reject('CONFLICT', self.files.write_text, 'absent.txt', 'replacement', owner='codex',
                    expected_sha256=self.digest(b''))
        self.assertEqual(path.read_bytes(), b'current')

    def test_dry_run_is_read_only_even_for_missing_parent_and_control_dir(self):
        preview = self.files.write_text('new/child.txt', 'preview', owner='codex',
                                         expected_sha256='MISSING', dry_run=True)
        self.assertTrue(preview['dry_run'])
        self.assertFalse(preview['committed'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_stop_blocks_real_and_preview_writes(self):
        self.put('.agentbus/STOP', b'stop')
        for dry_run in (False, True):
            self.reject('STOPPED', self.files.write_text, 'out.txt', 'no', owner='codex',
                        expected_sha256='MISSING', dry_run=dry_run)
        self.assertFalse((self.root / 'out.txt').exists())

    def test_stop_appearing_after_staging_preserves_bytes_and_releases_own_lease(self):
        target = self.put('keep.txt', b'old')
        real_fsync = self.module.os.fsync
        def add_stop_after_sync(fd):
            result = real_fsync(fd)
            if os.fstat(fd).st_size == len(b'new payload'):
                (self.root / '.agentbus/STOP').write_bytes(b'stop now')
            return result
        with patch.object(self.module.os, 'fsync', side_effect=add_stop_after_sync):
            self.reject('STOPPED', self.files.write_text, 'keep.txt', 'new payload', owner='codex',
                        expected_sha256=self.digest(b'old'))
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(list((self.root / '.agentbus/leases').glob('*.json')), [])

    def test_page_json_envelope_and_escaped_text_stay_within_output_limit(self):
        self.put('escaped.txt', (b'"' * 1000 + b'\r\n') * 100)
        result = self.files.read_text('escaped.txt')
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode('utf-8')), 64 * 1024)
        self.assertIsNotNone(result['next_line'])

    def test_two_participating_writers_cannot_both_commit_same_snapshot(self):
        self.put('shared.txt', b'old')
        def writer(value):
            try:
                return self.files.write_text('shared.txt', value, owner=value,
                                             expected_sha256=self.digest(b'old'))['committed']
            except self.module.WorkspaceFileError as exc:
                self.assertIn(exc.code, ('CONFLICT', 'LEASE_BUSY', 'FILE_CHANGED'))
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ('first', 'second')))
        self.assertEqual(sum(results), 1)
        self.assertIn((self.root / 'shared.txt').read_bytes(), (b'first', b'second'))

    def test_any_live_lease_including_same_owner_blocks_write(self):
        from agentbus.lease import LeaseManager
        manager = LeaseManager(self.root / '.agentbus')
        for owner in ('codex', 'peer'):
            lease = manager.acquire('a.txt', owner, ttl_s=120)
            self.addCleanup(manager.release, lease)
            self.reject('LEASE_BUSY', self.files.write_text, 'a.txt', 'no', owner='codex',
                        expected_sha256='MISSING')
            self.assertTrue(manager.release(lease))
        self.assertFalse((self.root / 'a.txt').exists())

    def test_expired_lease_can_be_replaced_without_preserving_old_token(self):
        from agentbus.lease import LeaseManager
        manager = LeaseManager(self.root / '.agentbus')
        old = manager.acquire('a.txt', 'peer', ttl_s=-1)
        self.files.write_text('a.txt', 'yes', owner='codex', expected_sha256='MISSING')
        self.assertEqual((self.root / 'a.txt').read_bytes(), b'yes')
        self.assertIsNone(manager.read('a.txt'))

    def test_corrupt_lease_is_not_treated_as_free(self):
        from agentbus.lease import LeaseManager
        manager = LeaseManager(self.root / '.agentbus')
        manager._file('a.txt').write_bytes(b'{corrupt')
        self.reject('LEASE_BUSY', self.files.write_text, 'a.txt', 'no', owner='codex', expected_sha256='MISSING')
        self.assertFalse((self.root / 'a.txt').exists())

    def test_lease_token_and_expiry_are_rechecked_before_publish(self):
        from agentbus.lease import LeaseManager
        real_acquire = LeaseManager.acquire
        for change in ('token', 'expired'):
            def changed(manager, *args, **kwargs):
                lease = real_acquire(manager, *args, **kwargs)
                record = lease.to_dict()
                if change == 'token':
                    record['token'] = 'replacement-token'
                else:
                    record['acquired_at'] = 0
                manager._file(lease.rel).write_text(json.dumps(record), encoding='utf-8')
                return lease
            with self.subTest(change=change), patch.object(LeaseManager, 'acquire', changed):
                self.reject('LEASE_LOST', self.files.write_text, change + '.txt', 'no',
                            owner='codex', expected_sha256='MISSING')
            self.assertFalse((self.root / (change + '.txt')).exists())

    def test_fsync_and_replace_failure_preserve_old_file_and_unrelated_temp(self):
        path = self.put('keep.txt', b'old')
        foreign = self.put('.foreign.tmp', b'owned by another writer')
        real_fsync = self.module.os.fsync
        lease_dir = self.root / '.agentbus/leases'
        from agentbus.lease import LeaseManager
        LeaseManager(self.root / '.agentbus')
        for operation in ('fsync', 'replace'):
            def fail_file_sync(fd):
                # Existing lease publication must remain real; fail only target staging.
                if os.fstat(fd).st_size == len(b'new payload'):
                    raise OSError('target fsync failed')
                return real_fsync(fd)
            replacement = fail_file_sync if operation == 'fsync' else OSError('replace failed')
            with self.subTest(operation=operation), patch.object(self.module.os, operation, side_effect=replacement):
                self.reject('IO_ERROR', self.files.write_text, 'keep.txt', 'new payload', owner='codex',
                            expected_sha256=self.digest(b'old'))
            self.assertEqual(path.read_bytes(), b'old')
            self.assertEqual(foreign.read_bytes(), b'owned by another writer')
            self.assertEqual(list(self.root.glob('.keep.txt.*.tmp')), [])
            self.assertEqual(list(lease_dir.glob('*.json')), [])

    def test_create_publish_does_not_overwrite_file_appearing_at_commit(self):
        target = self.root / 'race.txt'
        original = self.module.os.rename if os.name == 'nt' else self.module.os.link
        def another_writer(source, destination, *args, **kwargs):
            if Path(destination) == target:
                target.write_bytes(b'foreign write')
            return original(source, destination, *args, **kwargs)
        operation = 'rename' if os.name == 'nt' else 'link'
        with patch.object(self.module.os, operation, side_effect=another_writer):
            self.reject('CONFLICT', self.files.write_text, 'race.txt', 'our write', owner='codex',
                        expected_sha256='MISSING')
        self.assertEqual(target.read_bytes(), b'foreign write')

    def test_path_identity_is_rechecked_after_staging(self):
        target = self.put('keep.txt', b'old')
        real_fsync = self.module.os.fsync
        def replace_target_after_sync(fd):
            result = real_fsync(fd)
            if os.fstat(fd).st_size == len(b'new payload'):
                target.unlink()
                target.write_bytes(b'old')
            return result
        with patch.object(self.module.os, 'fsync', side_effect=replace_target_after_sync):
            self.reject('FILE_CHANGED', self.files.write_text, 'keep.txt', 'new payload', owner='codex',
                        expected_sha256=self.digest(b'old'))
        self.assertEqual(target.read_bytes(), b'old')

    def test_binary_artifact_and_manifest_mismatches_are_real_checks(self):
        raw = b'\x00\xffbinary\r\n'
        self.put('artifact.bin', raw)
        entry = self.files.describe_artifact('artifact.bin')
        self.assertEqual(entry, {'path': 'artifact.bin', 'bytes': len(raw), 'sha256': self.digest(raw)})
        self.assertEqual(self.files.verify_artifacts([entry])['verdict'], 'match')
        self.put('artifact.bin', b'changed')
        result = self.files.verify_artifacts([entry])
        self.assertEqual(result['verdict'], 'mismatch')
        self.assertEqual(len(result['mismatches']), 1)
        self.assertEqual(result['mismatches'][0]['path'], 'artifact.bin')

    def test_manifest_rejects_empty_duplicate_malformed_and_unsafe_entries(self):
        self.put('a.bin', b'a')
        entry = self.files.describe_artifact('a.bin')
        for entries in ([], [entry, entry], [{'path': 'a.bin', 'bytes': True, 'sha256': entry['sha256']}],
                        [{'path': '../private', 'bytes': 1, 'sha256': entry['sha256']}],
                        [{'path': 'a.bin', 'bytes': 1, 'sha256': 'bad'}]):
            with self.subTest(entries=entries):
                self.assertEqual(self.files.verify_artifacts(entries)['verdict'], 'mismatch')

    def make_junction(self, source, destination):
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(source), str(destination)],
                                    capture_output=True, timeout=10)
            if result.returncode:
                self.skipTest('Windows junction creation unavailable')
            self.addCleanup(lambda: os.rmdir(source) if source.exists() else None)
        else:
            source.symlink_to(destination, target_is_directory=True)

    def test_junction_parent_and_workspace_root_are_rejected(self):
        destination = self.root / 'actual'
        destination.mkdir()
        (destination / 'a.txt').write_bytes(b'private target')
        alias = self.root / 'alias'
        self.make_junction(alias, destination)
        self.reject('UNSAFE_PATH', self.files.read_text, 'alias/a.txt')
        self.reject('UNSAFE_PATH', self.module.WorkspaceFiles, alias)

    def test_control_junction_is_rejected_before_lease_or_stop_read(self):
        destination = self.root / 'actual'
        destination.mkdir()
        alias = self.root / '.agentbus'
        self.make_junction(alias, destination)
        self.reject('UNSAFE_PATH', self.files.write_text, 'out.txt', 'no', owner='codex',
                    expected_sha256='MISSING')
        self.assertEqual(list(destination.iterdir()), [])

    def test_final_symlink_and_dangling_symlink_are_rejected(self):
        self.put('actual.txt', b'actual')
        for name, target in [('link.txt', 'actual.txt'), ('dangling.txt', 'absent.txt')]:
            try:
                (self.root / name).symlink_to(target)
            except OSError:
                self.skipTest('symlink creation unavailable on this Windows host')
            self.reject('UNSAFE_PATH', self.files.read_text, name)
            self.reject('UNSAFE_PATH', self.files.write_text, name, 'no', owner='codex', expected_sha256='MISSING')


if __name__ == '__main__':
    unittest.main()
