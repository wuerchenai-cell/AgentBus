"""Replacing a task tree must not move live claims or hide recovery context."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agentbus.config import load
from agentbus.task_tree import TaskForest
from agentbus.tree_templates import import_template, builtin_templates
from agentbus.web_bridge import WebBridge

class ImportProtectionRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.forest = TaskForest(load(self.ws, create=True))
        for name in ('first','second'):
            self.forest.seed_leaf({'id':name,'title':name},branch_id='work')
        self.bridge = WebBridge(self.ws)
        self.document = builtin_templates()[0]['document']

    def preview(self):
        return self.bridge._import_tree({'template_id':'software-project','confirm':False})

    def files(self):
        return {p.relative_to(self.ws).as_posix():p.read_bytes()
                for p in self.ws.rglob('*') if p.is_file() and 'operation-locks' not in p.parts}

    def test_preview_lists_two_active_members_without_mutating_or_disclosing_tickets(self):
        for leaf, agent in [('first','workbuddy'),('second','trae')]:
            self.forest.claim_leaf(leaf,agent)
        before=self.files()
        view=self.preview()
        self.assertEqual(before,self.files())
        self.assertEqual({x['agent'] for x in view['impact']['processing']},{'workbuddy','trae'})
        self.assertFalse(view['impact']['can_replace'])
        self.assertNotIn('claim_id',json.dumps(view['impact']))
        with self.assertRaisesRegex(ValueError,'在途'):
            self.bridge._import_tree({'template_id':'software-project','confirm':True,'replace':True,'preview_id':view['preview_id']})
        self.assertEqual(before,self.files())

    def test_direct_import_also_rejects_live_work(self):
        self.forest.claim_leaf('first','workbuddy')
        before=self.files()
        with self.assertRaises(ValueError):
            import_template(self.forest,self.document,replace=True)
        self.assertEqual(before,self.files())

    def test_append_preserves_original_claim_and_all_original_structure(self):
        self.forest.claim_leaf('first','workbuddy')
        before=self.files()
        preview=self.preview()
        self.bridge._import_tree({'template_id':'software-project','confirm':True,'replace':False,'preview_id':preview['preview_id']})
        for name,data in before.items():
            if name!='.agentbus/task-tree.json' and not name.endswith('events.jsonl'):
                self.assertEqual((self.ws/name).read_bytes(),data,name)
        self.assertIn('first',{x['id'] for x in self.forest.snapshot()['leaves']})

    def test_replace_archives_original_tree_and_pending_card_but_preserves_info(self):
        task=self.forest.bus.post(self.forest.bus.make('codex','workbuddy','task','Queued','Work',result_to='codex'))
        info=self.forest.bus.post(self.forest.bus.make('workbuddy','codex','info','Notes','Keep these'))
        tree=self.forest.path.read_bytes()
        old=(self.forest.bus.dirs('workbuddy')['ready']/(task.id+'.json')).read_bytes()
        preview=self.preview()
        result=self.bridge._import_tree({'template_id':'software-project','confirm':True,'replace':True,'preview_id':preview['preview_id']})
        archive=self.ws/result['recovery_path']
        self.assertEqual((archive/'task-tree.snapshot').read_bytes(),tree)
        self.assertEqual((archive/'workbuddy/ready'/(task.id+'.json')).read_bytes(),old)
        self.assertTrue((archive/'RECOVERY.md').is_file())
        self.assertIsNotNone(self.forest.bus.find_message(info.id))

    def test_confirm_rechecks_new_work_since_preview(self):
        preview=self.preview()
        self.forest.claim_leaf('first','workbuddy')
        before=self.files()
        with self.assertRaises(ValueError):
            self.bridge._import_tree({'template_id':'software-project','confirm':True,'replace':True,'preview_id':preview['preview_id']})
        self.assertEqual(before,self.files())

    def test_replace_flag_is_not_coerced_from_string(self):
        preview=self.preview()
        with self.assertRaises(ValueError):
            self.bridge._import_tree({'template_id':'software-project','confirm':True,'replace':'false','preview_id':preview['preview_id']})

    def test_new_queued_work_after_preview_requires_repreview_before_replace(self):
        preview=self.preview()
        self.forest.bus.post(self.forest.bus.make('codex','workbuddy','task','New','Keep',result_to='codex'))
        before=self.files()
        with self.assertRaisesRegex(ValueError,'预览'):
            self.bridge._import_tree({'template_id':'software-project','confirm':True,'replace':True,'preview_id':preview['preview_id']})
        self.assertEqual(before,self.files())

    def test_publication_between_preflight_and_archive_is_not_silently_archived(self):
        from agentbus import tree_templates
        original=tree_templates._quarantine_mailboxes
        published=[]
        def publish_then_archive(bus,**kwargs):
            published.append(bus.post(bus.make('codex','trae','task','New concurrent task','Keep',result_to='codex')))
            return original(bus,**kwargs)
        with patch.object(tree_templates,'_quarantine_mailboxes',side_effect=publish_then_archive):
            import_template(self.forest,self.document,replace=True)
        msg=published[0]
        self.assertTrue((self.forest.bus.dirs('trae')['ready']/(msg.id+'.json')).is_file())
        self.assertFalse(any((self.forest.root/'trash').rglob(msg.id+'.json')))
