"""Source: Codex, 2026-10-03. Isolated source-export contract checks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from tools import export_source as release
except ImportError:
    release = None


LICENSE_NAMES = (
    'annotated-types-LICENSE.txt', 'codex-LICENSE.txt', 'codex-NOTICE.txt',
    'packaging-LICENSE.APACHE.txt', 'packaging-LICENSE.BSD.txt',
    'packaging-LICENSE.txt', 'pydantic-LICENSE.txt', 'pydantic-core-LICENSE.txt',
    'pyinstaller-COPYING.txt', 'python-LICENSE.txt', 'ratatui-LICENSE.txt',
    'ripgrep-LICENSE-MIT.txt', 'ripgrep-UNLICENSE.txt', 'tcl-license.terms',
    'tk-license.terms', 'typing-extensions-LICENSE.txt',
    'typing-inspection-LICENSE.txt',
    'pywebview-LICENSE.txt', 'pythonnet-LICENSE.txt', 'clr-loader-LICENSE.txt',
    'cffi-LICENSE.txt', 'bottle-LICENSE.txt', 'proxy-tools-LICENSE.txt',
    'pycparser-LICENSE.txt', 'webview2-sdk-LICENSE.txt',
    'altgraph-LICENSE.txt',
    'pefile-LICENSE.txt',
    'pywin32-ctypes-LICENSE.txt',
    'setuptools-LICENSE.txt',
    'setuptools-config-NOTICE.txt',
    'setuptools-validate-pyproject-NOTICE.txt',
    'setuptools-vendor-autocommand-LICENSE.txt',
    'setuptools-vendor-backports-tarfile-LICENSE.txt',
    'setuptools-vendor-importlib-metadata-LICENSE.txt',
    'setuptools-vendor-jaraco-context-LICENSE.txt',
    'setuptools-vendor-jaraco-functools-LICENSE.txt',
    'setuptools-vendor-jaraco-text-LICENSE.txt',
    'setuptools-vendor-more-itertools-LICENSE.txt',
    'setuptools-vendor-packaging-LICENSE.APACHE.txt',
    'setuptools-vendor-packaging-LICENSE.BSD.txt',
    'setuptools-vendor-packaging-LICENSE.txt',
    'setuptools-vendor-platformdirs-LICENSE.txt',
    'setuptools-vendor-tomli-LICENSE.txt',
    'setuptools-vendor-wheel-LICENSE.txt',
    'setuptools-vendor-zipp-LICENSE.txt',
)


class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(release, 'Source export tool is not implemented')
        self.temp = tempfile.TemporaryDirectory(prefix='agentbus-source-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'checkout'
        self.root.mkdir()
        self.output = self.base / 'source.zip'
        for name in ('entry.py', 'entry_tower.py', 'requirements-build.txt',
                     'SOURCE_LICENSE_STATUS.md', '.gitignore'):
            self.write(name, b'public root\n')
        self.write('agentbus/__init__.py', b'__version__ = "fixture"\n')
        self.write('agentbus/__main__.py', b'print("fixture")\n')
        self.write('tests/test_fixture.py', b'# isolated fixture\n')
        for name in ('plan', 'review', 'continuation'):
            self.write(f'schemas/{name}.schema.json', b'{"type":"object"}\n')
        self.write('skills/agentbus-team/SKILL.md', b'public skill\n')
        for name in ('README', 'ECOSYSTEM', 'RELEASE', 'SECURITY'):
            self.write(f'docs/public/{name}.md', f'public {name}\n'.encode())
        for name in ('export_source', 'check_offline', 'build_release',
                     'smoke_frozen', 'handover_screen', 'skill_manifest_audit',
                     'check_formal_sync', 'generate_build_manifest',
                     'delivery_self_proof', 'archived_binary_inventory', 'deploy_formal_release'):
            self.write(f'tools/{name}.py', f'# {name}\n'.encode())
        self.write('.github/workflows/offline-tests.yml', b'name: offline\n')
        self.write('share_assets/开始使用.md', '公开使用说明\n'.encode())
        self.write('share_assets/THIRD_PARTY_NOTICES.md', b'public notices\n')
        self.provenance = {
            'schema_version': 1, 'collected_at': '2026-10-03',
            'coverage': 'isolated fixture licenses', 'files': [],
        }
        for name in LICENSE_NAMES:
            blob = f'license fixture: {name}\n'.encode()
            self.write(f'share_assets/licenses/{name}', blob)
            self.provenance['files'].append({
                'path': f'licenses/{name}', 'component': name, 'version': 'fixture',
                'source': 'isolated test fixture',
                'sha256': hashlib.sha256(blob).hexdigest(),
            })
        self.save_provenance()

    def write(self, name, blob):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return path

    def save_provenance(self):
        self.write('share_assets/licenses/provenance.json',
                   json.dumps(self.provenance).encode())

    def archive(self):
        result = release.export_source(self.root, self.output)
        with zipfile.ZipFile(self.output) as package:
            content = {name: package.read(name) for name in package.namelist()}
        return result, content

    def test_whitelist_excludes_secrets_and_maps_public_readme(self):
        secret = b'EXCLUDED_PRIVATE_CANARY_20261003'
        for name in ('README.md', 'build/cache.py', 'dist/program.exe',
                     'pilots/history.md', 'venv/secret.py',
                     '.agentbus/config.json',
                     '.git/config', 'mcp-agentbus.json', '.lease.json',
                     'tools/live_codex_check.py', 'docs/public/PRIVATE.md',
                     'share_assets/private.txt', 'share_assets/licenses/private.txt',
                     'tests/nested/private.py', 'agentbus/__pycache__/private.py'):
            self.write(name, secret)
        self.write('agentbus/additional.py', b'# allowed module\n')
        self.write('tests/test_additional.py', b'# allowed test\n')
        _, content = self.archive()
        self.assertEqual(content['README.md'], b'public README\n')
        self.assertIn('agentbus/additional.py', content)
        self.assertIn('tests/test_additional.py', content)
        self.assertIn('share_assets/licenses/tcl-license.terms', content)
        self.assertEqual(content['docs/public/README.md'], b'public README\n')
        for blob in content.values():
            self.assertNotIn(secret, blob)
        self.assertNotIn('docs/public/PRIVATE.md', content)
        self.assertNotIn('tools/live_codex_check.py', content)

    def test_optional_git_attributes_preserve_release_checkout_bytes(self):
        self.write('.gitattributes', b'* -text\n')
        _, content = self.archive()
        self.assertEqual(content['.gitattributes'], b'* -text\n')

    def test_dependency_audit_tests_ship_their_explicit_tool(self):
        self.write('tests/test_build_dependency_audit.py',b'# source release consumer\n')
        self.output.write_bytes(b'previous source archive')
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root,self.output)
        self.assertEqual(self.output.read_bytes(),b'previous source archive')
        blob=b'# explicit dependency audit fixture\n'
        self.write('tools/build_dependency_audit.py',blob)
        _,content=self.archive()
        self.assertEqual(content['tools/build_dependency_audit.py'],blob)

    def test_optional_owner_license_is_exported_without_rewriting(self):
        license_bytes=b'Apache License\nVersion 2.0\noriginal test bytes\r\n'
        self.write('LICENSE',license_bytes)
        _,content=self.archive()
        self.assertEqual(content['LICENSE'],license_bytes)
        manifest=json.loads(content['source-manifest.json'])
        self.assertEqual(manifest['files']['LICENSE']['sha256'],hashlib.sha256(license_bytes).hexdigest())

    def test_web_desktop_exports_exact_assets_without_preview_snapshot(self):
        self.write('agentbus/web_desktop.py', b'# new web desktop\n')
        for name in ('index.html', 'app.css', 'app.js'):
            self.write('agentbus/web_assets/' + name, name.encode())
        entry = bytes((16, 16, 0, 0, 1, 0, 32, 0)) + (12).to_bytes(4, 'little') + (22).to_bytes(4, 'little')
        self.write(release.APP_ICON_FILE, b'\x00\x00\x01\x00\x01\x00' + entry + b'fixture-icon')
        self.write('agentbus/web_assets/preview.json', b'PRIVATE_PREVIEW_CANARY')
        _, content = self.archive()
        for name in ('index.html', 'app.css', 'app.js'):
            self.assertEqual(content['agentbus/web_assets/' + name], name.encode())
        self.assertIn(release.APP_ICON_FILE, content)
        self.assertNotIn('agentbus/web_assets/preview.json', content)

    def test_missing_web_asset_preserves_previous_archive(self):
        self.write('agentbus/web_desktop.py', b'# new web desktop\n')
        self.write('agentbus/web_assets/index.html', b'local page')
        self.write('agentbus/web_assets/app.css', b'local styles')
        self.output.write_bytes(b'existing archive')
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'existing archive')

    def test_excluded_trees_and_private_readme_are_never_accessed(self):
        forbidden = {self.root / name for name in (
            'build', 'build_other', 'dist', 'dist_other', 'pilots', 'venv',
            '.workbuddy', '.agentbus', '.git', 'README.md',
        )}
        for path in forbidden:
            if path.name == '.workbuddy':
                # Keep the private-path canary virtual: no WorkBuddy directory,
                # including an isolated fixture, is read or created by this test.
                continue
            if path.name == 'README.md':
                path.write_bytes(b'private canary')
            else:
                path.mkdir()
                (path / 'secret.txt').write_bytes(b'private canary')
        original_open, original_lstat = Path.open, Path.lstat
        original_stat, original_scandir = Path.stat, os.scandir

        def allowed(path):
            value = Path(os.path.abspath(path))
            if any(value == banned or banned in value.parents for banned in forbidden):
                raise AssertionError(f'excluded path accessed: {value.name}')

        def guarded_open(path, *args, **kwargs):
            allowed(path)
            return original_open(path, *args, **kwargs)

        def guarded_lstat(path, *args, **kwargs):
            allowed(path)
            return original_lstat(path, *args, **kwargs)

        def guarded_stat(path, *args, **kwargs):
            allowed(path)
            return original_stat(path, *args, **kwargs)

        def guarded_scandir(path):
            allowed(path)
            if Path(path) == self.root:
                raise AssertionError('whole checkout must never be scanned')
            return original_scandir(path)

        with patch.object(Path, 'open', guarded_open), \
             patch.object(Path, 'lstat', guarded_lstat), \
             patch.object(Path, 'stat', guarded_stat), \
             patch('os.scandir', guarded_scandir):
            result = release.export_source(self.root, self.output)
        self.assertTrue(self.output.is_file())
        self.assertGreater(result['file_count'], 30)

    def test_manifest_hashes_match_every_exported_file_without_source_root(self):
        result, content = self.archive()
        manifest = json.loads(content.pop('source-manifest.json'))
        self.assertEqual(manifest['format'], 'agentbus.source-package')
        self.assertIn('tools/skill_manifest_audit.py', content)
        self.assertEqual(set(manifest['files']), set(content))
        for name, blob in content.items():
            self.assertEqual(manifest['files'][name], {
                'sha256': hashlib.sha256(blob).hexdigest(), 'size': len(blob),
            })
        self.assertEqual(result['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertNotIn(str(self.root), json.dumps(result['manifest']))

    def test_zip_bytes_are_stable_after_source_mtime_changes(self):
        release.export_source(self.root, self.output)
        before = self.output.read_bytes()
        os.utime(self.root / 'entry.py', (1800000000, 1800000000))
        second = self.base / 'other.zip'
        release.export_source(self.root, second)
        self.assertEqual(before, second.read_bytes())
        with zipfile.ZipFile(second) as package:
            self.assertEqual(package.namelist(), sorted(package.namelist()))
            self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0)
                                for item in package.infolist()))

    def test_root_readme_links_resolve_without_changing_public_source(self):
        source = (b'[ecosystem](ECOSYSTEM.md) [release](RELEASE.md) '
                  b'[security](SECURITY.md) '
                  b'[license](../../SOURCE_LICENSE_STATUS.md)\n')
        self.write('docs/public/README.md', source)
        _, content = self.archive()
        self.assertEqual(content['README.md'],
                         b'[ecosystem](docs/public/ECOSYSTEM.md) '
                         b'[release](docs/public/RELEASE.md) '
                         b'[security](docs/public/SECURITY.md) '
                         b'[license](SOURCE_LICENSE_STATUS.md)\n')
        self.assertEqual(content['docs/public/README.md'], source)
        self.assertEqual((self.root / 'docs/public/README.md').read_bytes(), source)

    def test_unsafe_provenance_paths_are_rejected_before_external_read(self):
        for bad in ('../private.txt', 'licenses/../private.txt',
                    'licenses\\private.txt', '/private.txt', 'licenses/C:private.txt'):
            with self.subTest(path=bad):
                self.provenance['files'][0]['path'] = bad
                self.save_provenance()
                with self.assertRaises(release.SourceReleaseError):
                    release.export_source(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_provenance_requires_exact_unique_license_set(self):
        original = json.loads(json.dumps(self.provenance))
        cases = [
            {**original, 'files': original['files'][:-1]},
            {**original, 'files': original['files'] + [original['files'][0]]},
            {**original, 'files': {}},
            {**original, 'files': original['files'] + [{
                'path': 'licenses/unexpected-LICENSE.txt', 'component': 'unexpected',
                'version': '1', 'source': 'controlled fixture', 'sha256': '0' * 64}]},
            {**original, 'schema_version': True},
            {**original, 'files': [{**item, 'path': 'licenses/private.txt'}
                                  if index == 0 else item
                                  for index, item in enumerate(original['files'])]},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.provenance = value
                self.save_provenance()
                with self.assertRaises(release.SourceReleaseError):
                    release.export_source(self.root, self.output)

    def test_license_hash_mismatch_is_rejected(self):
        self.write('share_assets/licenses/python-LICENSE.txt', b'changed license')
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_required_resource_does_not_replace_existing_archive(self):
        self.output.write_bytes(b'existing release')
        (self.root / 'schemas/continuation.schema.json').unlink()
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'existing release')
        self.assertEqual(list(self.base.glob('.agentbus-source-*')), [])

    def test_replace_failure_keeps_existing_archive_and_removes_temporary(self):
        self.output.write_bytes(b'existing release')
        with patch('os.replace', side_effect=OSError('injected replace failure')):
            with self.assertRaises(release.SourceReleaseError):
                release.export_source(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'existing release')
        self.assertEqual(list(self.base.glob('.agentbus-source-*')), [])

    def test_oversized_source_is_rejected_before_output(self):
        self.write('agentbus/large.py', b'x' * (2 * 1024 * 1024 + 1))
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_total_size_limit_rejects_archive(self):
        for index in range(33):
            self.write(f'agentbus/large{index}.py', b'x' * (2 * 1024 * 1024))
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_allowed_source_name_must_be_regular_file(self):
        (self.root / 'agentbus/not_a_file.py').mkdir()
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root, self.output)

    def test_reparse_point_is_rejected_before_reading(self):
        target = self.root / 'agentbus/__main__.py'
        original_lstat = Path.lstat

        def reparse_lstat(path, *args, **kwargs):
            result = original_lstat(path, *args, **kwargs)
            if path == target:
                return SimpleNamespace(st_mode=result.st_mode,
                                       st_file_attributes=0x400)
            return result

        with patch.object(Path, 'lstat', reparse_lstat):
            with self.assertRaises(release.SourceReleaseError):
                release.export_source(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_symlink_source_file_is_rejected(self):
        outside = self.base / 'outside-secret.txt'
        outside.write_bytes(b'outside canary')
        link = self.root / 'agentbus/linked.py'
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest('symbolic link privilege unavailable: ' + str(exc))
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_symlink_resource_directory_is_rejected(self):
        outside = self.base / 'outside'
        outside.mkdir()
        assets = self.root / 'share_assets'
        assets.rename(self.base / 'original-assets')
        try:
            assets.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest('symbolic link privilege unavailable: ' + str(exc))
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root, self.output)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction contract')
    def test_junction_source_parent_is_rejected_before_checkout_access(self):
        # Source: Codex, 2026-10-04. Only isolated fixture directories are linked.
        actual_parent = self.base / 'actual-parent'
        actual_parent.mkdir()
        self.root.rename(actual_parent / 'checkout')
        parent = self.base / 'linked-parent'
        result = subprocess.run(
            ['cmd', '/c', 'mklink', '/J', str(parent), str(actual_parent)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        )
        if result.returncode:
            self.skipTest('directory junction unavailable')
        self.addCleanup(lambda: os.rmdir(parent) if parent.is_junction() else None)
        source = parent / 'checkout'
        original_lstat = Path.lstat

        def guarded_lstat(path, *args, **kwargs):
            if path == source or source in path.parents:
                raise AssertionError('checkout beneath junction must never be accessed')
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, 'lstat', guarded_lstat):
            with self.assertRaises(release.SourceReleaseError):
                release.export_source(source, self.output)
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction contract')
    def test_source_parent_replaced_after_validation_is_rejected_before_read(self):
        # Source: Codex, 2026-10-04. Replace only this test's temporary parent.
        parent = self.base / 'source-parent'
        parent.mkdir()
        source = parent / 'checkout'
        self.root.rename(source)
        moved_parent = self.base / 'moved-parent'
        original_destination, original_open = release._destination, Path.open

        def replace_parent(output):
            original_destination(output)
            parent.rename(moved_parent)
            result = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(parent), str(moved_parent)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            )
            if result.returncode:
                self.skipTest('directory junction unavailable')
            self.addCleanup(lambda: os.rmdir(parent) if parent.is_junction() else None)

        def guarded_open(path, *args, **kwargs):
            if path == source or source in path.parents:
                raise AssertionError('source beneath replaced parent must never be read')
            return original_open(path, *args, **kwargs)

        with patch.object(release, '_destination', replace_parent), \
             patch.object(Path, 'open', guarded_open):
            with self.assertRaises(release.SourceReleaseError):
                release.export_source(source, self.output)
        self.assertFalse(self.output.exists())

    def test_literal_private_root_is_rejected_before_normalization_or_inspection(self):
        # A virtual path canary: never create or inspect a private directory.
        source = self.root / '.WoRkBuDdY' / '..'
        with patch.object(Path, 'lstat', side_effect=AssertionError('must reject before lstat')):
            with self.assertRaises(release.SourceReleaseError):
                release.export_source(source, self.output)
        self.assertFalse(self.output.exists())

    def test_validate_source_root_returns_absolute_path_without_resolving_links(self):
        validate = getattr(release, 'validate_source_root', None)
        self.assertTrue(callable(validate), 'Public source-root validation is required')
        previous_cwd = Path.cwd()
        try:
            os.chdir(self.base)
            with patch.object(Path, 'resolve', side_effect=AssertionError('must not follow links')):
                self.assertEqual(validate(Path('checkout')), self.root)
        finally:
            os.chdir(previous_cwd)

    def test_private_unc_share_is_rejected_before_inspection(self):
        # Source: Codex, 2026-10-04. This UNC name is virtual; never contact it.
        source = Path(r'\\agentbus-fixture-server\.WoRkBuDdY\checkout')
        with patch.object(Path, 'lstat', side_effect=AssertionError('must reject before lstat')):
            with self.assertRaises(release.SourceReleaseError):
                release.validate_source_root(source)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction contract')
    def test_junction_resource_directory_is_rejected_without_external_read(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'private.txt').write_bytes(b'outside junction canary')
        assets = self.root / 'share_assets'
        assets.rename(self.base / 'original-assets')
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(assets), str(outside)],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        if result.returncode:
            self.skipTest('directory junction unavailable')
        self.addCleanup(lambda: os.rmdir(assets) if assets.is_junction() else None)
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path == outside or outside in path.parents:
                raise AssertionError('junction target must never be read')
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', guarded_open):
            with self.assertRaises(release.SourceReleaseError):
                release.export_source(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_output_symlink_parent_is_rejected(self):
        destination = self.base / 'linked-output'
        destination.mkdir()
        original_lstat = Path.lstat

        def reparse_lstat(path, *args, **kwargs):
            result = original_lstat(path, *args, **kwargs)
            if path == destination:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755,
                                       st_file_attributes=0x400)
            return result

        with patch.object(Path, 'lstat', reparse_lstat):
            with self.assertRaises(release.SourceReleaseError):
                release.export_source(self.root, destination / 'source.zip')
        self.assertEqual(list(destination.iterdir()), [])

    def test_provenance_duplicate_json_keys_are_rejected(self):
        value = json.dumps(self.provenance)
        value = value.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1')
        self.write('share_assets/licenses/provenance.json', value.encode())
        with self.assertRaises(release.SourceReleaseError):
            release.export_source(self.root, self.output)

    def test_source_replaced_between_stat_and_open_is_rejected_before_read(self):
        target = self.root / 'agentbus/__main__.py'
        replacement = self.base / 'replacement.py'
        replacement.write_bytes(b'outside replacement canary')
        original_open = Path.open

        def swapped_open(path, *args, **kwargs):
            if path == target and args == ('rb',):
                target.rename(self.base / 'original-main.py')
                replacement.rename(target)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', swapped_open):
            with self.assertRaises(release.SourceReleaseError):
                release.export_source(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_cli_exports_without_importing_runtime_or_current_user_state(self):
        exporter = Path(__file__).resolve().parents[1] / 'tools/export_source.py'
        process = subprocess.run(
            [sys.executable, '-X', 'utf8', str(exporter), '--root', str(self.root),
             '--output', str(self.output)], cwd=self.base, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding='utf-8', timeout=15,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())

    def test_web_desktop_source_package_includes_required_application_icon(self):
        self.write('agentbus/web_desktop.py', b'# web desktop entry\n')
        for relative in release.WEB_ASSET_FILES:
            self.write(relative, b'web asset\n')
        entry = bytes((16, 16, 0, 0, 1, 0, 32, 0)) + (12).to_bytes(4, 'little') + (22).to_bytes(4, 'little')
        icon = b'\x00\x00\x01\x00\x01\x00' + entry + b'fixture-icon'
        self.write(release.APP_ICON_FILE, icon)
        _, content = self.archive()
        self.assertEqual(content[release.APP_ICON_FILE], icon)

    def test_web_desktop_export_rejects_missing_application_icon(self):
        self.write('agentbus/web_desktop.py', b'# web desktop entry\n')
        for relative in release.WEB_ASSET_FILES:
            self.write(relative, b'web asset\n')
        with self.assertRaisesRegex(release.SourceReleaseError, 'application icon'):
            release.export_source(self.root, self.output)


if __name__ == '__main__':
    unittest.main()
