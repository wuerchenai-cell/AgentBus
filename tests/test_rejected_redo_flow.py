from pathlib import Path
import sys
_SOURCE = Path(__file__).resolve().parents[1]
for _entry in (str(_SOURCE), str(_SOURCE / "tests")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
import hashlib,unittest
import test_review_20261108_desktop_audit as audit_api
import test_supervision_flow as supervision_api
from agentbus import jobs
from agentbus.claim_fencing import captured_ticket
from agentbus.bus import BusError,LateResultRejected

class RejectedRedoFlowTests(unittest.TestCase):
    def test_rejected_work_can_redo_in_linked_job_and_get_a_fresh_independent_audit(self):
        c=audit_api.DesktopAuditRegression();c.setUp()
        try:
            bridge,forest=c.pending();old_leaf=forest.snapshot()['leaves'][0];old=c.bus.find_message(old_leaf['task_id']);old_ticket=captured_ticket(old)
            self.assertTrue(bridge.call('claim_audit',{'leaf_id':'work','agent':'reviewer'})['ok'])
            self.assertTrue(c.decision(bridge,decision='reject',reason='Need a clearer result')['ok'])
            old=c.bus.find_message(old.id)
            old_record=c.bus.dirs('worker')['blocked']/(old.id+'.json');job_path=c.workspace/'.agentbus/jobs'/(old.job_id+'.json')
            frozen={p:p.read_bytes() for p in [old_record,job_path]}
            child=jobs.start_job(c.cfg,'codex','Explicitly redo rejected output following reviewer notes',request_id='redo-rejected-output',parent_job_id=old.job_id)
            dispatched=jobs.dispatch_job(c.cfg,'codex',child['id'],[{'worker':'worker','title':'Revise rejected output','body':'Write improved output.txt addressing the independent rejection.','result_to':'codex','expected_artifacts':['output.txt']}])
            new_id=dispatched['tasks'][0]['id'];self.assertNotEqual(new_id,old.id)
            current=c.bus.claim('worker',c.bus.dirs('worker')['ready']/(new_id+'.json'));ticket=captured_ticket(current)
            c.bus.heartbeat('worker',current,ticket,note='new revision work')
            with self.assertRaises((BusError,LateResultRejected)):c.bus.commit_result('worker',current,old_ticket,body='old ticket cannot finish new work')
            c.artifact_path.write_text('improved output responding to independent reviewer\n',encoding='utf8')
            raw=c.artifact_path.read_bytes();c.artifacts=[{'path':'output.txt','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]
            supervision_api.StrictSupervisionFlowTests.submit_work(c,current,body='Revised output, ready for new independent review')
            active=c.bus._active_record('worker',new_id);self.assertEqual(active.status,'awaiting_audit')
            self.assertNotEqual(active.supervision['case_id'],old.supervision['case_id'])
            review=supervision_api.StrictSupervisionFlowTests.audit_task(c,active)
            self.assertNotEqual(review.id,old.supervision['review_task_id'])
            supervision_api.StrictSupervisionFlowTests.decide(c,review,'approve',reason='Independently checked revised output')
            done=c.bus.read(c.bus.dirs('worker')['done']/(new_id+'.json'));self.assertEqual(done.status,'done');self.assertEqual(done.supervision['state'],'approved')
            self.assertEqual(frozen,{p:p.read_bytes() for p in frozen})
            self.assertEqual(forest.snapshot()['leaves'][0]['state'],'rejected')
        finally:c.doCleanups()
