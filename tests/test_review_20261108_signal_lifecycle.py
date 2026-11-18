"""Real wait consumption, contact replacement and pending local wake lifecycle."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.cli import main
from agentbus.config import load
from agentbus.trigger.contacts import checkin_contact,load_contact
from agentbus.trigger.service import TriggerDeliveryService

class SignalLifecycleRegression(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.ws=Path(tmp.name);load(self.ws,create=True)
        self.bus=Bus(self.ws/'.agentbus');self.bus.init()
        self.service=TriggerDeliveryService(self.ws)
        self.signal=self.ws/'.agentbus/triggers/wake-signals/workbuddy.json'
        self.env=patch.dict('os.environ',{'AGENTBUS_DISABLE_DESKTOP_POPUP':'1'});self.env.start();self.addCleanup(self.env.stop)
        checkin_contact(self.ws,'workbuddy',session_id='old-window',notify_coordinator=False)

    def tasks(self):
        result=[]
        for index in range(2):
            task=self.bus.post(self.bus.make('codex','workbuddy','task','Work '+str(index),'Actual work',result_to='codex'))
            result.append(task)
        return result

    def notify(self):
        first,second=self.tasks()
        self.service.trigger_for_member('workbuddy',first.id,count_hop=False)
        self.service.trigger_for_member('workbuddy',second.id,count_hop=False)
        return first,second,json.loads(self.signal.read_text(encoding='utf-8'))['request_id']

    def wait(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):
            rc=main(['wait','--workspace',str(self.ws),'--agent','workbuddy','--timeout','0.02','--poll-interval','0.2'])
        return rc,out.getvalue()

    def test_rebind_retires_proven_local_notifications_and_old_queued_sources(self):
        first,second,request_id=self.notify()
        old=self.signal.read_bytes()
        checkin_contact(self.ws,'workbuddy',session_id='new-window',notify_coordinator=False)
        self.assertFalse(self.signal.exists())
        archived=list((self.ws/'.agentbus/triggers/signal-history/workbuddy').glob('*.json'))
        self.assertTrue(any(p.read_bytes()==old for p in archived))
        self.assertEqual(self.service.journal.get_request_status(request_id)['status'],'cancelled')
        scan=self.service.scan_and_deliver_pending()
        self.assertEqual(scan['notified'],1,scan)
        data=json.loads(self.signal.read_text(encoding='utf-8'))
        self.assertEqual(data['session_id'],'new-window')
        self.assertIn(data['source_message_ids'][0],[first.id,second.id])

    def test_repeated_checkin_keeps_current_notification_bytes(self):
        self.notify();before=self.signal.read_bytes()
        checkin_contact(self.ws,'workbuddy',session_id='old-window',notify_coordinator=False)
        self.assertEqual(before,self.signal.read_bytes())

    def test_wait_records_consumption_but_fifo_waits_for_actual_work_finish(self):
        first,second,request_id=self.notify()
        self.assertEqual(self.wait()[0],0)
        self.assertFalse(self.signal.exists())
        details=self.service.journal.get_request_status(request_id)['details']
        self.assertTrue(details.get('notification_consumed'))
        self.assertTrue(self.service.is_member_busy(load_contact(self.ws,'workbuddy')))
        self.assertEqual(self.service.scan_and_deliver_pending()['notified'],0)
        self.assertNotEqual(self.wait()[0],0)
        claimed=self.bus.claim('workbuddy',self.bus.dirs('workbuddy')['ready']/(first.id+'.json'))
        ticket=captured_ticket(claimed);self.bus.heartbeat('workbuddy',claimed,ticket,note='Actual work')
        self.bus.commit_result('workbuddy',claimed,ticket,body='Actual work completed')
        self.assertEqual(json.loads(self.signal.read_text(encoding='utf-8'))['source_message_ids'],[second.id])
        self.assertEqual(self.service.trigger_for_member('workbuddy',second.id,count_hop=False)['status'],'duplicate')

    def test_stopped_missing_contact_and_bad_json_are_preserved(self):
        self.notify();before=self.signal.read_bytes()
        self.service.set_stopped(True)
        self.assertNotEqual(self.wait()[0],0);self.assertEqual(before,self.signal.read_bytes())
        checkin_contact(self.ws,'workbuddy',session_id='new-window',notify_coordinator=False)
        self.assertEqual(before,self.signal.read_bytes())
        self.service.set_stopped(False)
        (self.ws/'.agentbus/agents/workbuddy/contact.json').unlink()
        code,text=self.wait();self.assertNotEqual(code,0);self.assertIn('TARGET_MISSING',text)
        self.assertEqual(before,self.signal.read_bytes())
        self.signal.write_bytes(b'{broken')
        code,text=self.wait();self.assertNotEqual(code,0);self.assertIn('CORRUPT',text)
        self.assertEqual(self.signal.read_bytes(),b'{broken')

    def test_uncertain_host_submission_is_not_cancelled_or_requeued_on_rebind(self):
        _,_,request_id=self.notify()
        self.service.journal.transition(request_id,'uncertain',{'uncertain_reason':'unknown_host_outcome'})
        before=self.signal.read_bytes()
        checkin_contact(self.ws,'workbuddy',session_id='new-window',notify_coordinator=False)
        self.assertEqual(before,self.signal.read_bytes())
        self.assertEqual(self.service.journal.get_request_status(request_id)['status'],'uncertain')

    def test_one_unreadable_contact_does_not_block_other_members(self):
        checkin_contact(self.ws,'trae',session_id='second-member',notify_coordinator=False)
        task=self.bus.post(self.bus.make('codex','trae','task','Other member','Work',result_to='codex'))
        original=load_contact
        def load_one(workspace,member):
            if member=='workbuddy':raise PermissionError('isolated unreadable contact')
            return original(workspace,member)
        with patch('agentbus.trigger.contacts.load_contact',side_effect=load_one):
            result=self.service.scan_and_deliver_pending()
        self.assertEqual(result['notified'],1,result)
        self.assertTrue(result['errors'])
        self.assertTrue((self.ws/'.agentbus/triggers/wake-signals/trae.json').is_file())

    def test_uncertain_host_without_local_signal_is_not_made_idle_by_bus_callback(self):
        _,_,request_id=self.notify()
        self.signal.unlink()  # Model a native host request, not a local-notification slot.
        self.service.journal.transition(request_id,'uncertain',{'uncertain_reason':'host_response_lost'})
        with patch.object(self.service,'_get_connector') as connector:
            result=self.service.on_work_state_changed('workbuddy')
        self.assertEqual(result['status'],'busy')
        connector.assert_not_called()
        self.assertEqual(self.service.journal.get_request_status(request_id)['status'],'uncertain')

    def test_signal_cannot_cancel_or_consume_another_requests_journal(self):
        _,_,request_id=self.notify()
        data=json.loads(self.signal.read_text(encoding='utf-8'))
        data['source_message_ids']=['m-wrong-source']
        self.signal.write_text(json.dumps(data),encoding='utf-8')
        before=self.signal.read_bytes()
        code,text=self.wait()
        self.assertNotEqual(code,0)
        self.assertIn('CORRUPT',text)
        self.assertEqual(self.signal.read_bytes(),before)
        self.assertNotIn('notification_consumed',self.service.journal.get_request_status(request_id)['details'])

    def test_legacy_direct_notification_waits_for_actual_completion(self):
        from agentbus.trigger.connectors.universal import UniversalWakeupConnector
        from agentbus.trigger.wake import make_member_wakeup
        first,second=self.tasks()
        contact=load_contact(self.ws,'workbuddy')
        UniversalWakeupConnector().submit(self.ws,contact,make_member_wakeup(self.ws,'workbuddy',first.id,contact))
        self.assertEqual(self.wait()[0],0)
        self.assertEqual(self.service.on_work_state_changed('workbuddy')['status'],'busy')
        self.assertTrue(self.service.is_member_busy(contact))
        self.assertEqual(self.service.scan_and_deliver_pending()['notified'],0)
        self.assertEqual(self.service.on_work_state_changed('workbuddy')['status'],'busy')
        claimed=self.bus.claim('workbuddy',self.bus.dirs('workbuddy')['ready']/(first.id+'.json'))
        ticket=captured_ticket(claimed);self.bus.heartbeat('workbuddy',claimed,ticket,note='Actual work')
        self.bus.commit_result('workbuddy',claimed,ticket,body='Actual completed work')
        self.assertEqual(json.loads(self.signal.read_text(encoding='utf-8'))['source_message_ids'],[second.id])

    def test_consumption_record_before_archive_crash_does_not_wake_twice(self):
        _,_,request_id=self.notify()
        self.service.journal.transition(request_id,'submitted',{'notification_consumed':True})
        code,text=self.wait()
        self.assertNotEqual(code,0)
        self.assertIn('ALREADY_CONSUMED',text)
        self.assertFalse(self.signal.exists())
