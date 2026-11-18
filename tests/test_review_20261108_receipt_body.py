"""JR-20261108-D05: receipt bodies must contain work, but may quote examples."""
import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from agentbus.claim_fencing import captured_ticket
from agentbus.cli import main, _prepare_reply_draft
from agentbus.config import load
from agentbus.mcp_server import AgentBusMcp
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


SUMMARY_PLACEHOLDER = '（填写实际完成内容；未完成或受阻请如实说明。）'
DRAFT_MARKER = '<!-- AGENTBUS_DRAFT_UNFINISHED：完成实际工作后删除本行，再填写以下结论。 -->'


class ReceiptBodyRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.cfg = load(self.workspace, create=True)
        self.forest = TaskForest(self.cfg)
        self.forest.seed_leaf({'id': 'review-body', 'title': '说明实测结论'}, branch_id='checks')
        self.bridge = WebBridge(self.workspace)
        claimed = self.bridge._claim_leaf({'leaf_id': 'review-body', 'agent': 'workbuddy'})
        self.bus = self.forest.bus
        self.task = self.bus._active_record('workbuddy', claimed['task_id'])
        self.ticket = captured_ticket(self.task)
        self.bus.heartbeat('workbuddy', self.task, self.ticket, note='started review')

    def answer(self, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = main(['answer', '--workspace', str(self.workspace), '--agent', 'workbuddy', *options])
        return rc, output.getvalue()

    def test_cli_empty_result_has_no_side_effects(self):
        flags = ('--claim-epoch', str(self.ticket.claim_epoch), '--claim-id', self.ticket.claim_id)
        for value in ('', ' \t\n\u3000'):
            with self.subTest(value=value):
                rc, output = self.answer('--body', value, *flags)
                self.assertNotEqual(rc, 0, output)
                self.assertIn('空白', output)
                self.assertFalse(self.bus.find_results(self.task.id))
                self.assertEqual(self.bus._active_record('workbuddy', self.task.id).status, self.task.status)

    def test_real_unfilled_draft_is_rejected_but_valid_bom_body_can_quote_it(self):
        relative = _prepare_reply_draft(self.cfg, 'workbuddy', self.task)
        path = self.workspace / relative
        draft = path.read_text(encoding='utf-8')
        self.assertIn(DRAFT_MARKER, draft)
        for unfilled in (draft, draft.replace(DRAFT_MARKER + '\n', '', 1)):
            path.write_text(unfilled, encoding='utf-8')
            rc, output = self.answer('--body-file', relative, '--auto-manifest')
            self.assertNotEqual(rc, 0, output)
            self.assertFalse(self.bus.find_results(self.task.id))
        filled = draft.replace(DRAFT_MARKER + '\n', '', 1).replace(
            SUMMARY_PLACEHOLDER,
            f'实测已交付。原模板中的「{SUMMARY_PLACEHOLDER}」已替换。\n\n> {DRAFT_MARKER}',
            1,
        )
        path.write_bytes(filled.encode('utf-8-sig'))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rc, output = self.answer('--body-file', relative, '--expected-body-sha256', digest, '--auto-manifest')
        self.assertEqual(rc, 0, output)
        receipt, = self.bus.find_results(self.task.id)
        self.assertEqual(receipt.body, filled)
        self.assertEqual(receipt.source_claim_id, self.ticket.claim_id)

    def test_mcp_blank_report_and_direct_result_are_rejected(self):
        mcp = AgentBusMcp(self.workspace, identity='workbuddy', session_tools=True)
        with self.assertRaisesRegex(ValueError, '空白'):
            mcp.report('workbuddy', self.task.id, '   ', ticket=self.ticket.to_dict())
        with self.assertRaisesRegex(ValueError, '空白'):
            mcp.session_send(to='codex', body='', type='result', reply_to=self.task.id,
                             ticket=self.ticket.to_dict())
        self.assertFalse(self.bus.find_results(self.task.id))
        body = f'核对完成。示例原文为「{SUMMARY_PLACEHOLDER}」。'
        mcp.report('workbuddy', self.task.id, body, ticket=self.ticket.to_dict())
        self.assertEqual(self.bus.find_results(self.task.id)[0].body, body)

    def test_gui_rejects_a_placeholder_before_adding_operator_attribution(self):
        payload = {'leaf_id': 'review-body', 'agent': 'workbuddy',
                   'operator_confirmed': True, 'summary': 'PLACEHOLDER'}
        with self.assertRaisesRegex(ValueError, 'PLACEHOLDER'):
            self.bridge._submit_leaf_result(payload)
        self.assertFalse(self.bus.find_results(self.task.id))
        payload['summary'] = f'操作人核对，已替换草稿文字「{SUMMARY_PLACEHOLDER}」。'
        self.bridge._submit_leaf_result(payload)
        self.assertIn(payload['summary'], self.bus.find_results(self.task.id)[0].body)
