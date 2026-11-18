"""A displayed restore plan and its approval ticket must observe one state."""
import unittest
from pathlib import Path
import tempfile
import json
from unittest.mock import patch
from agentbus import backup,gui_state

class PreviewContextTests(unittest.TestCase):
    def test_configuration_change_after_load_invalidates_cached_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);control=root/'.agentbus';control.mkdir()
            config=control/'config.json'
            config.write_text(json.dumps({'director':'codex','workers':{'codex':{'enabled':True}}}))
            (control/'task-tree.json').write_text(json.dumps({'schema_version':1,'roots':[{'id':'root','title':'root'}],'branches':[],'leaves':[]}))
            with patch.object(gui_state,'state_path',return_value=root/'ui.json'):
                bundle=backup.create_backup_bundle(root);original=backup.load
                def stale_load(*args,**kwargs):
                    cfg=original(*args,**kwargs)
                    config.write_text(json.dumps({'director':'other','workers':{'codex':{'enabled':True},'other':{'enabled':True}}}))
                    return cfg
                with patch.object(backup,'load',side_effect=stale_load):
                    with self.assertRaisesRegex(ValueError,'预览.*变化'):backup.preview_backup_restore(root,bundle,mode='replace')
                self.assertEqual(json.loads(config.read_text())['director'],'other')

    def test_change_during_plan_rejects_preview_instead_of_binding_new_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def workspace(name):
                ws=root/name;control=ws/'.agentbus';control.mkdir(parents=True)
                (control/'config.json').write_text(json.dumps({'director':'codex','workers':{'codex':{'enabled':True}}}))
                (control/'task-tree.json').write_text(json.dumps({'schema_version':1,'roots':[{'id':'root','title':name}],
                    'branches':[],'leaves':[{'id':'leaf','parent_id':'root','title':name}]}))
                return ws
            with patch.object(gui_state,'state_path',return_value=root/'ui.json'):
                source=workspace('source');target=workspace('target');bundle=backup.create_backup_bundle(source)
                original=backup._restore_plan
                def changed(*args,**kwargs):
                    plan=original(*args,**kwargs)
                    path=target/'.agentbus/task-tree.json';tree=json.loads(path.read_text())
                    tree['leaves'].append({'id':'new-leaf','parent_id':'root','title':'concurrent'})
                    path.write_text(json.dumps(tree))
                    return plan
                with patch.object(backup,'_restore_plan',side_effect=changed):
                    with self.assertRaisesRegex(ValueError,'预览.*变化'):
                        backup.preview_backup_restore(target,bundle,mode='replace')
                self.assertEqual(len(json.loads((target/'.agentbus/task-tree.json').read_text())['leaves']),2)
