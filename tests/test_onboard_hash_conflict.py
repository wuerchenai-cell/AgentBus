"""Onboarding never silently re-signs modified owned workflows."""
from agentbus.onboarding_pack import auto_onboard_agent
from agentbus.lease import LeaseManager
from agentbus.config import load
from unittest.mock import patch
from tests.onboard_test_utils import OnboardFixture

class TestOnboardHashConflict(OnboardFixture):
    def test_other_registered_asset_change_during_lease_refuses_new_member(self):
        skill = self.root / '.agentbus/workflows/codex/SKILL.md'
        original = LeaseManager.acquire
        changed = False
        def concurrent(manager, relative, owner, *args, **kwargs):
            nonlocal changed
            if relative == '.agentbus/config.json' and owner == 'agentbus-onboarding' and not changed:
                changed = True
                skill.write_bytes(skill.read_bytes() + b'\nConcurrent legitimate edit\n')
            return original(manager, relative, owner, *args, **kwargs)
        with patch.object(LeaseManager, 'acquire', concurrent):
            with self.assertRaises(ValueError):
                auto_onboard_agent(self.root, 'new-peer')
        self.assertNotIn('new-peer', load(self.root).data['workers'])
        self.assertTrue(skill.read_bytes().endswith(b'Concurrent legitimate edit\n'))
        self.assertFalse((self.root / '.agentbus/agents/new-peer').exists())

    def test_preserve_modified_skill_conflict_hash(self):
        self.add_peer()
        skill = self.root / '.public/skills/existing-peer/SKILL.md'
        skill.write_text('manually changed skill\n', encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'existing-peer')
        self.assertEqual(before, self.snapshot())

    def test_other_registered_skill_conflict_blocks_new_member_atomically(self):
        skill = self.root / '.agentbus/workflows/codex/SKILL.md'
        skill.write_text('manual change', encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(before, self.snapshot())

    def test_unregistered_existing_skill_is_not_silently_adopted(self):
        skill = self.root / '.agentbus/workflows/new-peer/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('unowned manual skill', encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(before, self.snapshot())
