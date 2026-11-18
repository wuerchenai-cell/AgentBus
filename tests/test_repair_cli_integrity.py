"""Owner-approved audit counterexamples; real local CLI/Bus, no model calls."""
import contextlib,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agentbus.bus import Bus
from agentbus.config import load,save
from agentbus.claim_fencing import captured_ticket
from agentbus.cli import main,_prepare_reply_draft
from agentbus.trigger.contacts import checkin_contact,load_contact
from agentbus.trigger.connectors.universal import UniversalWakeupConnector
from agentbus.trigger.wake import make_member_wakeup
from agentbus.trigger.service import TriggerDeliveryService

class CliIntegrity(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.ws=Path(self.tmp.name);self.cfg=load(self.ws,create=True);save(self.cfg)
  self.b=Bus(self.ws/'.agentbus');self.b.init()
  TriggerDeliveryService(self.ws).set_coordinator_route('existing_executor')
  self.env=patch.dict(os.environ,{'AGENTBUS_DISABLE_DESKTOP_POPUP':'1'});self.env.start();self.addCleanup(self.env.stop)
 def call(self,*args):
  o=io.StringIO()
  with contextlib.redirect_stdout(o),contextlib.redirect_stderr(o):rc=main([*args,'--workspace',str(self.ws)])
  return rc,o.getvalue()
 def task(self):
  t=self.b.make('codex','workbuddy','task',title='Real task',body='Write actual result',result_to='codex');self.b.post(t)
  c=self.b.claim('workbuddy',self.b.pending('workbuddy')[0]);ticket=captured_ticket(c)
  self.b.heartbeat('workbuddy',c,ticket,note='Actual fixture claimed')
  return c,ticket
 def test_no_ticket_implicit_answer_cannot_steal_new_claim(self):
  c,t=self.task();self.b.recover_claim('workbuddy',c.id,owner_confirmed_inactive=True)
  n=self.b.claim('workbuddy',self.b.pending('workbuddy')[0]);nt=captured_ticket(n)
  self.b.heartbeat('workbuddy',n,nt,note='New claim')
  rc,out=self.call('answer','--agent','workbuddy','--body','old conversation')
  self.assertNotEqual(rc,0,out);self.assertEqual(self.b.find_results(c.id),[])
 def test_recovered_ready_task_is_not_auto_claimed_by_old_answer(self):
  c,t=self.task();self.b.recover_claim('workbuddy',c.id,owner_confirmed_inactive=True)
  rc,out=self.call('answer','--agent','workbuddy','--id',c.id,'--body','old result without reclaim')
  self.assertNotEqual(rc,0,out);self.assertEqual(self.b.find_results(c.id),[])
 def test_no_ticket_heartbeat_cannot_refresh_another_claim(self):
  c,t=self.task();before=list((self.b.root/'heartbeats'/c.id).glob('*.json'))[0].read_bytes()
  rc,out=self.call('heartbeat','--agent','workbuddy')
  self.assertNotEqual(rc,0,out)
  self.assertEqual(list((self.b.root/'heartbeats'/c.id).glob('*.json'))[0].read_bytes(),before)
 def test_implicit_default_draft_cannot_supply_new_current_ticket(self):
  c,t=self.task();rel=_prepare_reply_draft(self.cfg,'workbuddy',c)
  p=self.ws/rel;p.write_text(p.read_text(encoding='utf-8').replace('AGENTBUS_DRAFT_UNFINISHED','COMPLETE').replace('（填写实际完成内容；未完成或受阻请如实说明。）','Actual fixture result checked.'),encoding='utf-8')
  rc,out=self.call('answer','--agent','workbuddy','--auto-manifest')
  self.assertNotEqual(rc,0,out);self.assertEqual(self.b.find_results(c.id),[])
 def test_old_draft_header_conflicts_with_explicit_new_ticket(self):
  c,t=self.task();rel=_prepare_reply_draft(self.cfg,'workbuddy',c)
  p=self.ws/rel;p.write_text(p.read_text(encoding='utf-8').replace('AGENTBUS_DRAFT_UNFINISHED','COMPLETE').replace('（填写实际完成内容；未完成或受阻请如实说明。）','Actual fixture result checked.'),encoding='utf-8')
  self.b.recover_claim('workbuddy',c.id,owner_confirmed_inactive=True)
  n=self.b.claim('workbuddy',self.b.pending('workbuddy')[0]);nt=captured_ticket(n)
  self.b.heartbeat('workbuddy',n,nt,note='new claim')
  rc,out=self.call('answer','--agent','workbuddy','--id',c.id,'--claim-epoch',str(nt.claim_epoch),'--claim-id',nt.claim_id,'--body-file',rel,'--auto-manifest')
  self.assertNotEqual(rc,0,out);self.assertEqual(self.b.find_results(c.id),[])
 def test_generated_draft_is_neutral_and_not_submittable_untouched(self):
  c,t=self.task();rel=_prepare_reply_draft(self.cfg,'workbuddy',c)
  text=(self.ws/rel).read_text(encoding='utf-8')
  self.assertIn('agentbus-claim-v1:',text)
  self.assertNotIn('已完成 `Real task`',text)
  self.assertNotIn('已在本地工作区完成验证',text)
  rc,out=self.call('answer','--agent','workbuddy','--id',c.id,'--claim-epoch',str(t.claim_epoch),'--claim-id',t.claim_id,'--body-file',rel,'--auto-manifest')
  self.assertNotEqual(rc,0,out)
 def test_original_ticket_in_edited_draft_can_submit_without_manual_flags(self):
  c,t=self.task();rel=_prepare_reply_draft(self.cfg,'workbuddy',c)
  p=self.ws/rel;text=p.read_text(encoding='utf-8')
  text=text.replace('AGENTBUS_DRAFT_UNFINISHED','AGENTBUS_DRAFT_COMPLETED')
  text=text.replace('（填写实际完成内容；未完成或受阻请如实说明。）','Actual fixture result checked.')
  p.write_text(text+'\nActual fixture result checked.\n',encoding='utf-8')
  rc,out=self.call('answer','--agent','workbuddy','--body-file',rel,'--auto-manifest')
  self.assertEqual(rc,0,out)
  self.assertEqual(self.b.find_results(c.id)[0].source_claim_id,t.claim_id)
 def test_wait_consumes_real_current_signal_and_exits_success(self):
  checkin_contact(self.ws,'workbuddy',session_id='local-current',notify_coordinator=False)
  contact=load_contact(self.ws,'workbuddy');req=make_member_wakeup(self.ws,'workbuddy','m-1793760000000-aaaaaa',contact)
  UniversalWakeupConnector().submit(self.ws,contact,req)
  rc,out=self.call('wait','--agent','workbuddy','--timeout','0.2','--poll-interval','0.2')
  self.assertEqual(rc,0,out)
  self.assertFalse((self.ws/'.agentbus/triggers/wake-signals/workbuddy.json').exists())
 def test_wait_does_not_consume_old_window_signal(self):
  checkin_contact(self.ws,'workbuddy',session_id='local-old',notify_coordinator=False)
  old=load_contact(self.ws,'workbuddy');req=make_member_wakeup(self.ws,'workbuddy','m-1793760000000-bbbbbb',old)
  UniversalWakeupConnector().submit(self.ws,old,req)
  checkin_contact(self.ws,'workbuddy',session_id='local-new',notify_coordinator=False)
  rc,out=self.call('wait','--agent','workbuddy','--timeout','0.2','--poll-interval','0.2')
  self.assertNotEqual(rc,0,out);self.assertNotIn('[唤醒]',out)
 def test_stopped_wait_preserves_signal_without_waking(self):
  checkin_contact(self.ws,'workbuddy',session_id='local-current',notify_coordinator=False)
  c=load_contact(self.ws,'workbuddy');req=make_member_wakeup(self.ws,'workbuddy','m-1793760000000-cccccc',c)
  UniversalWakeupConnector().submit(self.ws,c,req)
  TriggerDeliveryService(self.ws).set_stopped(True)
  rc,out=self.call('wait','--agent','workbuddy','--timeout','0.2')
  self.assertNotEqual(rc,0,out);self.assertNotIn('[唤醒]',out)
  self.assertTrue((self.ws/'.agentbus/triggers/wake-signals/workbuddy.json').exists())
if __name__=='__main__':unittest.main()
