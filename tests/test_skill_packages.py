"""Source: OpenCode, 2026-10-05: counterexamples for the skill package scan.

Provenance: Task 1 of 成果/agentbus/pilots/skill-library-20261005/IMPLEMENTATION_PLAN.md,
task m-1791169024286-6204ed. Isolated local tests only; nothing here is installed,
deployed or executed, and no private configuration or credential is read.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import contextlib
import os
import shutil
import stat
import sys
import tempfile
import threading
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENTRY = b'---\nname: demo\ndescription: demo\n---\n\nbody\n'


class SkillPackageTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('agentbus.skill_packages'),
                             'the candidate skill package scan is missing')
        self.module = importlib.import_module('agentbus.skill_packages')
        self.temp = tempfile.TemporaryDirectory(prefix='agentbus-skill-packages-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def make(self, name='skills/demo'):
        self.put(f'{name}/SKILL.md', ENTRY)
        return name

    def reject(self, code, *args, **kwargs):
        with self.assertRaises(self.module.SkillPackageError) as caught:
            self.module.inspect_package(self.root, *args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def scan(self, name='skills/demo', **kwargs):
        return self.module.inspect_package(self.root, name, **kwargs)

    def canonical(self, manifest):
        return json.dumps({'schema_version': 1, 'files': manifest},
                          ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')

    # --- counterexamples first: the entry file is not the identity -------------
    def test_changing_only_a_script_changes_the_digest(self):
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        before = self.scan(name)
        self.put(f'{name}/scripts/a.py', b'print(2)\r\n')
        after = self.scan(name)
        self.assertNotEqual(before['content_digest'], after['content_digest'])
        self.assertEqual({entry['path'] for entry in after['files']},
                         {'SKILL.md', 'scripts/a.py'})
        self.assertEqual([entry['path'] for entry in after['files']],
                         ['SKILL.md', 'scripts/a.py'])

    def test_utf8_bom_change_in_the_entry_changes_the_digest(self):
        name = self.make()
        plain = self.scan(name)
        self.put(f'{name}/SKILL.md', b'\xef\xbb\xbf' + ENTRY)
        with_bom = self.scan(name)
        self.assertNotEqual(plain['content_digest'], with_bom['content_digest'])
        self.assertTrue(with_bom['execution_metadata']['entry_bom'])
        self.assertFalse(plain['execution_metadata']['entry_bom'])

    def test_line_ending_only_change_is_detected(self):
        name = self.make()
        lf = self.scan(name)
        self.put(f'{name}/SKILL.md', ENTRY.replace(b'\n', b'\r\n'))
        crlf = self.scan(name)
        self.assertNotEqual(lf['content_digest'], crlf['content_digest'])
        self.assertEqual(crlf['execution_metadata']['entry_newline_style'], 'CRLF')

    def test_adding_or_removing_a_resource_changes_the_digest(self):
        name = self.make()
        self.put(f'{name}/references/b.md', b'ref\n')
        with_ref = self.scan(name)
        (self.root / name / 'references' / 'b.md').unlink()
        without = self.scan(name)
        self.assertNotEqual(with_ref['content_digest'], without['content_digest'])

    def test_identical_bytes_at_a_different_path_change_the_digest(self):
        name = self.make()
        self.put(f'{name}/references/one.md', b'same\n')
        first = self.scan(name)
        (self.root / name / 'references' / 'one.md').rename(
            self.root / name / 'references' / 'two.md')
        second = self.scan(name)
        self.assertNotEqual(first['content_digest'], second['content_digest'])
        self.assertEqual(hashlib.sha256(b'same\n').hexdigest(),
                         second['files'][1]['sha256'])

    # --- digest contract --------------------------------------------------------
    def test_digest_matches_the_published_three_field_algorithm(self):
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        self.put(f'{name}/assets/logo.bin', bytes(range(256)))
        result = self.scan(name)
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['digest_covers'], 'paths_and_raw_bytes_only')
        for entry in result['files']:
            self.assertEqual(set(entry), {'path', 'bytes', 'sha256'})
        expected = hashlib.sha256(self.canonical(result['files'])).hexdigest()
        self.assertEqual(result['content_digest'], expected)
        self.assertEqual(result['total_bytes'],
                         sum(entry['bytes'] for entry in result['files']))
        self.assertEqual(result['name'], 'demo')
        self.assertEqual(result['source'], name)

    def test_repeated_scan_is_byte_identical(self):
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        self.assertEqual(self.scan(name), self.scan(name))

    def test_binary_and_unicode_paths_are_supported(self):
        name = self.make()
        self.put(f'{name}/assets/二进制.bin', bytes(range(256)))
        self.put(f'{name}/references/中文 报告.md', '内容😀\n'.encode('utf-8'))
        result = self.scan(name)
        self.assertEqual([entry['path'] for entry in result['files']],
                         ['SKILL.md', 'assets/二进制.bin', 'references/中文 报告.md'])

    def test_manifest_is_sorted_by_the_whole_utf8_path(self):
        # Per-segment tuple sorting puts "a/x" first because b'' < b'-'; the
        # contract asks for the UTF-8 bytes of the whole relative POSIX path,
        # where b'-' (0x2d) < b'/' (0x2f) puts "a-/y" first.
        self.assertLess(self.module._sort_key('a-/y'), self.module._sort_key('a/x'))
        name = self.make()
        (self.root / name / 'a').mkdir()
        (self.root / name / 'a' / 'x').write_bytes(b'1\n')
        (self.root / name / 'a-').mkdir()
        (self.root / name / 'a-' / 'y').write_bytes(b'2\n')
        result = self.scan(name)
        ordered = [entry['path'] for entry in result['files']]
        self.assertEqual(ordered, sorted(ordered, key=self.module._sort_key))
        self.assertEqual(ordered, ['SKILL.md', 'a-/y', 'a/x'])
        self.assertEqual(result['content_digest'],
                         hashlib.sha256(self.canonical(result['files'])).hexdigest())

    def test_windows_style_separators_resolve_to_the_same_manifest(self):
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        forward = self.scan(name)
        backward = self.scan(name.replace('/', '\\'))
        self.assertEqual(forward['content_digest'], backward['content_digest'])

    # --- rejections -------------------------------------------------------------
    def test_empty_directory_is_rejected(self):
        (self.root / 'skills' / 'empty').mkdir(parents=True)
        self.reject('EMPTY_PACKAGE', 'skills/empty')

    def test_missing_skill_entry_is_rejected(self):
        self.put('skills/noskill/scripts/a.py', b'print(1)\n')
        self.reject('NO_SKILL_ENTRY', 'skills/noskill')

    def test_missing_directory_is_rejected(self):
        self.reject('NOT_FOUND', 'skills/absent')

    def test_file_instead_of_directory_is_rejected(self):
        self.put('skills/file.txt', b'x')
        self.reject('NOT_DIRECTORY', 'skills/file.txt')

    def test_non_utf8_entry_is_rejected_without_rewriting_it(self):
        name = self.make()
        raw = b'---\nname: demo\ndescription: \xff\xfe bad\n---\n'
        self.put(f'{name}/SKILL.md', raw)
        self.reject('INVALID_UTF8', name)
        self.assertEqual((self.root / name / 'SKILL.md').read_bytes(), raw)

    def test_escape_and_absolute_paths_are_rejected(self):
        self.reject('UNSAFE_PATH', '../outside')
        self.reject('UNSAFE_PATH', '/etc')
        self.reject('UNSAFE_PATH', 'C:/Windows')
        self.reject('UNSAFE_PATH', 'skills/../../elsewhere')
        self.reject('UNSAFE_PATH', 'skills/trailing ')
        self.reject('UNSAFE_PATH', 'skills/star*name')
        # An interior space is legal on disk and is not a path escape.
        self.put('skills/de mo/SKILL.md', ENTRY)
        self.assertEqual(self.scan('skills/de mo')['name'], 'de mo')

    def test_control_characters_and_reserved_names_are_rejected(self):
        self.make()
        self.reject('UNSAFE_PATH', 'skills/de\x01mo')
        self.reject('UNSAFE_PATH', 'skills/trailing ')
        self.reject('UNSAFE_PATH', 'skills/nul')
        self.reject('UNSAFE_PATH', 'skills/CON.txt')
        self.reject('UNSAFE_PATH', 'skills/star*name')

    def test_private_directory_is_rejected(self):
        self.put('.workbuddy/skills/demo/SKILL.md', ENTRY)
        self.reject('UNSAFE_PATH', '.workbuddy/skills/demo')

    def test_empty_private_subdirectory_is_rejected_not_silently_skipped(self):
        # A private directory holding no regular file used to pass unnoticed,
        # because the old code only tripped on a file inside one. An empty
        # private directory still must not be enumerated or ignored.
        name = self.make()
        (self.root / name / '.workbuddy').mkdir()
        self.reject('UNSAFE_PATH', name)

    def test_deep_empty_private_tree_is_rejected(self):
        name = self.make()
        (self.root / name / '.git' / 'a' / 'b').mkdir(parents=True)
        self.reject('UNSAFE_PATH', name)

    def test_private_directory_in_the_package_path_is_rejected(self):
        self.put('skills/.git/demo/SKILL.md', ENTRY)
        self.reject('UNSAFE_PATH', 'skills/.git/demo')

    def test_private_component_is_refused_before_enumeration(self):
        name = self.make()
        private = self.root / name / '.workbuddy'
        private.mkdir()
        seen: list[str] = []
        original_scandir = self.module.os.scandir

        def watched(path, *args, **kwargs):
            seen.append(str(path))
            return original_scandir(path, *args, **kwargs)

        with mock.patch.object(self.module.os, 'scandir', watched):
            with self.assertRaises(self.module.SkillPackageError) as caught:
                self.scan(name)
        self.assertEqual(caught.exception.code, 'UNSAFE_PATH')
        self.assertFalse([entry for entry in seen if str(private) in entry],
                         'a private directory was enumerated before being refused')

    def test_symlink_entry_is_rejected(self):
        name = self.make()
        target = self.put('elsewhere/target.md', b'outside\n')
        link = self.root / name / 'link.md'
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('symlinks are unavailable on this platform')
        self.require_real_link(link)
        self.reject('UNSAFE_PATH', name)

    def test_symlinked_package_directory_is_rejected(self):
        real = self.make('skills/real')
        try:
            os.symlink(self.root / real, self.root / 'skills' / 'linked')
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('symlinks are unavailable on this platform')
        self.require_real_link(self.root / 'skills' / 'linked')
        self.reject('UNSAFE_PATH', 'skills/linked')

    def require_real_link(self, link):
        """Codex fixture correction, 2026-10-05: a host's successful return
        does not prove it created a link. Original UNSAFE_PATH assertions run
        only after a real symlink/reparse point is observed by lstat.
        """
        info = link.lstat()
        if not (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            self.skipTest('host returned without creating real symlink/reparse point')

    def test_case_only_duplicate_names_are_reported_as_the_single_file_on_disk(self):
        # On a case-insensitive filesystem the OS silently collapses the two
        # names, so the honest answer is one file, not a silent merge of two.
        if os.name != 'nt':
            self.skipTest('case-insensitive collapse needs a case-insensitive filesystem')
        name = self.make()
        self.put(f'{name}/refs/File.md', b'a\n')
        try:
            self.put(f'{name}/refs/file.md', b'b\n')
        except OSError:
            self.skipTest('filesystem refused the case-only duplicate')
        result = self.scan(name)
        folded = [entry['path'] for entry in result['files']]
        self.assertEqual(len(folded), len({path.casefold() for path in folded}))
        self.assertEqual(result['files'][-1]['sha256'], hashlib.sha256(b'b\n').hexdigest())

    def test_unicode_normalisation_collisions_are_rejected(self):
        # NFC and NFD spellings are distinct on NTFS, so the scanner itself has
        # to refuse them; it cannot rely on the filesystem collapsing them.
        name = self.make()
        composed = unicodedata.normalize('NFC', 'refs/café.md')
        decomposed = unicodedata.normalize('NFD', 'refs/café.md')
        if composed == decomposed:
            self.skipTest('this platform normalises filenames on write')
        self.put(f'{name}/{composed}', b'x\n')
        self.put(f'{name}/{decomposed}', b'y\n')
        self.reject('PATH_COLLISION', name)

    def test_collision_helper_refuses_case_only_duplicates(self):
        with self.assertRaises(self.module.SkillPackageError) as caught:
            self.module._check_collisions(['refs/File.md', 'refs/file.md'])
        self.assertEqual(caught.exception.code, 'PATH_COLLISION')
        with self.assertRaises(self.module.SkillPackageError) as caught:
            self.module._check_collisions(['SKILL.md', 'SKILL.md'])
        self.assertEqual(caught.exception.code, 'PATH_COLLISION')

    def test_collision_helper_refuses_directory_only_duplicates(self):
        # Two directories that differ only by case can each hide a subtree, so a
        # file-only comparison cannot see the clash. These paths share every
        # character after the differing one, so only a directory-level entry can
        # catch them: the files themselves are never both present in one listing.
        with self.assertRaises(self.module.SkillPackageError) as caught:
            self.module._check_collisions(['Dir', 'dir'])
        self.assertEqual(caught.exception.code, 'PATH_COLLISION')
        with self.assertRaises(self.module.SkillPackageError) as caught:
            self.module._check_collisions(['Refs/inner.md', 'refs/inner.md'])
        self.assertEqual(caught.exception.code, 'PATH_COLLISION')
        # Distinct directories and distinct files must stay acceptable.
        self.module._check_collisions(['Refs/inner.md', 'refs/other.md'])

    def test_collision_check_includes_directories_found_during_the_scan(self):
        name = self.make()
        (self.root / name / 'Refs').mkdir()
        scanner_paths: list[str] = []
        original = self.module._check_collisions

        def spy(entries):
            scanner_paths.extend(entries)
            return original(entries)

        with mock.patch.object(self.module, '_check_collisions', spy):
            self.scan(name)
        self.assertIn('Refs', scanner_paths)
        self.assertIn('SKILL.md', scanner_paths)

    # --- bounds ------------------------------------------------------------------
    def test_file_count_limit_is_enforced(self):
        name = self.make()
        for index in range(5):
            self.put(f'{name}/refs/f{index}.md', b'x')
        self.reject('TOO_LARGE', name, max_files=3)

    def test_single_file_size_limit_is_enforced(self):
        name = self.make()
        self.put(f'{name}/assets/big.bin', b'0' * 4096)
        self.reject('TOO_LARGE', name, max_file_bytes=1024)

    def test_total_byte_limit_is_enforced(self):
        name = self.make()
        self.put(f'{name}/assets/big.bin', b'0' * 4096)
        self.reject('TOO_LARGE', name, max_total_bytes=2048)

    def test_directory_node_limit_is_enforced(self):
        # max_dir_bytes used to be accepted and then ignored, so a package of
        # empty directories could expand without any bound. The root counts as a
        # node, so a limit of 1 leaves room for the root alone.
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        self.assertEqual(self.scan(name, max_dir_bytes=2)['name'], 'demo')
        self.reject('TOO_LARGE', name, max_dir_bytes=1)

    def test_empty_directory_tree_cannot_expand_past_the_node_limit(self):
        name = self.make()
        node = self.root / name / 'empty'
        for index in range(6):
            node = node / f'd{index}'
        node.mkdir(parents=True)
        self.reject('TOO_LARGE', name, max_dir_bytes=4, max_files=64)

    def test_single_directory_entry_count_is_bounded_while_listing(self):
        name = self.make()
        assets = self.root / name / 'assets'
        assets.mkdir()
        for index in range(8):
            (assets / f'f{index}.bin').write_bytes(b'0')
        with self.assertRaises(self.module.SkillPackageError) as caught:
            self.scan(name, max_dir_bytes=4)
        self.assertEqual(caught.exception.code, 'TOO_LARGE')

    def test_listing_stops_at_the_limit_without_materialising_everything(self):
        name = self.make()
        assets = self.root / name / 'assets'
        assets.mkdir()
        for index in range(40):
            (assets / f'f{index}.bin').write_bytes(b'0')
        yielded: dict[str, int] = {}
        original_scandir = self.module.os.scandir

        @contextlib.contextmanager
        def counting(path, *args, **kwargs):
            # A context manager, like the real scandir, so the scan keeps working.
            with original_scandir(path, *args, **kwargs) as entries:
                class Traced:
                    def __iter__(inner):
                        for entry in entries:
                            yielded[str(path)] = yielded.get(str(path), 0) + 1
                            yield entry
                yield Traced()

        with mock.patch.object(self.module.os, 'scandir', counting):
            with self.assertRaises(self.module.SkillPackageError):
                self.scan(name, max_dir_bytes=6)
        assets_key = str(self.root / name / 'assets')
        self.assertLessEqual(yielded.get(assets_key, 0), 7,
                             'listing consumed far more entries than the limit allows')
        self.assertGreater(len(yielded), 0)

    def test_total_byte_limit_refuses_before_reading_the_oversized_file(self):
        name = self.make()
        self.put(f'{name}/assets/big.bin', b'0' * 200000)
        read_paths: list[str] = []
        original = self.module.WorkspaceFiles.describe_artifact

        def watched(self_files, path):
            read_paths.append(path)
            return original(self_files, path)

        with mock.patch.object(self.module.WorkspaceFiles, 'describe_artifact', watched):
            with self.assertRaises(self.module.SkillPackageError) as caught:
                self.scan(name, max_total_bytes=4096)
        self.assertEqual(caught.exception.code, 'TOO_LARGE')
        self.assertNotIn(f'{name}/assets/big.bin', read_paths,
                         'the oversized file was read before the budget refused it')

    def test_invalid_limits_are_rejected(self):
        name = self.make()
        for key in ('max_files', 'max_total_bytes', 'max_file_bytes', 'max_dir_bytes'):
            with self.assertRaises(self.module.SkillPackageError) as caught:
                self.scan(name, **{key: 0})
            self.assertEqual(caught.exception.code, 'INVALID_ARGUMENT')
        with self.assertRaises(self.module.SkillPackageError):
            self.scan(name, max_files=True)

    def test_nesting_depth_is_bounded(self):
        name = self.make()
        deep = self.root / name
        for index in range(40):
            deep = deep / 'd'
        deep.mkdir(parents=True)
        (deep / 'leaf.md').write_bytes(b'leaf')
        self.reject('TOO_DEEP', name)

    # --- concurrency -------------------------------------------------------------
    def mutate_after_read(self, mutate, call):
        """Wrap the reader so a mutation lands inside the scan window.

        ``call`` is the 1-based read after which the mutation runs, so a test can
        place the change before or after the affected subtree was enumerated.

        Codex internal integration migration, 2026-10-05: the production reader
        is now _Scanner._bounded_artifact. Only the injection point changes;
        original mutations/error assertions remain and author bytes are archived.
        """
        original = self.module._Scanner._bounded_artifact
        state = {'calls': 0}

        def wrapper(self_scanner, path, remaining):
            entry = original(self_scanner, path, remaining)
            state['calls'] += 1
            if state['calls'] == call:
                mutate()
            return entry

        return original, wrapper

    def test_rewrite_between_passes_is_rejected_not_a_stale_digest(self):
        name = self.make()
        entry_file = self.put(f'{name}/SKILL.md', ENTRY)
        original, wrapper = self.mutate_after_read(
            lambda: entry_file.write_bytes(ENTRY + b'changed\n'), 1)
        with mock.patch.object(self.module._Scanner, '_bounded_artifact', wrapper):
            with self.assertRaises(self.module.SkillPackageError) as caught:
                self.scan(name)
        self.assertEqual(caught.exception.code, 'FILE_CHANGED')
        self.assertEqual(entry_file.read_bytes(), ENTRY + b'changed\n')
        self.assertIs(self.module._Scanner._bounded_artifact, original)

    def test_file_swapped_for_a_directory_is_rejected_not_silently_skipped(self):
        # The scanner enumerates first and reads later. If a regular file becomes
        # a directory in that window, the manifest would understate the package,
        # so the identity re-check must refuse instead of returning it.
        name = self.make()
        target = self.put(f'{name}/scripts/a.py', b'print(1)\n')

        def swap():
            target.unlink()
            target.mkdir()
            (target / 'inner.md').write_bytes(b'inner\n')

        # Mutate after the last enumerated file so the change lands after the
        # directory listing was taken.
        original, wrapper = self.mutate_after_read(swap, 2)
        with mock.patch.object(self.module._Scanner, '_bounded_artifact', wrapper):
            with self.assertRaises(self.module.SkillPackageError) as caught:
                self.scan(name)
        self.assertIn(caught.exception.code, {'FILE_CHANGED', 'UNSAFE_PATH'})
        self.assertIs(self.module._Scanner._bounded_artifact, original)

    def test_extra_file_added_mid_scan_is_rejected(self):
        name = self.make()
        added = self.put(f'{name}/scripts/a.py', b'print(1)\n').parent
        late = added / 'late.py'

        def inject():
            late.write_bytes(b'print(3)\n')

        original, wrapper = self.mutate_after_read(inject, 2)
        with mock.patch.object(self.module._Scanner, '_bounded_artifact', wrapper):
            with self.assertRaises(self.module.SkillPackageError) as caught:
                self.scan(name)
        self.assertEqual(caught.exception.code, 'FILE_CHANGED')
        self.assertIs(self.module._Scanner._bounded_artifact, original)

    def test_repeated_scans_never_return_a_torn_manifest(self):
        name = self.make()
        target = self.put(f'{name}/scripts/a.py', b'print(1)\n')
        stop = threading.Event()

        def flip():
            toggle = False
            while not stop.is_set():
                try:
                    target.write_bytes(b'print(9)\n' if toggle else b'print(1)\n')
                except OSError:
                    break
                toggle = not toggle

        worker = threading.Thread(target=flip, daemon=True)
        worker.start()
        try:
            for _ in range(40):
                try:
                    self.scan(name)
                except self.module.SkillPackageError as exc:
                    self.assertIn(exc.code, {'FILE_CHANGED', 'IO_ERROR'})
        finally:
            stop.set()
            worker.join(timeout=5)
        quiet = self.scan(name)
        self.assertEqual(quiet, self.scan(name))

    # --- scan root identity ------------------------------------------------------
    def test_scan_root_identity_and_version_are_recorded(self):
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        captured: dict = {}
        original = self.module._Scanner.verify_unchanged

        def spy(self_scanner):
            captured['identities'] = dict(self_scanner.identities)
            captured['versions'] = dict(self_scanner.versions)
            return original(self_scanner)

        with mock.patch.object(self.module._Scanner, 'verify_unchanged', spy):
            self.scan(name)
        package_root = (self.root / name)
        self.assertIn(package_root, captured['identities'],
                      'the scan root itself was never recorded')
        self.assertIn(package_root, captured['versions'])
        self.assertIn(package_root / 'scripts', captured['identities'])

    def test_root_replaced_between_enumeration_and_verification_is_refused(self):
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        original, wrapper = self.mutate_after_read(
            lambda: self.replace_root_with_copy(name), 2)
        with mock.patch.object(self.module._Scanner, '_bounded_artifact', wrapper):
            with self.assertRaises(self.module.SkillPackageError) as caught:
                self.scan(name)
        self.assertIn(caught.exception.code, {'FILE_CHANGED', 'UNSAFE_PATH', 'IO_ERROR'})

    def replace_root_with_copy(self, name):
        """Move the package aside and copy it back so the root inode changes."""
        source = self.root / name
        moved = self.root / (name + '-moved')
        shutil.copytree(source, moved)
        shutil.rmtree(source)
        shutil.copytree(moved, source)
        shutil.rmtree(moved)

    def test_root_timestamp_only_change_is_refused(self):
        if os.name == 'nt':
            self.skipTest('directory mtime granularity differs on this platform')
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        package_root = self.root / name
        original = self.module._Scanner.verify_unchanged

        def touch_then_verify(self_scanner):
            os.utime(package_root, (1_000_000, 1_000_000))
            return original(self_scanner)

        with mock.patch.object(self.module._Scanner, 'verify_unchanged', touch_then_verify):
            with self.assertRaises(self.module.SkillPackageError) as caught:
                self.scan(name)
        self.assertEqual(caught.exception.code, 'FILE_CHANGED')

    # --- honesty about what the digest proves ------------------------------------
    def test_result_does_not_claim_discovery_or_invocation(self):
        name = self.make()
        result = self.scan(name)
        self.assertEqual(result['discovered'], 'unverified')
        self.assertEqual(result['invoked'], 'unverified')
        self.assertTrue(result['read_only'])
        self.assertFalse(result['installed'])
        metadata = result['execution_metadata']
        self.assertEqual(metadata['host_discovery'], 'unverified')
        self.assertEqual(metadata['host_invocation'], 'unverified')
        self.assertEqual(metadata['executable_bit'], 'unknown')
        self.assertEqual(metadata['interpreter'], 'unknown')

    def test_scripts_are_listed_but_not_executed(self):
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'raise SystemExit("must not run")\n')
        result = self.scan(name)
        self.assertIn('scripts/a.py', result['execution_metadata']['script_like_files'])
        self.assertIn('scripts/a.py', result['execution_metadata']['scripts_not_executed'])

    def test_read_only_scan_creates_no_control_state(self):
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        self.scan(name)
        self.assertFalse((self.root / '.agentbus').exists())

    def test_permission_only_change_is_outside_the_digest(self):
        name = self.make()
        self.put(f'{name}/scripts/a.py', b'print(1)\n')
        before = self.scan(name)
        script = self.root / name / 'scripts' / 'a.py'
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        after = self.scan(name)
        self.assertEqual(before['content_digest'], after['content_digest'])
        self.assertEqual(after['execution_metadata']['executable_bit'], 'unknown')
        self.assertEqual(after['execution_metadata']['posix_mode'], 'unknown')


if __name__ == '__main__':
    unittest.main()
