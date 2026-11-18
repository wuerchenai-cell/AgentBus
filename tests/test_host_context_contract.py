"""Explicit host context and mandatory dual-scope release contract."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import project_paths

class HostContextContractTests(unittest.TestCase):
 def test_source_root_is_never_inferred_from_snapshot_ancestors(self):
  with patch.dict(os.environ,{},clear=True):self.assertIsNone(project_paths.host_source_root())
 def test_source_root_invalid_explicit_value_fails(self):
  with tempfile.TemporaryDirectory() as d:
   with patch.dict(os.environ,{'AGENTBUS_TEST_SOURCE_ROOT':d},clear=True):
    with self.assertRaises(ValueError):project_paths.host_source_root()
 def test_explicit_public_source_root_is_returned(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'agentbus').mkdir();(root/'agentbus/__init__.py').write_text('# fixture');(root/'tools').mkdir();(root/'tests').mkdir()
   with patch.dict(os.environ,{'AGENTBUS_TEST_SOURCE_ROOT':d},clear=True):self.assertEqual(project_paths.host_source_root(),root.resolve())
 def test_private_source_path_rejected_before_existence_check(self):
  with patch.dict(os.environ,{'AGENTBUS_TEST_SOURCE_ROOT':'C:/.workbuddy/private'},clear=True):
   with self.assertRaises(ValueError):project_paths.host_source_root()
 def test_release_environment_binds_exact_source_and_project(self):
  from tools.build_release import build_offline_test_env
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'.agentbus').mkdir();(root/'.agentbus/config.json').write_text('{}')
   env=build_offline_test_env(root,{},source_root=root/'candidate')
   self.assertEqual(env['AGENTBUS_TEST_SOURCE_ROOT'],str((root/'candidate').resolve()))
   self.assertEqual(env['AGENTBUS_TEST_PROJECT_ROOT'],str(root.resolve()))
 def run_scope(self,args,extra=None):
  root=Path(__file__).resolve().parents[1]
  env=dict(os.environ);env.pop('AGENTBUS_TEST_PROJECT_ROOT',None);env.pop('AGENTBUS_TEST_SOURCE_ROOT',None)
  env.update(extra or {})
  return subprocess.run([sys.executable,'-B','-X','utf8',str(root/'tools/check_offline.py'),*args],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
 def test_host_scope_without_explicit_context_cannot_pass(self):
  result=self.run_scope(['--scope','host'])
  self.assertEqual(result.returncode,2,result.stdout+result.stderr);self.assertIn('explicit',result.stderr)
 def test_host_scope_cannot_be_narrowed_to_hide_a_gate(self):
  result=self.run_scope(['--scope','host','--pattern','test_project_paths.py'])
  self.assertEqual(result.returncode,2,result.stdout+result.stderr);self.assertIn('cannot be narrowed',result.stderr)
 def test_portable_scope_scrubs_inherited_host_and_reports_its_limit(self):
  result=self.run_scope(['--scope','portable','--pattern','test_project_paths.py'],{'AGENTBUS_TEST_PROJECT_ROOT':'missing-stale-host','AGENTBUS_TEST_SOURCE_ROOT':'missing-stale-source'})
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  report=json.loads(next(x for x in result.stdout.splitlines() if x.startswith('{')))
  self.assertFalse(report['host_validation_performed']);self.assertEqual(report['scope'],'portable')
  self.assertEqual(report['normal_release_requires'],['portable','host'])

if __name__=='__main__':unittest.main(verbosity=2)
