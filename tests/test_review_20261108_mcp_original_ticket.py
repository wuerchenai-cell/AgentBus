"""JR-20261108-D02: a new MCP connection must not borrow disk claim tickets."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from agentbus.bus import Bus, BusError
from agentbus import claim_fencing as fencing
from agentbus.config import load
from agentbus.mcp_server import AgentBusMcp


class McpOriginalTicketRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        load(self.workspace, create=True)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()
        self.task = self.bus.make('codex', 'workbuddy', 'task', body='review ticket', result_to='codex')
        self.bus.post(self.task)

    def external_claim(self):
        message = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        ticket = fencing.captured_ticket(message)
        return message, ticket

    def test_fresh_connection_without_original_ticket_cannot_submit(self):
        message, _ = self.external_claim()
        active = self.bus.processing('workbuddy')[0]
        original = active.read_bytes()
        client = AgentBusMcp(self.workspace)
        with self.assertRaisesRegex((ValueError, BusError), '票据|ticket'):
            client.report('workbuddy', message.id, 'new connection must not finish another claim')
        self.assertEqual(active.read_bytes(), original)
        self.assertEqual(self.bus.find_results(message.id), [])
        self.assertFalse((self.bus.dirs('workbuddy')['done'] / (message.id + '.json')).exists())
        self.assertEqual(client._tickets, {})

    def test_same_connection_can_use_its_successful_claim_ticket(self):
        client = AgentBusMcp(self.workspace)
        client.next_task('workbuddy')
        claimed = self.bus.read(self.bus.processing('workbuddy')[0])
        original_ticket = client._tickets[('workbuddy', claimed.id)]
        client.report('workbuddy', claimed.id, 'completed with the retained original ticket')
        results = self.bus.find_results(claimed.id)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source_claim_id, original_ticket.claim_id)
        self.assertEqual(self.bus.processing('workbuddy'), [])

    def test_fresh_connection_accepts_explicit_original_ticket(self):
        message, ticket = self.external_claim()
        client = AgentBusMcp(self.workspace)
        client.report('workbuddy', message.id, 'explicit original ticket', ticket=ticket.to_dict())
        results = self.bus.find_results(message.id)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source_claim_id, ticket.claim_id)

    def test_old_connection_never_refreshes_cached_ticket_after_recovery(self):
        first = AgentBusMcp(self.workspace)
        first.next_task('workbuddy')
        old_message = self.bus.read(self.bus.processing('workbuddy')[0])
        old_ticket = first._tickets[('workbuddy', old_message.id)]
        self.bus.recover_claim('workbuddy', old_message.id, owner_confirmed_inactive=True)
        second = AgentBusMcp(self.workspace)
        second.next_task('workbuddy')
        active = self.bus.processing('workbuddy')[0]
        original = active.read_bytes()
        with self.assertRaises((ValueError, BusError)):
            first.report('workbuddy', old_message.id, 'late response')
        self.assertEqual(active.read_bytes(), original)
        self.assertEqual(first._tickets[('workbuddy', old_message.id)], old_ticket)
        self.assertEqual(self.bus.find_results(old_message.id), [])
        self.assertTrue(fencing.list_quarantined(self.bus.root, old_message.id))

    def test_wrong_explicit_ticket_is_not_replaced_with_current_disk_ticket(self):
        message, ticket = self.external_claim()
        invalid = {**ticket.to_dict(), 'claim_id': '0' * 32}
        client = AgentBusMcp(self.workspace)
        with self.assertRaises((ValueError, BusError)):
            client.report('workbuddy', message.id, 'wrong ticket', ticket=invalid)
        self.assertEqual(self.bus.find_results(message.id), [])
        self.assertTrue(self.bus.processing('workbuddy'))


if __name__ == '__main__':
    unittest.main()
