"""Publication failures must preserve the last usable formal program and truth."""
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('repair_deployer', Path(__file__).resolve().parents[1] / 'tools/deploy_formal_release.py')
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)

class ReleaseIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.formal = self.root/'成果/AgentBus.exe'
        self.source = self.root/'成果/agentbus/dist-release/AgentBus.exe'
        self.source.parent.mkdir(parents=True)
        self.formal.write_bytes(b'old-formal')
        self.source.write_bytes(b'new-formal')
        self.workflow = self.root/'.agentbus/workflow.json'
        self.workflow.parent.mkdir(); self.workflow.write_text('{"old":true}')
        self.checkout = self.root/'成果/agentbus'
        from tools.export_source import LICENSE_NAMES
        self.public = {
            'LICENSE': b'Apache License fixture\n',
            'SOURCE_LICENSE_STATUS.md': b'Not a complete third party audit\n',
            'share_assets/THIRD_PARTY_NOTICES.md': b'Third party notices\n',
            'share_assets/开始使用.md': b'Public first-run guide\n',
        }
        for name in ('README.md', 'RELEASE.md', 'ECOSYSTEM.md', 'SECURITY.md'):
            self.public['docs/public/'+name] = ('Public '+name+'\n').encode()
        self.provenance = {'schema_version': 1, 'collected_at': '2026-11-16',
                           'coverage': 'isolated test fixture', 'files': []}
        for name in sorted(LICENSE_NAMES):
            blob = ('License fixture '+name+'\n').encode()
            self.public['share_assets/licenses/'+name] = blob
            self.provenance['files'].append({
                'path': 'licenses/'+name, 'component': name, 'version': 'fixture',
                'source': 'isolated test', 'sha256': hashlib.sha256(blob).hexdigest()})
        self.public['share_assets/licenses/provenance.json'] = json.dumps(self.provenance).encode()
        for name, blob in self.public.items():
            path = self.checkout/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
    def refresh(self, root, exe):
        self.workflow.write_text(json.dumps({'launcher':{'executable':str(exe), 'sha256':deployer.file_hash(exe)}}))
    def test_atomic_replace_failure_keeps_old_executable(self):
        original = deployer.os.replace
        def fail(source, target):
            if Path(target) == self.formal:
                raise PermissionError('simulated running formal program')
            return original(source, target)
        with patch.object(deployer.os, 'replace', side_effect=fail):
            with self.assertRaises(PermissionError): deployer.atomic_copy(self.source, self.formal)
        self.assertEqual(self.formal.read_bytes(), b'old-formal')
        self.assertEqual(self.source.read_bytes(), b'new-formal')
    def test_refresh_failure_rolls_back_program_and_truth(self):
        def fail(root, exe):
            self.workflow.write_text('{"broken":true}')
            raise RuntimeError('refresh failed')
        with self.assertRaisesRegex(RuntimeError, 'refresh failed'):
            deployer.deploy(self.root, refresh_workflow=fail)
        self.assertEqual(self.formal.read_bytes(), b'old-formal')
        self.assertEqual(self.workflow.read_text(), '{"old":true}')
        self.assertTrue(self.source.is_file())
    def test_zip_failure_rolls_back_all_published_truth(self):
        bundle = self.root/'成果/AgentBus-win-x64-green.zip';bundle.write_bytes(b'old-zip')
        with patch.object(deployer, 'atomic_zip', side_effect=OSError('full disk')):
            with self.assertRaisesRegex(OSError, 'full disk'):
                deployer.deploy(self.root, refresh_workflow=self.refresh)
        self.assertEqual(self.formal.read_bytes(), b'old-formal')
        self.assertEqual(self.workflow.read_text(), '{"old":true}')
        self.assertEqual(bundle.read_bytes(), b'old-zip')
    def test_success_has_one_formal_binary_and_matching_zip(self):
        result = deployer.deploy(self.root, refresh_workflow=self.refresh)
        self.assertEqual(result['sha256'], deployer.file_hash(self.formal))
        self.assertFalse(self.source.exists())
        with zipfile.ZipFile(self.root/'成果/AgentBus-win-x64-green.zip') as z:
            self.assertEqual(z.read('AgentBus.exe'), self.formal.read_bytes())
        self.assertEqual(json.loads(self.workflow.read_text())['launcher']['sha256'], result['sha256'])

    def test_portable_bundle_has_exact_public_files_and_complete_hashes(self):
        bundle = self.root/'成果/clean.zip'
        deployer.atomic_zip(self.root, self.formal, bundle)
        expected = {'AgentBus.exe', 'LICENSE', 'SOURCE_LICENSE_STATUS.md',
                    '开始使用.md', 'THIRD_PARTY_NOTICES.md', 'licenses/provenance.json',
                    'package-manifest.json'}
        expected.update('docs/public/'+name for name in ('README.md','RELEASE.md','ECOSYSTEM.md','SECURITY.md'))
        expected.update(item['path'] for item in self.provenance['files'])
        with zipfile.ZipFile(bundle) as z:
            self.assertEqual(set(z.namelist()), expected)
            self.assertEqual(len(z.namelist()), len(expected))
            manifest = json.loads(z.read('package-manifest.json'))
            self.assertEqual(manifest['format'], 'agentbus.portable-package')
            self.assertEqual(set(manifest['files']), expected-{'package-manifest.json'})
            for name, digest in manifest['files'].items():
                self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), digest)
            self.assertEqual(z.read('LICENSE'), self.public['LICENSE'])
            self.assertEqual(z.read('SOURCE_LICENSE_STATUS.md'), self.public['SOURCE_LICENSE_STATUS.md'])
            for item in self.provenance['files']:
                self.assertEqual(z.read(item['path']), self.public['share_assets/'+item['path']])

    def test_portable_bundle_never_reads_or_exports_project_documents(self):
        canary = b'PRIVATE_PROJECT_CANARY_PORTABLE_20261116'
        for name in ('AGENTS.md','AGENTBUS.md','PROJECT.md','00-系统自我坦白与快速上手.md'):
            (self.root/name).write_bytes(canary)
        (self.checkout/'share_assets/unlisted.txt').write_bytes(canary)
        (self.checkout/'share_assets/licenses/unlisted.txt').write_bytes(canary)
        bundle = self.root/'成果/clean.zip'
        original = Path.open
        private = {self.root/name for name in ('AGENTS.md','AGENTBUS.md','PROJECT.md','00-系统自我坦白与快速上手.md')}
        def guarded(path, *args, **kwargs):
            self.assertNotIn(path, private, 'project documents must not even be read')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            deployer.atomic_zip(self.root, self.formal, bundle)
        with zipfile.ZipFile(bundle) as z:
            for name in z.namelist(): self.assertNotIn(canary, z.read(name))

    def test_missing_or_changed_public_license_preserves_old_bundle(self):
        bundle = self.root/'成果/clean.zip'
        for name, corrupt in (('LICENSE', False), ('share_assets/licenses/python-LICENSE.txt', False),
                              ('share_assets/licenses/python-LICENSE.txt', True)):
            with self.subTest(name=name, corrupt=corrupt):
                path=self.checkout/name; before=path.read_bytes()
                bundle.write_bytes(b'previous valid package')
                if corrupt: path.write_bytes(b'changed license')
                else: path.unlink()
                try:
                    with self.assertRaises(ValueError):
                        deployer.atomic_zip(self.root, self.formal, bundle)
                    self.assertEqual(bundle.read_bytes(), b'previous valid package')
                    self.assertEqual(self.formal.read_bytes(), b'old-formal')
                finally: path.write_bytes(before)

    def test_invalid_provenance_cannot_select_an_external_file(self):
        path=self.checkout/'share_assets/licenses/provenance.json'
        payload=json.loads(path.read_bytes()); payload['files'][0]['path']='../outside.txt'
        path.write_text(json.dumps(payload),encoding='utf-8')
        bundle=self.root/'成果/clean.zip';bundle.write_bytes(b'previous package')
        with self.assertRaises(ValueError): deployer.atomic_zip(self.root,self.formal,bundle)
        self.assertEqual(bundle.read_bytes(),b'previous package')

    def test_missing_public_asset_rolls_back_whole_release(self):
        (self.checkout/'LICENSE').unlink()
        bundle=self.root/'成果/AgentBus-win-x64-green.zip';bundle.write_bytes(b'previous package')
        with self.assertRaises(ValueError): deployer.deploy(self.root,refresh_workflow=self.refresh)
        self.assertEqual(self.formal.read_bytes(),b'old-formal')
        self.assertEqual(self.workflow.read_text(),'{"old":true}')
        self.assertEqual(bundle.read_bytes(),b'previous package')

    def test_package_write_failure_preserves_old_bundle(self):
        bundle=self.root/'成果/clean.zip';bundle.write_bytes(b'previous package')
        with patch.object(zipfile.ZipFile,'writestr',side_effect=OSError('simulated full disk')):
            with self.assertRaisesRegex(OSError,'full disk'):
                deployer.atomic_zip(self.root,self.formal,bundle)
        self.assertEqual(bundle.read_bytes(),b'previous package')
        self.assertFalse(list(bundle.parent.glob('.clean.zip.*.pending')))

    def test_corrupt_written_license_is_caught_before_replacement(self):
        bundle=self.root/'成果/clean.zip';bundle.write_bytes(b'previous package')
        original=zipfile.ZipFile.writestr
        def corrupt(archive,name,blob,*args,**kwargs):
            if name=='licenses/python-LICENSE.txt': blob=b'corrupted output'
            return original(archive,name,blob,*args,**kwargs)
        with patch.object(zipfile.ZipFile,'writestr',corrupt):
            with self.assertRaisesRegex(OSError,'content verification failed'):
                deployer.atomic_zip(self.root,self.formal,bundle)
        self.assertEqual(bundle.read_bytes(),b'previous package')

    def test_formal_executable_change_during_packaging_is_rejected(self):
        bundle=self.root/'成果/clean.zip';bundle.write_bytes(b'previous package')
        original=zipfile.ZipFile.write
        def changing(archive,filename,*args,**kwargs):
            result=original(archive,filename,*args,**kwargs)
            self.formal.write_bytes(b'other release')
            return result
        with patch.object(zipfile.ZipFile,'write',changing):
            with self.assertRaisesRegex(OSError,'changed while packaging'):
                deployer.atomic_zip(self.root,self.formal,bundle)
        self.assertEqual(bundle.read_bytes(),b'previous package')

    def test_symlink_public_asset_is_rejected(self):
        from agentbus.sharing import ShareError
        from tools.export_source import SourceReleaseError

        license_path=self.checkout/'LICENSE'; license_path.unlink()
        target=self.root/'outside-license.txt'; target.write_bytes(b'external content')
        try: license_path.symlink_to(target)
        except OSError as exc: self.skipTest(f'symlink not supported by host: {exc.winerror if hasattr(exc,"winerror") else exc}')
        bundle=self.root/'成果/clean.zip';bundle.write_bytes(b'previous package')
        with self.assertRaisesRegex(ShareError, '公开分发资源校验失败') as caught:
            deployer.atomic_zip(self.root,self.formal,bundle)
        cause = caught.exception.__cause__
        self.assertIsInstance(cause, SourceReleaseError)
        self.assertIn('Links and reparse', str(cause))
        self.assertEqual(bundle.read_bytes(),b'previous package')

    def test_failed_rollback_preserves_old_binary_backup(self):
        original = deployer.os.replace
        def fail_restore(source, target):
            if Path(target) == self.formal and Path(source).read_bytes() == b'old-formal':
                raise PermissionError('rollback destination locked')
            return original(source, target)
        def fail(root, exe): raise RuntimeError('refresh failed')
        with patch.object(deployer.os, 'replace', side_effect=fail_restore):
            with self.assertRaises(Exception): deployer.deploy(self.root, refresh_workflow=fail)
        retained = list(self.formal.parent.glob('.AgentBus.exe.*.pending'))
        self.assertTrue(any(path.read_bytes() == b'old-formal' for path in retained))

    def test_source_export_includes_mailbox_cli_dependency(self):
        from tools.export_source import export_source, SourceReleaseError
        from test_source_release import SourceReleaseTests
        fixture = SourceReleaseTests('test_whitelist_excludes_secrets_and_maps_public_readme')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.write('tests/test_audit_mailbox_backlog.py', b'# consumer fixture\n')
        fixture.output.write_bytes(b'previous archive')
        with self.assertRaises(SourceReleaseError): export_source(fixture.root, fixture.output)
        self.assertEqual(fixture.output.read_bytes(), b'previous archive')
        body=b'# mailbox CLI public fixture\n'
        fixture.write('tools/audit_mailbox_backlog.py', body)
        export_source(fixture.root, fixture.output)
        with zipfile.ZipFile(fixture.output) as archive:
            self.assertEqual(archive.read('tools/audit_mailbox_backlog.py'), body)

    def test_publication_preserves_dated_state_history(self):
        state=self.root/'STATE.md'
        history='# State\n\n## 2026-10-31 | Earlier release\n\n- **文件大小**：`9 字节`\n  - **SHA256 摘要**：`'+'0'*64+'`\n'
        state.write_text(history,encoding='utf-8')
        deployer.deploy(self.root, refresh_workflow=self.refresh)
        self.assertIn(history.partition('\n\n')[2],state.read_text(encoding='utf-8'))
        self.assertIn(deployer.file_hash(self.formal),state.read_text(encoding='utf-8'))
