"""Desktop strict-audit actions must use original review claims and real commits."""
import json
import unittest
from unittest.mock import patch
from agentbus.bus import Bus, BusError
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge
import test_supervision_flow as fixtures

class DesktopAuditRegression(unittest.TestCase):
    setUp=fixtures.StrictSupervisionFlowTests.setUp
    enable_supervision=fixtures.StrictSupervisionFlowTests.enable_supervision
    _write_unittest_check=fixtures.StrictSupervisionFlowTests._write_unittest_check

    def pending(self):
        self.enable_supervision()
        forest=TaskForest(self.cfg)
        forest.seed_leaf({'id':'work','title':'Audited output','expected_artifacts':['output.txt']},branch_id='branch')
        bridge=WebBridge(self.workspace)
        bridge._claim_leaf({'leaf_id':'work','agent':'worker'})
        bridge._submit_leaf_result({'leaf_id':'work','agent':'worker','summary':'Actual output ready',
                                    'artifact_paths':['output.txt'],'operator_confirmed':True})
        return bridge,forest

    def decision(self,bridge,**changes):
        payload={'leaf_id':'work','agent':'reviewer','decision':'approve','reason':'Independently checked output and tests',
                 'artifacts_reviewed':True,'checks_reviewed':True,'text_scan_reviewed':True,'operator_confirmed':True}
        payload.update(changes)
        return bridge.call('submit_audit',payload)

    def test_approve_uses_a_formal_review_claim_and_closes_worker_task(self):
        bridge,forest=self.pending()
        self.assertEqual(forest.snapshot()['leaves'][0]['state'],'awaiting_audit')
        result=bridge.call('claim_audit',{'leaf_id':'work','agent':'reviewer'})
        self.assertTrue(result['ok'],result)
        self.assertNotIn('claim_id',json.dumps(result))
        result=self.decision(bridge)
        self.assertTrue(result['ok'],result)
        self.assertEqual(forest.snapshot()['leaves'][0]['state'],'delivered')
        self.assertTrue(any(e.get('event')=='ui_operator_submitted_audit' for e in self.bus.events()))

    def test_rejection_keeps_explicit_reason_and_does_not_deliver(self):
        bridge,forest=self.pending()
        self.assertTrue(bridge.call('claim_audit',{'leaf_id':'work','agent':'reviewer'})['ok'])
        result=self.decision(bridge,decision='reject',reason='Needs a clearer acceptance case')
        self.assertTrue(result['ok'],result)
        leaf=forest.snapshot()['leaves'][0]
        self.assertEqual(leaf['state'],'rejected')
        self.assertIn('Needs a clearer acceptance case',leaf['results'])

    def test_self_review_and_non_auditor_cannot_claim(self):
        bridge,forest=self.pending()
        for agent in ('worker','codex'):
            result=bridge.call('claim_audit',{'leaf_id':'work','agent':agent})
            self.assertFalse(result['ok'],result)
        self.assertEqual(forest.snapshot()['leaves'][0]['state'],'awaiting_audit')

    def test_new_window_cannot_reconstruct_previous_review_ticket(self):
        bridge,forest=self.pending()
        self.assertTrue(bridge.call('claim_audit',{'leaf_id':'work','agent':'reviewer'})['ok'])
        other=WebBridge(self.workspace)
        self.assertFalse(other.call('claim_audit',{'leaf_id':'work','agent':'reviewer'})['ok'])
        self.assertFalse(self.decision(other)['ok'])
        self.assertEqual(forest.snapshot()['leaves'][0]['state'],'awaiting_audit')

    def test_context_is_readonly_and_checkboxes_are_not_coerced(self):
        bridge,forest=self.pending()
        before={str(p):p.read_bytes() for p in self.workspace.rglob('*') if p.is_file()}
        info=bridge.call('audit_context',{'leaf_id':'work'})
        self.assertTrue(info['ok'],info)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.workspace.rglob('*') if p.is_file()})
        bridge.call('claim_audit',{'leaf_id':'work','agent':'reviewer'})
        for change in ({'operator_confirmed':False},{'checks_reviewed':'true'},{'reason':''}):
            self.assertFalse(self.decision(bridge,**change)['ok'])
        self.assertEqual(forest.snapshot()['leaves'][0]['state'],'awaiting_audit')

    def test_changed_artifact_cannot_be_approved_from_desktop(self):
        bridge,forest=self.pending()
        bridge.call('claim_audit',{'leaf_id':'work','agent':'reviewer'})
        self.artifact_path.write_text('changed after review',encoding='utf-8')
        self.assertFalse(self.decision(bridge)['ok'])
        self.assertNotEqual(forest.snapshot()['leaves'][0]['state'],'delivered')

    def test_failure_after_review_archive_gives_original_ticket_recovery_guidance(self):
        bridge,forest=self.pending()
        bridge.call('claim_audit',{'leaf_id':'work','agent':'reviewer'})
        original=Bus._finish_receipt
        def fail_source(bus,agent,current,*args,**kwargs):
            if agent=='worker':raise BusError('isolated source completion write failure')
            return original(bus,agent,current,*args,**kwargs)
        with patch.object(Bus,'_finish_receipt',fail_source):
            self.assertFalse(self.decision(bridge)['ok'])
        info=bridge.call('audit_context',{'leaf_id':'work'})
        self.assertFalse(info['ok'])
        self.assertIn('原票据',str(info))
        self.assertIn('重试 answer',str(info))
