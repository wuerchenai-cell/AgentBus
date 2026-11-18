"""Export dependent public audit inputs without broad directory discovery."""
import json,os,unittest,zipfile
from unittest.mock import patch
import test_source_release as fixtures
from tools import export_source as release

class ExportAuditInputTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.SourceReleaseTests('test_whitelist_excludes_secrets_and_maps_public_readme')
  self.f.setUp();self.addCleanup(self.f.doCleanups)
 def export(self):return release.export_source(self.f.root,self.f.output)
 def test_missing_core_tool_rejects_without_replacing_previous(self):
  self.f.output.write_bytes(b'old archive');(self.f.root/'tools/check_offline.py').unlink()
  with self.assertRaises(release.SourceReleaseError):self.export()
  self.assertEqual(self.f.output.read_bytes(),b'old archive')
 def test_shipped_audit_test_requires_its_tool(self):
  self.f.write('tests/test_tree_capability_audit.py',b'# consumer\n')
  with self.assertRaises(release.SourceReleaseError):self.export()
 def test_explicit_tool_and_consumer_are_exported_with_original_digest(self):
  self.f.write('tests/test_tree_capability_audit.py',b'# consumer\n')
  self.f.write('tools/tree_capability_audit.py',b'# public tool\n');self.export()
  with zipfile.ZipFile(self.f.output) as z:self.assertEqual(z.read('tools/tree_capability_audit.py'),b'# public tool\n')
 def test_minimal_fixture_does_not_require_unshipped_audits(self):
  self.export();self.assertTrue(self.f.output.is_file())
 def test_shipped_workbench_test_requires_its_json_fixture(self):
  self.f.write('tests/test_workbench_invariants.py',b'# fixture consumer\n')
  with self.assertRaises(release.SourceReleaseError):self.export()
 def test_bad_json_fixture_rejects_preserving_previous(self):
  self.f.write('tests/test_workbench_invariants.py',b'# fixture consumer\n');self.f.write('tests/data/workbench_contract_surface.json',b'{bad')
  self.f.output.write_bytes(b'previous')
  with self.assertRaises(release.SourceReleaseError):self.export()
  self.assertEqual(self.f.output.read_bytes(),b'previous')
 def test_exact_fixture_only_without_sibling_secret(self):
  self.f.write('tests/test_workbench_invariants.py',b'# fixture consumer\n');self.f.write('tests/data/workbench_contract_surface.json',b'{"functions":{}}')
  self.f.write('tests/data/private.json',b'PRIVATE_CANARY');self.export()
  with zipfile.ZipFile(self.f.output) as z:
   self.assertEqual(json.loads(z.read('tests/data/workbench_contract_surface.json')),{'functions':{}});self.assertNotIn('tests/data/private.json',z.namelist())
 def test_audit_symlink_rejected_without_reading_target(self):
  self.f.write('tests/test_tree_capability_audit.py',b'# consumer\n');target=self.f.base/'outside.py';target.write_bytes(b'EXTERNAL_CANARY')
  link=self.f.root/'tools/tree_capability_audit.py'
  try:link.symlink_to(target)
  except OSError as exc:self.skipTest('Host cannot create test symlink: '+str(exc))
  with self.assertRaises(release.SourceReleaseError):self.export()
 def test_fixture_size_limit_unchanged(self):
  self.f.write('tests/test_workbench_invariants.py',b'# fixture consumer\n');self.f.write('tests/data/workbench_contract_surface.json',b' '+b'{}'*200)
  with patch.object(release,'MAX_FILE_BYTES',128):
   with self.assertRaises(release.SourceReleaseError):self.export()

if __name__=='__main__':unittest.main(verbosity=2)
