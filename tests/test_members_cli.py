"""Codex, 2026-10-05: public entrypoint tests, no model or credential access."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib,io,json,sys,tempfile,unittest
from pathlib import Path
from agentbus.cli import main
from agentbus.config import load,save
from agentbus.project_workflow import prepare_project_workflow

class MembersCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.cfg=load(self.root,create=True)
        self.cfg.data['workers']['peer']={'enabled':True,'executor_enabled':True,'can_spawn':False,'kind':'gui'}
        save(self.cfg);prepare_project_workflow(self.cfg)
        path=self.root/'.agentbus/workflow.json';data=json.loads(path.read_text(encoding='utf-8'))
        data['launcher']={'mode':'source','python':sys.executable,'entrypoint':str(Path(__file__).resolve())}
        path.write_text(json.dumps(data),encoding='utf-8')

    def run_cli(self,*args):
        out=io.StringIO();err=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=main(['member',*args,'--workspace',str(self.root)])
        return code,out.getvalue(),err.getvalue()

    def test_invite_and_status_real_entry(self):
        code,text,err=self.run_cli('invite','--caller','codex','--member','peer','--request-id','r-1')
        self.assertEqual(code,0,err);value=json.loads(text);self.assertEqual(value['state'],'committed')
        code,text,err=self.run_cli('status','--invite-id',value['invite_id'])
        self.assertEqual(code,0,err);self.assertFalse(json.loads(text)['capability_passed'])

    def test_stop_conflict_is_nonzero_and_has_no_task(self):
        (self.root/'.agentbus/STOP').write_text('stop')
        code,text,err=self.run_cli('invite','--caller','codex','--member','peer','--request-id','r-1')
        self.assertEqual(code,4);self.assertFalse(json.loads(text).get('task_id'))

    def test_missing_input_reports_actionable_error(self):
        code,text,err=self.run_cli('invite','--caller','codex','--member','peer')
        self.assertEqual(code,4);self.assertIn('request-id',err)

    def test_public_card_library_import_requires_registered_member(self):
        card=dict(schema_version=1,member_id='peer',display_name='Peer',host_version=None,
          model_id=None,route='desktop-file',tools=['read'],specialties=['review'],limitations=['write unverified'],
          samples=[],source='actual self-report',updated_at='2026-10-05T10:00:00+08:00')
        (self.root/'card.json').write_text(json.dumps(card),encoding='utf-8');library=self.root/'public-library'
        code,text,err=self.run_cli('library-save','--card-file','card.json','--library-root',str(library),'--expected-sha256','MISSING')
        self.assertEqual(code,0,err)
        code,text,err=self.run_cli('import','--caller','codex','--member','peer','--library-root',str(library),'--expected-sha256','MISSING')
        self.assertEqual(code,0,err)
        code,text,err=self.run_cli('card','--member','peer');self.assertEqual(code,0,err)
        self.assertEqual(json.loads(text)['member_id'],'peer')
        self.assertNotIn('permission',text)

    def test_unknown_member_refused(self):
        code,text,err=self.run_cli('invite','--caller','codex','--member','ghost','--request-id','r-1')
        self.assertEqual(code,4);self.assertIn('UNKNOWN_MEMBER',err)

    def test_duplicate_card_keys_rejected_before_library_write(self):
        (self.root/'duplicate.json').write_text('{"schema_version":1,"schema_version":1}',encoding='utf-8')
        library=self.root/'public-library'
        code,text,err=self.run_cli('library-save','--card-file','duplicate.json','--library-root',str(library),'--expected-sha256','MISSING')
        self.assertEqual(code,4);self.assertIn('INVALID_CARD',err)
        self.assertFalse(library.exists())


if __name__ == '__main__':
    unittest.main()
