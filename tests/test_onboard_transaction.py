"""Onboarding commits its exact file/mailbox set or rolls back."""
import os
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from agentbus.config import load
from agentbus.onboarding_pack import auto_onboard_agent
from agentbus.project_workflow import workflow_status
from agentbus.lease import LeaseManager
from tests.onboard_test_utils import OnboardFixture

class TestOnboardTransaction(OnboardFixture):
    def test_corrupt_config_is_not_auto_recovered_outside_the_transaction(self):
        path = self.root / '.agentbus/config.json'
        path.with_name('config.json.bak').write_bytes(path.read_bytes())
        path.write_text('{corrupt', encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(before, self.snapshot())

    def unprepare(self):
        path = self.root / '.agentbus/workflow.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        for relative in record['files']:
            (self.root / relative).unlink()
        path.unlink()

    def test_first_workflow_and_member_are_one_transaction(self):
        self.unprepare()
        before = self.snapshot()
        original = os.replace
        def fail_manifest(source, target):
            if Path(target) == self.root / '.agentbus/workflow.json':
                raise OSError('initial workflow publish failure')
            return original(source, target)
        with patch('os.replace', side_effect=fail_manifest):
            with self.assertRaises(OSError):
                auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(before, self.snapshot())
        result = auto_onboard_agent(self.root, 'new-peer')
        self.assertTrue(result['success'])
        self.assertTrue(workflow_status(load(self.root))['ready'])

    def test_active_configuration_lease_refuses_without_writes(self):
        manager = LeaseManager(self.root / '.agentbus')
        lease = manager.acquire('.agentbus/config.json', 'other-member', ttl_s=120)
        self.assertIsNotNone(lease)
        before = self.snapshot()
        try:
            with self.assertRaises(ValueError):
                auto_onboard_agent(self.root, 'new-peer')
        finally:
            manager.release(lease)
        self.assertEqual(before, self.snapshot())
    def test_atomic_onboard_transaction(self):
        before = self.snapshot()
        original = os.replace
        def fail_workflow(source, target):
            if Path(target) == self.root / '.agentbus/workflow.json':
                raise OSError('injected workflow commit failure')
            return original(source, target)
        with patch('os.replace', side_effect=fail_workflow):
            with self.assertRaises(OSError):
                auto_onboard_agent(self.root, 'new-peer', display_name='New Peer')
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / '.agentbus/agents/new-peer').exists())
        self.assertFalse((self.root / '.agentbus/workflows/new-peer').exists())

    def test_repeat_onboard_is_byte_stable_and_workflow_ready(self):
        first = auto_onboard_agent(self.root, 'new-peer', display_name='New Peer', preset='cursor')
        before = self.snapshot()
        second = auto_onboard_agent(self.root, 'new-peer', display_name='New Peer', preset='cursor')
        self.assertTrue(first['success'])
        self.assertTrue(second['success'])
        self.assertEqual(before, self.snapshot())
        self.assertTrue(workflow_status(load(self.root))['ready'])
        for folder in ('ready', 'processing', 'done', 'blocked'):
            self.assertTrue((self.root / '.agentbus/agents/new-peer' / folder).is_dir())

    def test_late_prompt_failure_leaves_no_partial_member(self):
        before = self.snapshot()
        with patch('agentbus.onboarding_pack.build_one_click_join_prompt', side_effect=ValueError('invalid launcher')):
            with self.assertRaises(ValueError):
                auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / '.agentbus/agents/new-peer').exists())

    def test_config_change_while_acquiring_lease_is_not_overwritten(self):
        original = LeaseManager.acquire
        changed = False
        def concurrent(manager, relative, owner, *args, **kwargs):
            nonlocal changed
            if relative == '.agentbus/config.json' and owner == 'agentbus-onboarding' and not changed:
                changed = True
                path = self.root / '.agentbus/config.json'
                data = json.loads(path.read_text(encoding='utf-8'))
                data['concurrent_marker'] = 'must survive'
                path.write_text(json.dumps(data), encoding='utf-8')
            return original(manager, relative, owner, *args, **kwargs)
        with patch.object(LeaseManager, 'acquire', concurrent):
            with self.assertRaises(ValueError):
                auto_onboard_agent(self.root, 'new-peer')
        data = json.loads((self.root / '.agentbus/config.json').read_text(encoding='utf-8'))
        self.assertEqual('must survive', data['concurrent_marker'])
        self.assertNotIn('new-peer', data['workers'])

    def test_failure_after_atomic_replace_rolls_back_all_original_bytes(self):
        before = self.snapshot()
        original = os.replace
        once = False
        def after_replace(source, target):
            nonlocal once
            original(source, target)
            if Path(target) == self.root / '.agentbus/config.json' and not once:
                once = True
                raise OSError('injected after config publish')
        with patch('os.replace', side_effect=after_replace):
            with self.assertRaises(OSError):
                auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / '.agentbus/agents/new-peer').exists())

    def test_hard_exit_mid_commit_recovers_on_next_onboard(self):
        code = '''import os, sys
from pathlib import Path
from agentbus.onboarding_pack import auto_onboard_agent
root = Path(sys.argv[1])
original = os.replace
def interrupted(source, target):
    original(source, target)
    if Path(target) == root / ".agentbus/config.json":
        os._exit(71)
os.replace = interrupted
auto_onboard_agent(root, "new-peer")
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.root)], capture_output=True, timeout=20)
        self.assertEqual(71, result.returncode, result.stderr.decode('utf-8', errors='replace'))
        # The dead process's leases are not automatically proof of inactivity; expire only this fixture's records.
        for path in (self.root / '.agentbus/leases').glob('*.json'):
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('owner') == 'agentbus-onboarding':
                data['acquired_at'] = 0
                path.write_text(json.dumps(data), encoding='utf-8')
        result = auto_onboard_agent(self.root, 'new-peer')
        self.assertTrue(result['success'])
        self.assertTrue(workflow_status(load(self.root))['ready'])
        self.assertFalse((self.root / '.agentbus/onboarding-transaction.json').exists())
