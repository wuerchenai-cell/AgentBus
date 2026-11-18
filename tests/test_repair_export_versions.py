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
if __name__=='__main__':unittest.main()
