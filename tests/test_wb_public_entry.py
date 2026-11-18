"""WorkBuddy modules exercised through actual public CLI, no model calls."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentbus.cli import main


class WorkBuddyPublicEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        code, _ = self.call('init')
        self.assertEqual(0, code)
        self.source = self.root / 'sources' / 'example'
        self.source.mkdir(parents=True)
        (self.source / 'SKILL.md').write_text(
            '---\nname: example\ndescription: Example project skill\n---\nRead project files.\n',
            encoding='utf-8')

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([*args, '--workspace', str(self.root)])
        return code, out.getvalue() + err.getvalue()

    def test_targets_support_generic_without_native_claim(self):
        code, output = self.call('skills', 'targets')
        self.assertEqual(0, code)
        value = json.loads(output)
        self.assertIn('codex', value['hosts'])
        self.assertFalse(value['native_discovery_claimed'])

    def test_sync_default_is_preview(self):
        code, output = self.call('skills', 'sync', '--path', 'sources/example', '--host', 'codex')
        self.assertEqual(0, code, output)
        value = json.loads(output)
        self.assertFalse(value['write_permitted'])
        self.assertFalse((self.root / '.codex/skills/example/SKILL.md').exists())

    def test_sync_and_rollback_transaction(self):
        code, output = self.call('skills', 'sync', '--path', 'sources/example', '--host', 'codex', '--confirm')
        self.assertEqual(0, code, output)
        value = json.loads(output)
        self.assertTrue(value['deployed'])
        target = Path(value['target_dir']) / 'SKILL.md'
        self.assertEqual((self.source / 'SKILL.md').read_bytes(), target.read_bytes())
        code, output = self.call('skills', 'rollback', '--path', 'sources/example', '--host', 'codex', '--confirm')
        self.assertEqual(0, code, output)
        self.assertTrue(json.loads(output)['rolled_back'])
        self.assertFalse(target.exists())

    def test_global_sync_is_refused(self):
        code, _ = self.call('skills', 'sync', '--path', 'sources/example', '--host', 'codex', '--scope', 'global', '--confirm')
        self.assertEqual(4, code)

    def test_private_source_is_refused(self):
        code, _ = self.call('skills', 'sync', '--path', '.workbuddy/secret', '--host', 'codex')
        self.assertEqual(4, code)

    def test_assess_is_readonly_and_cannot_dispatch(self):
        (self.root / 'snapshot.json').write_text(json.dumps({'taskbook': {'work_items': [], 'acceptance': []}, 'changes': {}}), encoding='utf-8')
        code, output = self.call('plan', 'assess', '--file', 'snapshot.json', '--reason', 'Improve the task tree', '--caller', 'codex')
        self.assertEqual(0, code, output)
        value = json.loads(output)
        self.assertFalse(value['may_dispatch'])
        self.assertFalse(value['dispatch_performed'])
        self.assertTrue(value['read_only'])

    def test_convergence_missing_evidence_is_not_completed(self):
        snapshot = {'taskbook': {'work_items': [{'id': 'w1', 'title': 'Example', 'status': 'completed',
                    'owner': 'codex', 'artifacts': ['output.txt'], 'acceptance': ['a1']}],
                    'acceptance': [{'id': 'a1'}]}, 'changes': {}}
        (self.root / 'snapshot.json').write_text(json.dumps(snapshot), encoding='utf-8')
        code, output = self.call('plan', 'convergence', '--file', 'snapshot.json')
        self.assertEqual(0, code, output)
        value = json.loads(output)
        self.assertFalse(value['ok'])
        self.assertIn('UNVERIFIED_COMPLETION', [row['code'] for row in value['violations']])

    def test_convergence_does_not_read_current_plan_implicitly(self):
        code, _ = self.call('plan', 'convergence')
        self.assertEqual(4, code)

    def test_malformed_snapshot_cannot_report_empty_success(self):
        (self.root / 'snapshot.json').write_text('{"work_items":[],"changes":[]}', encoding='utf-8')
        code, _ = self.call('plan', 'convergence', '--file', 'snapshot.json')
        self.assertEqual(4, code)

    def deployment(self):
        code, output = self.call('skills', 'sync', '--path', 'sources/example', '--host', 'codex', '--confirm')
        self.assertEqual(0, code, output)
        target = Path(json.loads(output)['target_dir'])
        record_path = next((self.root / '.agentbus/skill-center/records').glob('*.json'))
        return target, record_path, json.loads(record_path.read_text(encoding='utf-8'))

    def test_rollback_refuses_external_backup_before_read(self):
        target, path, record = self.deployment()
        original = (target / 'SKILL.md').read_bytes()
        record['backup_dir'] = str(self.root.parent / 'external-backup')
        path.write_text(json.dumps(record), encoding='utf-8')
        code, _ = self.call('skills', 'rollback', '--path', 'sources/example', '--host', 'codex', '--confirm')
        self.assertEqual(4, code)
        self.assertEqual(original, (target / 'SKILL.md').read_bytes())

    def test_rollback_refuses_injected_managed_file_without_digest(self):
        target, path, record = self.deployment()
        user_file = target / 'user-notes.md'
        user_file.write_bytes(b'USER FILE')
        record['managed_files'].append('user-notes.md')
        path.write_text(json.dumps(record), encoding='utf-8')
        code, _ = self.call('skills', 'rollback', '--path', 'sources/example', '--host', 'codex', '--confirm')
        self.assertEqual(4, code)
        self.assertEqual(b'USER FILE', user_file.read_bytes())

    def test_record_commit_failure_rolls_back_target_bytes(self):
        from agentbus import skill_install
        original_write = skill_install._atomic_write
        def fail_record(path, data):
            if Path(path).parent.name == 'records':
                raise OSError('injected record commit failure')
            return original_write(path, data)
        with patch.object(skill_install, '_atomic_write', side_effect=fail_record):
            code, _ = self.call('skills', 'sync', '--path', 'sources/example', '--host', 'codex', '--confirm')
        self.assertEqual(4, code)
        self.assertFalse((self.root / '.codex/skills/example/SKILL.md').exists())

    def test_user_modified_rollback_retains_recovery_record(self):
        target, record_path, _ = self.deployment()
        (target / 'SKILL.md').write_bytes(b'USER MODIFIED')
        code, output = self.call('skills', 'rollback', '--path', 'sources/example', '--host', 'codex', '--confirm')
        self.assertEqual(0, code, output)
        self.assertFalse(json.loads(output)['complete'])
        self.assertTrue(record_path.exists())
        self.assertEqual(b'USER MODIFIED', (target / 'SKILL.md').read_bytes())


if __name__ == '__main__':
    unittest.main()
