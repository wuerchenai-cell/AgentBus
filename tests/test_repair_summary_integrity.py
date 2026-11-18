import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agentbus.trigger.contacts import checkin_contact

class SummaryIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name)
        self.folder=self.ws/'.agentbus/agents/alpha/done';self.folder.mkdir(parents=True)
    def checkin(self):
        return checkin_contact(self.ws, 'alpha', session_id='actual-fixture-chat', notify_coordinator=False)['briefing']
    def test_summary_uses_result_and_ignores_original_task_instruction(self):
        (self.folder/'m-one.json').write_text(json.dumps({'id':'m-one', 'title':'Task',
            'body':'Original requested work', 'result':'Actual completed work'}), encoding='utf-8')
        self.assertEqual(self.checkin()['recent_done_tasks'][0]['summary'], 'Actual completed work')
    def test_newest_completion_is_not_lost_after_1000_older_records(self):
        for n in range(1002):
            (self.folder/f'm-{n:04}.json').write_text(json.dumps({'id':f'm-{n:04}', 'body':'older'}), encoding='utf-8')
        events=self.ws/'.agentbus/events.jsonl'
        events.write_text(json.dumps({'event':'done','agent':'alpha','id':'m-1001','ts':'2099-01-01T00:00:00Z'})+'\n', encoding='utf-8')
        self.assertIn('m-1001', [x['id'] for x in self.checkin()['recent_done_tasks']])
    def test_nonobject_json_and_event_do_not_crash_checkin(self):
        (self.folder/'m-malformed.json').write_text('[]')
        (self.ws/'.agentbus/events.jsonl').write_text('[]\n')
        brief=self.checkin()
        self.assertEqual(brief['recent_done_tasks'], [])
