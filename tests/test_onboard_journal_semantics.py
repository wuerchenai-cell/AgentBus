"""2026-10-14 review: recovery journals cannot reverse arbitrary public settings."""
import base64
import copy
import hashlib
import json
import subprocess
import sys
from agentbus.onboarding_pack import auto_onboard_agent
from tests.onboard_test_utils import OnboardFixture


class OnboardJournalSemanticsTests(OnboardFixture):
    def journal(self, changes, member='new-peer'):
        record = {'schema_version': 1, 'member_id': member, 'created_directories': [],
            'changes': {name: {'before': base64.b64encode(before).decode('ascii') if before is not None else None,
                              'after': base64.b64encode(after).decode('ascii')} for name, (before, after) in changes.items()}}
        (self.root / '.agentbus/onboarding-transaction.json').write_text(json.dumps(record), encoding='utf-8')

    def test_forged_config_journal_cannot_enable_old_disabled_member(self):
        path = self.root / '.agentbus/config.json'
        after = path.read_bytes()
        before = json.loads(after)
        before['workers']['workbuddy'].update(enabled=True, executor_enabled=True)
        self.journal({'.agentbus/config.json': (json.dumps(before).encode(), after)})
        original = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(original, self.snapshot())

    def test_forged_existing_skill_content_is_not_a_recovery_write(self):
        self.add_peer()
        rel = '.agentbus/workflows/codex/SKILL.md'
        current = (self.root / rel).read_bytes()
        self.journal({rel: (b'malicious prior content', current)}, member='existing-peer')
        original = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(original, self.snapshot())

    def test_forged_entry_content_is_not_a_recovery_write(self):
        self.add_peer()
        current = (self.root / 'AGENTBUS.md').read_bytes()
        self.journal({'AGENTBUS.md': (b'forged prior entry', current)}, member='existing-peer')
        original = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(original, self.snapshot())

    def test_forged_workflow_cannot_replace_registered_launcher(self):
        self.add_peer()
        current = (self.root / '.agentbus/workflow.json').read_bytes()
        forged = json.loads(current)
        forged['launcher']['entrypoint'] = str(self.root / 'untrusted-entry.py')
        self.journal({'.agentbus/workflow.json': (json.dumps(forged).encode(), current)}, member='existing-peer')
        original = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(original, self.snapshot())

    def test_initial_workflow_recovery_for_existing_member_has_no_config_mutation(self):
        self.add_peer()
        path = self.root / '.agentbus/workflow.json'
        workflow = json.loads(path.read_text(encoding='utf-8'))
        for relative in workflow['files']:
            (self.root / relative).unlink()
        path.unlink()
        before_config = (self.root / '.agentbus/config.json').read_bytes()
        code = '''import os, sys
from pathlib import Path
from agentbus.onboarding_pack import auto_onboard_agent
root = Path(sys.argv[1])
original = os.replace
def interrupted(source, target):
    original(source, target)
    if Path(target) == root / "AGENTBUS.md":
        os._exit(71)
os.replace = interrupted
auto_onboard_agent(root, "existing-peer")
'''
        child = subprocess.run([sys.executable, '-c', code, str(self.root)], capture_output=True, timeout=20)
        self.assertEqual(71, child.returncode, child.stderr.decode('utf-8', errors='replace'))
        for lease in (self.root / '.agentbus/leases').glob('*.json'):
            record = json.loads(lease.read_text(encoding='utf-8'))
            if record.get('owner') == 'agentbus-onboarding':
                record['acquired_at'] = 0
                lease.write_text(json.dumps(record), encoding='utf-8')
        result = auto_onboard_agent(self.root, 'existing-peer')
        self.assertTrue(result['success'])
        self.assertEqual(before_config, (self.root / '.agentbus/config.json').read_bytes())
        self.assertFalse((self.root / '.agentbus/onboarding-transaction.json').exists())
