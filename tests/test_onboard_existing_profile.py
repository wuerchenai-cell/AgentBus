"""Existing profiles never gain permissions or get replaced by onboarding."""
from agentbus.config import load
from agentbus.onboarding_pack import auto_onboard_agent
from agentbus.onboarding_pack import generate_onboarding_pack
from agentbus.project_workflow import workflow_status
from tests.onboard_test_utils import OnboardFixture

class TestOnboardExistingProfile(OnboardFixture):
    def test_new_file_member_has_the_published_write_capability(self):
        auto_onboard_agent(self.root, 'new-peer')
        pack = generate_onboarding_pack(self.root, 'new-peer')
        self.assertTrue(pack['writable'])
        self.assertEqual([pack['output_path']], pack['sample_task']['expected_artifacts'])

    def test_preserve_existing_profile_canonical_path(self):
        original = self.add_peer()
        result = auto_onboard_agent(self.root, 'existing-peer')
        self.assertTrue(result['success'])
        self.assertEqual(original, load(self.root).data['workers']['existing-peer'])
        self.assertEqual('Existing Peer', result['display_name'])
        self.assertEqual('.public/skills/existing-peer/SKILL.md', result['skill_path'])
        self.assertFalse((self.root / '.agentbus/workflows/existing-peer/SKILL.md').exists())
        self.assertTrue(workflow_status(load(self.root))['ready'])

    def test_disabled_profile_refused_without_enabling_or_writes(self):
        self.add_peer(enabled=False, executor_enabled=False)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'existing-peer')
        self.assertEqual(before, self.snapshot())

    def test_conflicting_display_or_preset_does_not_claim_success(self):
        self.add_peer(preset='generic')
        before = self.snapshot()
        for args in ({'display_name': 'Changed Name'}, {'preset': 'cursor'}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    auto_onboard_agent(self.root, 'existing-peer', **args)
        self.assertEqual(before, self.snapshot())
