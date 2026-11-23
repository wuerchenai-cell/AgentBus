import contextlib,io,zipfile,unittest
import test_repair_artifact_versions as fixtures
from agentbus.cli import main

class VersionExportTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.ArtifactVersionRepairTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.f.revisions();self.f.finish('accept')
 def call(self):
  out=io.StringIO()
  with contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):
   rc=main(['tree','artifacts','--workspace',str(self.f.ws),'--export','final.zip'])
  return rc,out.getvalue()
 def test_export_uses_one_current_verified_version_and_preserves_history(self):
  rc,out=self.call();self.assertEqual(rc,0,out)
  with zipfile.ZipFile(self.f.ws/'final.zip') as z:
   self.assertEqual(z.namelist(),['module.py'])
   self.assertEqual(z.read('module.py'),b'VERSION = 2\n')
 def test_unapproved_latest_change_still_prevents_export(self):
  (self.f.ws/'module.py').write_bytes(b'UNAUTHORIZED\n')
  rc,out=self.call();self.assertNotEqual(rc,0,out)
 def test_summary_distinguishes_historical_and_current_hashes(self):
  out=io.StringIO()
  with contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):
   rc=main(['tree','artifacts','--workspace',str(self.f.ws),'--summary-file','summary.md'])
  self.assertEqual(rc,0,out.getvalue())
  text=(self.f.ws/'summary.md').read_text(encoding='utf-8')
  self.assertIn('历史版本（已被合法后继替代）',text)
  self.assertIn('当前版本',text)
  self.assertIn('历史摘要仅用于追溯',text)
 def test_export_paths_cannot_overwrite_source_or_each_other(self):
  from agentbus.web_bridge import WebBridge
  source=self.f.ws/'module.py'
  before=source.read_bytes()
  for archive, report in [('copy.zip','copy.zip'),('output','output/module.py'),('copy.zip','module.py')]:
   with self.subTest(archive=archive,report=report),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    rc=main(['tree','artifacts','--workspace',str(self.f.ws),'--export',archive,'--summary-file',report])
    self.assertNotEqual(rc,0)
    self.assertEqual(source.read_bytes(),before)
    self.assertFalse((self.f.ws/archive).exists())
  bridge=WebBridge(self.f.ws)
  dest=self.f.ws/'已验收成果.zip'
  outcome=bridge.call('export_delivery',{'path':str(dest)})
  self.assertTrue(outcome['ok'],outcome)
  with zipfile.ZipFile(dest) as bundle:
   self.assertEqual(bundle.read('module.py'),before)
 def test_summary_cannot_overwrite_hardlinked_artifact(self):
  import os
  alias=self.f.ws/'alias.md'
  alias_source=self.f.ws/'module.py'
  os.link(alias_source,alias)
  before=alias_source.read_bytes()
  with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   self.assertNotEqual(main(['tree','artifacts','--workspace',str(self.f.ws),'--summary-file',str(alias)]),0)
  self.assertEqual(alias_source.read_bytes(),before)
 def test_notification_must_reference_the_current_mail(self):
  from unittest.mock import Mock
  from agentbus.web_bridge import WebBridge
  service=Mock()
  cases=[
      ({'status':'no_bound_contact'},'waiting'),
      ({'status':'queued','submit_status':'queued'},'waiting'),
      ({'status':'ok','submit_status':'notified','notified_task_id':'old'},'waiting'),
      ({'status':'ok','submit_status':'notified','notified_task_id':'new'},'notified'),
      ({'status':'ok','submit_status':'accepted','accepted_by_host':True,'notified_task_id':'new'},'accepted'),
      ({'status':'error'},'failed'),
  ]
  for outcome,expected in cases:
   with self.subTest(outcome=outcome):
    service.trigger_for_member.return_value=outcome
    self.assertEqual(WebBridge._wake_after_dispatch(service,'codex','new'),expected)
if __name__=='__main__':unittest.main()
