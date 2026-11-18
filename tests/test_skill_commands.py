"""Source: Codex, 2026-10-05. Real read-only skills CLI integration."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agentbus.cli import main
from agentbus.config import load


class SkillCommandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        load(self.root, create=True)
        self.source = self.root / 'skills' / '示例'
        self.source.mkdir(parents=True)
        (self.source / 'SKILL.md').write_bytes(b'---\nname: demo\n---\n# Demo\r\n')
        (self.source / 'scripts').mkdir()
        (self.source / 'scripts' / 'a.py').write_bytes(b'print(1)\r\n')
        (self.source / 'assets').mkdir()
        (self.source / 'assets' / 'pixel.bin').write_bytes(b'\xff\x00\x80')

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(['skills', *args, '--workspace', str(self.root)])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_inspect_reads_all_resources_and_changes_digest_when_script_changes(self):
        before = self.snapshot()
        code, stdout, stderr = self.invoke('inspect', '--path', 'skills/示例')
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual({x['path'] for x in report['files']},
                         {'SKILL.md', 'scripts/a.py', 'assets/pixel.bin'})
        self.assertEqual(self.snapshot(), before)
        (self.source / 'scripts' / 'a.py').write_bytes(b'print(2)\r\n')
        code, stdout, stderr = self.invoke('inspect', '--path', 'skills/示例')
        self.assertEqual(code, 0, stderr)
        self.assertNotEqual(report['content_digest'], json.loads(stdout)['content_digest'])

    def test_preview_missing_target_never_creates_target_or_evidence(self):
        before = self.snapshot()
        code, stdout, stderr = self.invoke('preview', '--path', 'skills/示例',
            '--target-path', 'host-skills/demo', '--host', 'custom_agent',
            '--scope', 'project', '--route', 'desktop')
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report['action'], 'create')
        self.assertIs(report['write_permitted'], False)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / 'host-skills').exists())
        self.assertIn('未验证', report['presentation']['invocation_label'])

    def test_existing_target_without_owner_remains_conflict_even_when_equal(self):
        code, stdout, stderr = self.invoke('preview', '--path', 'skills/示例',
            '--target-path', 'skills/示例', '--host', 'codex',
            '--scope', 'global', '--route', 'desktop')
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report['action'], 'conflict')
        self.assertIn('OWNER_UNKNOWN', report['reason_codes'])
        self.assertIn('保留', report['presentation']['summary'])
        self.assertIn('未验证', report['presentation']['execution_version_label'])

    def test_manifest_entries_are_three_fields_with_raw_bytes(self):
        code, stdout, stderr = self.invoke('inspect', '--path', 'skills/示例')
        self.assertEqual(code, 0, stderr)
        entry = next(x for x in json.loads(stdout)['files'] if x['path'] == 'scripts/a.py')
        self.assertEqual(set(entry), {'path', 'bytes', 'sha256'})
        self.assertEqual(entry['bytes'], len(b'print(1)\r\n'))
        self.assertEqual(entry['sha256'], hashlib.sha256(b'print(1)\r\n').hexdigest())

    def test_record_with_unknown_fields_is_not_silently_trusted(self):
        record = self.root / 'record.json'
        record.write_text('{"owner": "agentbus", "evil": "authorize"}', encoding='utf-8')
        before = self.snapshot()
        code, stdout, stderr = self.invoke('preview', '--path', 'skills/示例',
            '--target-path', 'skills/示例', '--host', 'codex',
            '--scope', 'project', '--route', 'desktop', '--record-file', 'record.json')
        self.assertEqual(self.snapshot(), before)
        if code == 0:
            self.assertEqual(json.loads(stdout)['action'], 'conflict')
            self.assertIs(json.loads(stdout)['write_permitted'], False)
        else:
            self.assertEqual(code, 4, stderr)

    def test_target_escape_is_rejected_without_reading_or_writing(self):
        before = self.snapshot()
        code, stdout, stderr = self.invoke('preview', '--path', 'skills/示例',
            '--target-path', '../outside', '--host', 'codex',
            '--scope', 'project', '--route', 'desktop')
        self.assertEqual(code, 4, stderr)
        self.assertIn('UNSAFE_PATH', stderr)
        self.assertEqual(self.snapshot(), before)

    def test_private_source_is_rejected_before_content_access(self):
        code, stdout, stderr = self.invoke('inspect', '--path', '.workbuddy/skills/demo')
        self.assertEqual(code, 4, stderr)
        self.assertIn('UNSAFE_PATH', stderr)

    def test_missing_entry_reports_nonzero(self):
        (self.source / 'SKILL.md').unlink()
        code, stdout, stderr = self.invoke('inspect', '--path', 'skills/示例')
        self.assertEqual(code, 4, stderr)
        self.assertTrue(stderr)

    def test_invalid_record_json_reports_nonzero_and_no_mutation(self):
        (self.root / 'record.json').write_bytes(b'{broken')
        before = self.snapshot()
        code, stdout, stderr = self.invoke('preview', '--path', 'skills/示例',
            '--target-path', 'skills/示例', '--host', 'codex',
            '--scope', 'project', '--route', 'desktop', '--record-file', 'record.json')
        self.assertEqual(code, 4, stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
