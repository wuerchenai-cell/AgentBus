"""Source: Codex, 2026-10-02. The entry must work from any host."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from agentbus.cli import main


class TeamEntryTests(unittest.TestCase):
    def setUp(self):
        # A source-host test must register its own existing launcher rather
        # than depend on this machine's user install.json or a frozen EXE.
        import sys
        self.registry=tempfile.TemporaryDirectory()
        self.addCleanup(self.registry.cleanup)
        state_dir=Path(self.registry.name)
        (state_dir/'install.json').write_text(json.dumps({'launcher':{
            'mode':'source','python':sys.executable,
            'entrypoint':str(Path(__file__).resolve().parents[1]/'entry.py')}}),encoding='utf-8')
        self.launcher_patch=patch('agentbus.skill_install.default_state_dir',return_value=state_dir)
        self.launcher_patch.start()
        self.addCleanup(self.launcher_patch.stop)

    def test_first_known_product_import_does_not_grant_default_spawn_capabilities(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace=Path(temp)/'friend project'
            template=Path(temp)/'team-template.json'
            template.write_text(json.dumps({'format':'agentbus.team-template',
                'schema_version':1,'coordinator':'codex',
                'members':[{'id':name,'label':name} for name in
                           ('codex','workbuddy','trae','zcode','opencode')]}),encoding='utf-8')
            code,_,err=self.run_cli('share','import','--workspace',str(workspace),
                                   '--template',str(template))
            self.assertEqual(code,0,err)
            cfg=json.loads((workspace/'.agentbus/config.json').read_text(encoding='utf-8'))
            for name,worker in cfg['workers'].items():
                if worker['enabled']:
                    self.assertFalse(worker['can_spawn'],name)
                    self.assertTrue(worker['human_in_loop'],name)
                    self.assertFalse(worker.get('command'),name)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_program_tools_can_configure_without_desktop_control(self):
        with tempfile.TemporaryDirectory() as temp:
            code, out, err = self.run_cli('team', 'configure', '--workspace', temp,
                '--members', 'codex,workbuddy,trae,zcode,opencode', '--coordinator', 'codex')
            self.assertEqual(code, 0, err)
            cfg = json.loads((Path(temp)/'.agentbus/config.json').read_text(encoding='utf8'))
            self.assertEqual(cfg['director'], 'codex')
            self.assertTrue(cfg['workers']['opencode']['enabled'])
            self.assertTrue((Path(temp)/'AGENTBUS.md').is_file())

    def test_configure_rejects_unknown_member_before_creating_project(self):
        with tempfile.TemporaryDirectory() as temp:
            code, _, _ = self.run_cli('team','configure','--workspace',temp,
                '--members','codex,unregistered','--coordinator','codex')
            self.assertNotEqual(code, 0)
            self.assertFalse((Path(temp)/'.agentbus/config.json').exists())

    def test_configure_preserves_user_project_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            rules=Path(temp)/'AGENTS.md'; rules.write_text('user rules',encoding='utf8')
            code, _, err=self.run_cli('team','configure','--workspace',temp,
                '--members','trae,zcode','--coordinator','trae')
            self.assertEqual(code,0,err)
            self.assertEqual(rules.read_text(),'user rules')

    def test_workflow_conflict_returns_error_without_gui_dialog(self):
        with tempfile.TemporaryDirectory() as temp:
            entry=Path(temp)/'AGENTBUS.md'; entry.write_text('user owned',encoding='utf8')
            code, out, err=self.run_cli('team','configure','--workspace',temp,
                '--members','codex','--coordinator','codex')
            self.assertNotEqual(code,0)
            self.assertIn('已有文件',err)
            self.assertEqual(entry.read_text(),'user owned')
            result=json.loads(out)
            self.assertTrue(result['saved'])
            self.assertFalse(result['workflow_ready'])
            self.assertTrue((Path(temp)/'.agentbus/config.json').is_file())

    def test_configure_preserves_permission_changed_before_lease(self):
        from agentbus.config import load,save
        from agentbus.lease import LeaseManager
        with tempfile.TemporaryDirectory() as temp:
            save(load(temp,create=True))
            original=LeaseManager.acquire
            def racing_acquire(manager,rel,*args,**kwargs):
                if rel=='.agentbus/config.json':
                    cfg=load(temp)
                    cfg.data['workers']['codex']['sandbox']='read-only'
                    save(cfg)
                return original(manager,rel,*args,**kwargs)
            with patch.object(LeaseManager,'acquire',racing_acquire):
                code,_,err=self.run_cli('team','configure','--workspace',temp,
                    '--members','codex,workbuddy','--coordinator','codex')
            self.assertEqual(code,0,err)
            self.assertEqual(load(temp).worker('codex')['sandbox'],'read-only')

    # Source: actual relocated friend-package import failed on an empty project,
    # Codex, 2026-10-02. Validation must precede creating the first config.
    def test_share_import_initializes_a_new_project_from_valid_template(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace=Path(temp)/'new project'
            template=Path(temp)/'team-template.json'
            template.write_text(json.dumps({'format':'agentbus.team-template',
                'schema_version':1,'coordinator':'future-peer',
                'members':[{'id':'future-peer','label':'Future peer'}]}),encoding='utf-8')
            code,out,err=self.run_cli('share','import','--workspace',str(workspace),
                                     '--template',str(template))
            self.assertEqual(code,0,err)
            cfg=json.loads((workspace/'.agentbus/config.json').read_text(encoding='utf-8'))
            self.assertEqual(cfg['director'],'future-peer')
            selected={name for name,worker in cfg['workers'].items() if worker['enabled']}
            self.assertEqual(selected,{'future-peer'})
            self.assertFalse(cfg['workers']['future-peer']['can_spawn'])
            self.assertTrue(json.loads(out)['workflow_ready'])

    def test_invalid_share_template_does_not_initialize_a_new_project(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace=Path(temp)/'new project'
            template=Path(temp)/'team-template.json'
            template.write_text('{"coordinator":"codex","token":"forbidden"}',encoding='utf-8')
            code,_,_=self.run_cli('share','import','--workspace',str(workspace),
                                '--template',str(template))
            self.assertNotEqual(code,0)
            self.assertFalse(workspace.exists())


if __name__ == '__main__':
    unittest.main()
