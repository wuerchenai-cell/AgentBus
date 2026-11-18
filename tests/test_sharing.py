"""Source: Codex sharing task, 2026-10-02. No models or private stores."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentbus.config import Config, DEFAULTS


class SharingTests(unittest.TestCase):
    def public_assets(self, root):
        from tools.export_source import LICENSE_NAMES
        source = Path(__file__).resolve().parents[1]
        names = ['LICENSE', 'SOURCE_LICENSE_STATUS.md', 'share_assets/开始使用.md',
                 'share_assets/THIRD_PARTY_NOTICES.md', 'share_assets/licenses/provenance.json']
        names += ['docs/public/'+name for name in ('README.md','RELEASE.md','ECOSYSTEM.md','SECURITY.md')]
        names += ['share_assets/licenses/'+name for name in sorted(LICENSE_NAMES)]
        for name in names:
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes((source/name).read_bytes())
        return root/'share_assets'

    def test_template_ids_follow_profile_validator_before_import(self):
        sharing = self.sharing()
        for name in ('new_agent', 'con', 'nul'):
            with self.subTest(name=name), self.assertRaises(sharing.ShareError):
                sharing._checked_template({'format': sharing.FORMAT,
                    'schema_version': sharing.SCHEMA_VERSION, 'coordinator': name,
                    'members': [{'id': name, 'label': 'New Agent'}]})

    def test_new_template_does_not_retain_candidate_commands(self):
        sharing = self.sharing()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(DEFAULTS['workers']['codex'],
                {'candidate_command': ['local-only-program']}):
            cfg = sharing.new_template_config(Path(tmp))
        self.assertNotIn('candidate_command', cfg.worker('codex'))

    def sharing(self):
        try:
            return importlib.import_module('agentbus.sharing')
        except ImportError:
            self.fail('sharing module is missing: no pure portable team export exists')

    def config(self, root: Path, *, saved=False) -> Config:
        data = copy.deepcopy(DEFAULTS)
        data['workspace'] = str(root)
        data['workers']['codex'].update(
            command=r'C:\PRIVATE OWNER\codex.exe', model='PRIVATE MODEL',
            env={'TOKEN': 'SECRET SHOULD NEVER EXPORT'},
            sandbox='workspace-write', timeout_s=123)
        data['workers']['trae']['enabled'] = False
        data['workers']['zcode']['enabled'] = False
        data['workers']['opencode']['enabled'] = False
        path = root / '.agentbus' / 'config.json'
        if saved:
            path.parent.mkdir(parents=True)
            path.write_text('existing local configuration', encoding='utf-8')
        return Config(path=path, workspace=root, data=data)

    def test_export_whitelists_product_members_and_coordinator_only(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.config(Path(temp))
            original = copy.deepcopy(cfg.data)
            value = self.sharing().export_template(cfg)
            self.assertEqual(set(value), {'format', 'schema_version', 'coordinator', 'members'})
            self.assertEqual(value['format'], 'agentbus.team-template')
            self.assertEqual(value['schema_version'], 1)
            self.assertEqual(value['coordinator'], 'codex')
            self.assertEqual([x['id'] for x in value['members']], ['codex', 'workbuddy'])
            self.assertTrue(all(set(x) == {'id', 'label'} for x in value['members']))
            raw = json.dumps(value)
            for private in (str(cfg.workspace), 'PRIVATE OWNER', 'PRIVATE MODEL', 'SECRET', 'sandbox'):
                self.assertNotIn(private, raw)
            self.assertEqual(cfg.data, original)

    def test_import_retains_local_permissions_capabilities_and_disabled_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.config(Path(temp))
            original = copy.deepcopy(cfg.data)
            template = self.sharing().export_template(cfg)
            template['members'].append({'id': 'new-editor', 'label': 'New Editor'})
            template['coordinator'] = 'new-editor'
            updated = self.sharing().import_template(cfg, template)
            self.assertEqual(updated.director, 'new-editor')
            self.assertEqual(updated.worker('codex')['sandbox'], 'workspace-write')
            self.assertEqual(updated.worker('codex')['command'], cfg.worker('codex')['command'])
            self.assertTrue(updated.worker('codex')['can_spawn'])
            self.assertEqual(updated.worker('new-editor')['kind'], 'gui')
            self.assertFalse(updated.worker('new-editor')['can_spawn'])
            self.assertFalse(updated.worker('trae')['enabled'])
            self.assertEqual(cfg.data, original)
            self.assertFalse(cfg.path.exists(), 'import must not secretly persist the configuration')

    def test_existing_group_requires_explicit_replace_and_never_writes_itself(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.config(Path(temp), saved=True)
            api = self.sharing()
            payload = api.export_template(cfg)
            with self.assertRaisesRegex(api.ShareError, 'replace|已有|确认'):
                api.import_template(cfg, payload)
            api.import_template(cfg, payload, replace=True)
            self.assertEqual(cfg.path.read_text(), 'existing local configuration')

    def test_import_rejects_injected_commands_credentials_paths_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.config(Path(temp))
            api = self.sharing()
            for injected in ({'command': 'malicious'}, {'workspace': 'C:/private'}, {'auth': 'secret'}):
                payload = api.export_template(cfg)
                payload.update(injected)
                with self.subTest(injected=injected), self.assertRaises(api.ShareError):
                    api.import_template(cfg, payload)
            payload = api.export_template(cfg)
            payload['members'][0]['can_spawn'] = True
            with self.assertRaises(api.ShareError):
                api.import_template(cfg, payload)

    def test_import_rejects_bad_identity_schema_duplicate_and_absent_leader(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.config(Path(temp))
            api = self.sharing()
            for identity in ('../escape', 'C:/private', 'human', 'director', 'UpperCase', '', 'x' * 65):
                payload = api.export_template(cfg)
                payload['members'][0]['id'] = identity
                with self.subTest(identity=identity), self.assertRaises(api.ShareError):
                    api.import_template(cfg, payload)
            for modify in (
                lambda p: p.update(schema_version=99),
                lambda p: p.update(schema_version=True),
                lambda p: p['members'].append(p['members'][0].copy()),
                lambda p: p.update(coordinator='not-selected'),
                lambda p: p.update(members=[]),
            ):
                payload = api.export_template(cfg)
                modify(payload)
                with self.assertRaises(api.ShareError):
                    api.import_template(cfg, payload)

    def test_labels_with_paths_or_account_identifiers_are_not_exported(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.config(Path(temp))
            cfg.worker('codex')['label'] = 'person@example.com'
            cfg.worker('workbuddy')['label'] = r'C:\Users\PRIVATE OWNER'
            payload = self.sharing().export_template(cfg)
            self.assertEqual([m['label'] for m in payload['members']], ['codex', 'workbuddy'])

    def test_package_has_fixed_files_hashes_and_no_workspace_or_launcher_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = self.config(root / 'private-project')
            cfg.workspace.mkdir()
            (cfg.workspace / 'secret.txt').write_text('SECRET WORKSPACE FILE')
            exe = root / '程序 空格' / 'AgentBus.exe'
            exe.parent.mkdir()
            exe.write_bytes(b'opaque-test-executable')
            result = self.sharing().build_share_package(exe, cfg, root / 'friends.zip')
            self.assertTrue((root / 'friends.zip').is_file())
            self.assertEqual(result['sha256'], hashlib.sha256((root / 'friends.zip').read_bytes()).hexdigest())
            with zipfile.ZipFile(root / 'friends.zip') as archive:
                names = archive.namelist()
                self.assertEqual(len(names), len(set(names)))
                for name in names:
                    self.assertFalse(name.startswith(('/', '\\')))
                    self.assertNotIn('..', Path(name).parts)
                self.assertTrue({'AgentBus.exe', 'team-template.json', '开始使用.md',
                                 'THIRD_PARTY_NOTICES.md', 'package-manifest.json'} <= set(names))
                self.assertTrue(any(n.startswith('licenses/') for n in names))
                manifest = json.loads(archive.read('package-manifest.json'))
                for name, digest in manifest['files'].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)
                text = '\n'.join(archive.read(n).decode('utf-8') for n in names if not n.endswith('.exe'))
                for private in (str(cfg.workspace), 'PRIVATE OWNER', 'PRIVATE MODEL', 'SECRET', 'install.json'):
                    self.assertNotIn(private, text)
                self.assertIn('使用这个小组工作', archive.read('开始使用.md').decode('utf-8'))

    def test_package_carries_project_license_and_public_reference_docs(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);exe=root/'AgentBus.exe';exe.write_bytes(b'fixture')
            archive_path=root/'package.zip'
            self.sharing().build_share_package(exe,self.config(root/'project'),archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.read('LICENSE'),(Path(__file__).resolve().parents[1]/'LICENSE').read_bytes())
                self.assertIn('SOURCE_LICENSE_STATUS.md',archive.namelist())
                for name in ('README.md','RELEASE.md','ECOSYSTEM.md','SECURITY.md'):
                    self.assertIn('docs/public/'+name,archive.namelist())

    def test_formal_and_template_exports_have_identical_public_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);exe=root/'AgentBus.exe';exe.write_bytes(b'fixture')
            api=self.sharing();base=root/'base.zip';team=root/'team.zip'
            api.build_portable_package(exe,base)
            api.build_share_package(exe,self.config(root/'project'),team)
            with zipfile.ZipFile(base) as a,zipfile.ZipFile(team) as b:
                self.assertEqual(set(b.namelist())-set(a.namelist()),{'team-template.json'})
                self.assertNotIn('team-template.json',a.namelist())
                for name in a.namelist():
                    if name!='package-manifest.json':self.assertEqual(a.read(name),b.read(name))

    def test_missing_project_license_preserves_previous_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);assets=self.public_assets(root)
            (root/'LICENSE').unlink();exe=root/'AgentBus.exe';exe.write_bytes(b'fixture')
            out=root/'package.zip';out.write_bytes(b'previous package')
            with patch('agentbus.sharing._assets',return_value=assets):
                with self.assertRaises(self.sharing().ShareError):
                    self.sharing().build_share_package(exe,self.config(root/'project'),out)
            self.assertEqual(out.read_bytes(),b'previous package')

    def test_provenance_cannot_add_a_self_declared_unknown_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);assets=self.public_assets(root)
            path=assets/'licenses/provenance.json';payload=json.loads(path.read_bytes())
            secret=b'UNLISTED_PRIVATE_CANARY';(assets/'licenses/private.txt').write_bytes(secret)
            payload['files'].append({'path':'licenses/private.txt','component':'extra','version':'1',
                'source':'untrusted','sha256':hashlib.sha256(secret).hexdigest()})
            path.write_text(json.dumps(payload),encoding='utf-8')
            exe=root/'AgentBus.exe';exe.write_bytes(b'fixture');out=root/'package.zip';out.write_bytes(b'previous')
            with patch('agentbus.sharing._assets',return_value=assets):
                with self.assertRaises(self.sharing().ShareError):
                    self.sharing().build_share_package(exe,self.config(root/'project'),out)
            self.assertEqual(out.read_bytes(),b'previous')

    def test_export_streams_the_executable_instead_of_reading_all_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);exe=root/'AgentBus.exe';exe.write_bytes(b'fixture')
            api=self.sharing();original=Path.read_bytes
            def guarded(path):
                self.assertNotEqual(path,exe,'executable must be streamed')
                return original(path)
            with patch.object(Path,'read_bytes',guarded):
                api.build_share_package(exe,self.config(root/'project'),root/'package.zip')

    def test_incomplete_frozen_assets_do_not_fall_back_to_developer_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);exe=root/'AgentBus.exe';exe.write_bytes(b'fixture')
            with patch.object(sys,'_MEIPASS',str(root/'incomplete-bundle'),create=True):
                with self.assertRaises(self.sharing().ShareError):
                    self.sharing().build_share_package(exe,self.config(root/'project'),root/'package.zip')
            self.assertFalse((root/'package.zip').exists())

    def test_archive_corruption_is_rejected_before_replacing_previous_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);exe=root/'AgentBus.exe';exe.write_bytes(b'fixture')
            out=root/'package.zip';out.write_bytes(b'previous');original=zipfile.ZipFile.writestr
            def corrupt(archive,name,blob,*args,**kwargs):
                if name=='licenses/python-LICENSE.txt':blob=b'corrupted'
                return original(archive,name,blob,*args,**kwargs)
            with patch.object(zipfile.ZipFile,'writestr',corrupt):
                with self.assertRaises(OSError):
                    self.sharing().build_share_package(exe,self.config(root/'project'),out)
            self.assertEqual(out.read_bytes(),b'previous')

    def test_package_rejects_missing_executable_and_output_executable_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = self.config(root)
            api = self.sharing()
            with self.assertRaises(api.ShareError):
                api.build_share_package(root / 'absent.exe', cfg, root / 'friends.zip')
            exe = root / 'AgentBus.exe'
            exe.write_bytes(b'fake')
            with self.assertRaises(api.ShareError):
                api.build_share_package(exe, cfg, exe)
            self.assertEqual(exe.read_bytes(), b'fake')

    def test_moved_package_registers_the_friends_launcher_without_personal_paths(self):
        from agentbus.skill_install import install_for_current_user
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = self.config(root / 'creator-project')
            creator_exe = root / 'creator-install' / 'AgentBus.exe'
            creator_exe.parent.mkdir()
            creator_exe.write_bytes(b'fake executable fixture')
            archive_path = root / 'share.zip'
            self.sharing().build_share_package(creator_exe, cfg, archive_path)
            moved = root / '朋友 长期位置'
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(moved)
            source = root / 'bundled-skill.md'
            source.write_text('---\nname: agentbus-team\n---\nRead current project.\n', encoding='utf-8')
            home = root / '朋友 用户'
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'executable', str(moved / 'AgentBus.exe')), \
                    patch('agentbus.skill_install.Path.home', return_value=home), \
                    patch('agentbus.skill_install.bundled_skill_source', return_value=source), \
                    patch.dict(os.environ, {'CODEX_HOME': ''}):
                installed = install_for_current_user('portable-test')
            state = json.loads(installed.manifest_file.read_text(encoding='utf-8'))
            self.assertEqual(state['launcher']['executable'], str((moved / 'AgentBus.exe').resolve()))
            self.assertNotIn(str(creator_exe), json.dumps(state))

    def test_assets_ignore_unlisted_files_and_reject_tampered_licenses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            api = self.sharing()
            assets = self.public_assets(root)
            (assets / 'private-do-not-export.txt').write_text('PRIVATE OWNER')
            exe = root / 'AgentBus.exe'
            exe.write_bytes(b'fixture')
            cfg = self.config(root / 'project')
            with patch('agentbus.sharing._assets', return_value=assets):
                api.build_share_package(exe, cfg, root / 'clean.zip')
                with zipfile.ZipFile(root / 'clean.zip') as archive:
                    self.assertNotIn('private-do-not-export.txt', archive.namelist())
                provenance = json.loads((assets / 'licenses' / 'provenance.json').read_text())
                bad_license = assets / provenance['files'][0]['path']
                bad_license.write_text('tampered')
                with self.assertRaisesRegex(api.ShareError, '校验'):
                    api.build_share_package(exe, cfg, root / 'tampered.zip')
            self.assertFalse((root / 'tampered.zip').exists())


if __name__ == '__main__':
    unittest.main()
