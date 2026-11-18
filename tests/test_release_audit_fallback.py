"""Noncritical notification auditing must not overturn a durable task state."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agentbus.bus import Bus
from agentbus.config import load
from agentbus.trigger.service import TriggerDeliveryService

class AuditFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name);load(self.ws,create=True)
        self.bus=Bus(self.ws/'.agentbus');self.bus.init()
    def test_best_effort_audit_retains_event_and_reports_failure(self):
        with patch.object(self.bus,'audit') as audit:
            self.assertTrue(self.bus._audit_best_effort(event='fixture',id='m-fixture'))
            audit.assert_called_once_with(event='fixture',id='m-fixture')
        with patch.object(self.bus,'audit',side_effect=OSError('read-only log')):
            self.assertFalse(self.bus._audit_best_effort(event='fixture'))
    def test_state_notification_and_audit_failure_do_not_raise(self):
        with patch.object(TriggerDeliveryService,'on_work_state_changed',side_effect=RuntimeError('host disconnected')), \
             patch.object(self.bus,'audit',side_effect=OSError('read-only log')):
            self.bus._sync_trigger_work_state('codex','m-fixture')
