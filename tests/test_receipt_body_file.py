"""Source: Codex Task 5, 2026-10-05. Isolated CLI body-file regression.

The tests exercise real mailbox writes and terminal states, never models.
They catch lost whitespace, partial pages, version mixing, and fake receipts.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.cli import main
from agentbus.config import load
from agentbus.workspace_files import WorkspaceFiles, MAX_TEXT_BYTES

TEXT = '  弯引号：“确认成功”；ASCII: "quoted words"; slash: C:\\folder with spaces\\file.txt\r\n  行二：反斜线\\\\，空格尾部  \r\n最后一行  '


class ReceiptBodyFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agentbus receipt body ')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.cfg = load(self.workspace, create=True)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()

    def invoke(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                code = main(list(arguments))
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue()

    def task(self):
        task = self.bus.make('codex', 'workbuddy', 'task', body='isolated receipt fixture', result_to='codex')
        self.bus.post(task)
        claimed = self.bus.claim('workbuddy', self.bus.dirs('workbuddy')['ready'] / (task.id + '.json'))
        self.bus.heartbeat('workbuddy', claimed, captured_ticket(claimed), note='test receipt worker started')
        return claimed

    def put(self, raw, relative='reports/回执 with spaces.md'):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return relative, hashlib.sha256(raw).hexdigest()

    def answer(self, task, *options):
        return self.invoke('answer', '--workspace', str(self.workspace), '--agent', 'workbuddy',
                           '--id', task.id, '--claim-epoch', str(task.claim_epoch),
                           '--claim-id', task.claim_id, *options)

    def post(self, *options, reply_to=None):
        args = ['post', '--workspace', str(self.workspace), '--from', 'workbuddy' if reply_to else 'codex',
                '--to', 'codex' if reply_to else 'workbuddy', '--title', 'body fixture',
                '--type', 'result' if reply_to else 'task']
        args += ['--reply-to', reply_to] if reply_to else ['--result-to', 'codex']
        return self.invoke(*args, *options)

    def receipt(self, task):
        values = [self.bus.read(path) for path in self.bus.pending('codex')]
        return next(value for value in values if value.reply_to == task.id)

    def unchanged_answer_failure(self, task, *options):
        processing = self.bus.dirs('workbuddy')['processing'] / (task.id + '.json')
        original = processing.read_bytes()
        event_path = self.bus.root / 'events.jsonl'
        events = event_path.read_bytes() if event_path.exists() else None
        code, output = self.answer(task, *options)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(processing.read_bytes(), original)
        self.assertEqual(self.bus.pending('codex'), [])
        self.assertEqual(list(self.bus.dirs('workbuddy')['done'].glob('*.json')), [])
        self.assertEqual(list(self.bus.dirs('workbuddy')['blocked'].glob('*.json')), [])
        self.assertEqual(event_path.read_bytes() if event_path.exists() else None, events)
        return code, output

    def test_answer_body_file_preserves_quotes_spaces_backslashes_crlf(self):
        task = self.task()
        raw = TEXT.encode('utf8')
        relative, digest = self.put(raw)
        code, output = self.answer(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.receipt(task).body.encode('utf8'), raw)
        terminal = self.bus.read(self.bus.dirs('workbuddy')['done'] / (task.id + '.json'))
        self.assertEqual((terminal.status, terminal.result.encode('utf8')), ('done', raw))
        self.assertEqual((self.workspace / relative).read_bytes(), raw)

    def test_post_body_file_preserves_raw_task_content(self):
        raw = TEXT.encode('utf8')
        relative, digest = self.put(raw)
        code, output = self.post('--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 0, output)
        message = self.bus.read(self.bus.pending('workbuddy')[0])
        self.assertEqual(message.body.encode('utf8'), raw)
        self.assertEqual(message.result_to, 'codex')

    def test_post_result_body_file_keeps_task_reference_and_content(self):
        task = self.task()
        raw = TEXT.encode('utf8')
        relative, digest = self.put(raw)
        code, output = self.post('--body-file', relative, '--expected-body-sha256', digest,
                                 '--claim-epoch', str(task.claim_epoch), '--claim-id', task.claim_id,
                                 reply_to=task.id)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.receipt(task).body.encode('utf8'), raw)
        self.assertNotEqual(self.receipt(task).id, task.id)

    def test_answer_collects_every_page_without_truncating_or_normalizing(self):
        task = self.task()
        text = ''.join(f'  第{number}行：“原文” "ASCII" \\ tail  \r\n' for number in range(750)) + ' last  '
        raw = text.encode('utf8')
        relative, digest = self.put(raw)
        code, output = self.answer(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.receipt(task).body.encode('utf8'), raw)

    def test_raw_hash_includes_bom_while_text_uses_existing_bom_decode_contract(self):
        task = self.task()
        raw = b'\xef\xbb\xbf' + TEXT.encode('utf8')
        relative, digest = self.put(raw)
        code, output = self.answer(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.receipt(task).body, TEXT)
        self.assertEqual((self.workspace / relative).read_bytes(), raw)

    def test_missing_body_file_is_not_archived_or_replaced(self):
        task = self.task()
        code, output = self.unchanged_answer_failure(task, '--body-file', 'missing.md', '--expected-body-sha256', '0' * 64)
        self.assertEqual(code, 4, output)
        self.assertIn('NOT_FOUND', output)

    def test_wrong_raw_hash_is_rejected_before_any_receipt_state_change(self):
        task = self.task()
        relative, _ = self.put(TEXT.encode('utf8'))
        code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', '0' * 64)
        self.assertEqual(code, 4, output)
        self.assertIn('CONFLICT', output)

    def test_bom_stripped_hash_does_not_match_raw_file_hash(self):
        task = self.task()
        relative, _ = self.put(b'\xef\xbb\xbf' + TEXT.encode('utf8'))
        digest = hashlib.sha256(TEXT.encode('utf8')).hexdigest()
        code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 4, output)

    def test_file_input_requires_expected_raw_hash(self):
        task = self.task()
        relative, _ = self.put(TEXT.encode('utf8'))
        code, output = self.unchanged_answer_failure(task, '--body-file', relative)
        self.assertEqual(code, 4, output)

    def test_expected_raw_hash_requires_file_input(self):
        task = self.task()
        code, output = self.unchanged_answer_failure(task, '--body', TEXT, '--expected-body-sha256', '0' * 64)
        self.assertEqual(code, 4, output)

    def test_invalid_hash_is_rejected_before_read_or_archive(self):
        task = self.task()
        relative, _ = self.put(TEXT.encode('utf8'))
        for digest in ('not-a-hash', 'A' * 64, ''):
            with self.subTest(digest=digest):
                code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', digest)
                self.assertEqual(code, 4, output)

    def test_invalid_utf8_rejects_receipt_without_archiving(self):
        task = self.task()
        relative, digest = self.put(b'not utf8: \xff')
        code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 4, output)
        self.assertIn('INVALID_UTF8', output)

    def test_unsafe_relative_private_and_absolute_paths_are_rejected(self):
        task = self.task()
        for relative in ('../outside.md', '.agentbus/config.json', '.workbuddy/private.md', str(self.workspace / 'absolute.md')):
            with self.subTest(relative=relative):
                code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', '0' * 64)
                self.assertEqual(code, 4, output)
                self.assertIn('UNSAFE_PATH', output)

    def test_directory_is_not_a_body_file(self):
        task = self.task()
        (self.workspace / 'directory').mkdir()
        code, output = self.unchanged_answer_failure(task, '--body-file', 'directory', '--expected-body-sha256', '0' * 64)
        self.assertEqual(code, 4, output)
        self.assertIn('NOT_FILE', output)

    def test_file_exceeding_existing_eight_mib_limit_is_rejected(self):
        task = self.task()
        relative, digest = self.put(b'x' * (MAX_TEXT_BYTES + 1))
        code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 4, output)
        self.assertIn('TOO_LARGE', output)

    def test_existing_single_line_page_limit_still_applies(self):
        task = self.task()
        relative, digest = self.put(b'x' * 70000)
        code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 4, output)
        self.assertIn('PAGE_TOO_LARGE', output)

    def test_changed_file_between_pages_cannot_mix_two_versions(self):
        task = self.task()
        raw = ('line\r\n' * 250).encode('utf8')
        relative, digest = self.put(raw)
        original_read = WorkspaceFiles.read_text
        count = 0

        def changed_after_first_page(instance, *args, **kwargs):
            nonlocal count
            page = original_read(instance, *args, **kwargs)
            count += 1
            if count == 1:
                (self.workspace / relative).write_bytes(b'changed\r\n' * 250)
            return page

        with patch.object(WorkspaceFiles, 'read_text', changed_after_first_page):
            code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 4, output)
        self.assertIn('CONFLICT', output)

    def test_more_than_128_pages_rejects_without_submitting_a_partial_body(self):
        task = self.task()
        relative, digest = self.put(b'\n' * 25800)
        code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 4, output)
        self.assertIn('128 pages', output)

    def test_exact_placeholder_file_is_rejected_before_archive(self):
        task = self.task()
        for raw in (b'PLACEHOLDER', b'  PLACEHOLDER\r\n'):
            with self.subTest(raw=raw):
                relative, digest = self.put(raw)
                code, output = self.unchanged_answer_failure(task, '--body-file', relative, '--expected-body-sha256', digest)
                self.assertEqual(code, 4, output)

    def test_exact_placeholder_inline_answer_is_rejected(self):
        task = self.task()
        code, output = self.unchanged_answer_failure(task, '--body', 'PLACEHOLDER')
        self.assertEqual(code, 4, output)

    def test_post_exact_placeholder_does_not_publish_a_message(self):
        code, output = self.post('--intent', 'PLACEHOLDER')
        self.assertNotEqual(code, 0, output)
        self.assertEqual(self.bus.pending('workbuddy'), [])

    def test_normal_report_mentioning_placeholder_is_not_rejected(self):
        task = self.task()
        text = '报告指出：不要用 PLACEHOLDER 代替真实正文。'
        relative, digest = self.put(text.encode('utf8'))
        code, output = self.answer(task, '--body-file', relative, '--expected-body-sha256', digest)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.receipt(task).body, text)

    def test_inline_and_body_file_are_mutually_exclusive_for_answer(self):
        task = self.task()
        relative, digest = self.put(TEXT.encode('utf8'))
        self.unchanged_answer_failure(task, '--body', 'different', '--body-file', relative, '--expected-body-sha256', digest)

    def test_inline_and_body_file_are_mutually_exclusive_for_post(self):
        relative, digest = self.put(TEXT.encode('utf8'))
        code, output = self.post('--intent', 'different', '--body-file', relative, '--expected-body-sha256', digest)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(self.bus.pending('workbuddy'), [])

    def test_bad_result_file_keeps_original_task_and_posts_nothing(self):
        task = self.task()
        processing = self.bus.dirs('workbuddy')['processing'] / (task.id + '.json')
        original = processing.read_bytes()
        code, output = self.post('--body-file', 'absent.md', '--expected-body-sha256', '0' * 64, reply_to=task.id)
        self.assertEqual(code, 4, output)
        self.assertEqual(self.bus.pending('codex'), [])
        self.assertEqual(processing.read_bytes(), original)

    def test_legacy_inline_answer_remains_byte_preserving(self):
        task = self.task()
        code, output = self.answer(task, '--body', TEXT)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.receipt(task).body, TEXT)

    @unittest.skipUnless(os.name == 'nt', 'requires Windows PowerShell 5.1')
    def test_powershell51_body_file_path_avoids_native_body_quote_splitting(self):
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        if not powershell.is_file():
            self.skipTest('Windows PowerShell 5.1 is not installed')
        task = self.task()
        raw = TEXT.encode('utf8')
        relative, digest = self.put(raw)
        script = '''
$ErrorActionPreference = 'Continue'
& $env:AGENTBUS_RECEIPT_PYTHON -B -X utf8 $env:AGENTBUS_RECEIPT_ENTRY answer --workspace $env:AGENTBUS_RECEIPT_WORKSPACE --agent workbuddy --id $env:AGENTBUS_RECEIPT_TASK --claim-epoch $env:AGENTBUS_RECEIPT_EPOCH --claim-id $env:AGENTBUS_RECEIPT_CLAIM --body-file $env:AGENTBUS_RECEIPT_FILE --expected-body-sha256 $env:AGENTBUS_RECEIPT_HASH 2>&1 | Out-String | Write-Output
exit $LASTEXITCODE
'''
        environment = dict(os.environ)
        environment.update(AGENTBUS_RECEIPT_PYTHON=sys.executable, AGENTBUS_RECEIPT_ENTRY=str(ROOT / 'entry.py'),
                           AGENTBUS_RECEIPT_WORKSPACE=str(self.workspace), AGENTBUS_RECEIPT_TASK=task.id,
                           AGENTBUS_RECEIPT_EPOCH=str(task.claim_epoch), AGENTBUS_RECEIPT_CLAIM=task.claim_id,
                           AGENTBUS_RECEIPT_FILE=relative, AGENTBUS_RECEIPT_HASH=digest)
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        outcome = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                 env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
        self.assertEqual(self.receipt(task).body.encode('utf8'), raw)
        self.assertEqual((self.workspace / relative).read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
