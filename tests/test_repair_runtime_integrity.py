import tempfile,unittest,sys,time
from pathlib import Path
from unittest.mock import patch
from agentbus.bus import Bus
from agentbus.config import load,save
from agentbus.claim_fencing import captured_ticket
from agentbus import exclusive_lock as E
from agentbus.acp import ACPAdapter
from agentbus.adapters import AdapterError
from agentbus.supervision import run_allowlisted_checks
from agentbus.open_patterns import run_pre_answer_gate

class RuntimeIntegrity(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='中文运行审查-');self.addCleanup(self.tmp.cleanup)
  self.ws=Path(self.tmp.name);self.cfg=load(self.ws,create=True);save(self.cfg)
  self.b=Bus(self.ws/'.agentbus');self.b.init()
 def claim(self):
  m=self.b.make('codex','workbuddy','task',body='Local work',result_to='codex');self.b.post(m)
  c=self.b.claim('workbuddy',self.b.pending('workbuddy')[0]);return c,captured_ticket(c)
 def test_heartbeat_renews_corresponding_task_exclusive_lock(self):
  c,t=self.claim();E.acquire_task_lock(self.b.root,c.id,'workbuddy',t,ttl_s=.5)
  before=E.get_task_lock_info(self.b.root,c.id)['expires_ts']
  self.b.heartbeat('workbuddy',c,t,note='Actual progress')
  self.assertGreater(E.get_task_lock_info(self.b.root,c.id)['expires_ts'],before+100)
 def test_expired_exclusive_lock_cannot_pass_delivery_verification(self):
  c,t=self.claim();E.acquire_task_lock(self.b.root,c.id,'workbuddy',t,ttl_s=.05);time.sleep(.08)
  with self.assertRaises(E.ExclusiveLockError):E.verify_task_lock(self.b.root,c.id,'workbuddy',t)
 def test_existing_acp_session_is_not_replaced_by_new_session(self):
  a=ACPAdapter(['unused-local-fixture']);a.session_id='original-session';calls=[]
  def request(method,params,**kwargs):
   calls.append(method)
   return {'agentCapabilities':{'loadSession':False}} if method=='initialize' else {'sessionId':'WRONG-new-session'}
  with patch.object(a,'_launch'),patch.object(a,'_stop_process'),patch.object(a,'_notify'),patch.object(a,'_request',side_effect=request):
   with self.assertRaises(AdapterError):a.start(self.ws)
  self.assertNotIn('session/new',calls)
 def test_supervision_child_chinese_stdout_is_utf8(self):
  check={'argv':[sys.executable,'-c','print("中文运行核验")'],'timeout_seconds':10}
  r=run_allowlisted_checks(self.ws,[check])[0]
  self.assertEqual(r['stdout'].strip(),'中文运行核验');self.assertEqual(r['exit_code'],0)
 def test_legal_utf8_bom_python_is_not_rejected(self):
  (self.ws/'bom.py').write_text('value = 1\n',encoding='utf-8-sig')
  self.assertTrue(run_pre_answer_gate(self.ws,artifact_paths=['bom.py'])['passed'])
if __name__=='__main__':unittest.main()
