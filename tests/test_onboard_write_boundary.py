"""Lexical workspace and write ancestors are checked before resolve."""
from pathlib import Path
from agentbus.onboarding_pack import auto_onboard_agent
from tests.onboard_test_utils import OnboardFixture

class TestOnboardWriteBoundary(OnboardFixture):
    def test_prevent_workspace_escape_on_write(self):
        outside = Path(self.tmp.name) / 'outside'
        self.link(self.root / '.agentbus/agents/new-peer', outside)
        before = (self.root / '.agentbus/config.json').read_bytes()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(before, (self.root / '.agentbus/config.json').read_bytes())
        self.assertEqual([], list(outside.iterdir()))

    def test_workspace_junction_is_rejected_before_resolve(self):
        alias = Path(self.tmp.name) / 'workspace-alias'
        self.link(alias, self.root)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(alias, 'new-peer')
        self.assertEqual(before, self.snapshot())

    def test_skill_parent_junction_is_refused_before_any_config_change(self):
        outside = Path(self.tmp.name) / 'outside-skills'
        self.link(self.root / '.agentbus/workflows/new-peer', outside)
        before = (self.root / '.agentbus/config.json').read_bytes()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'new-peer')
        self.assertEqual(before, (self.root / '.agentbus/config.json').read_bytes())
        self.assertEqual([], list(outside.iterdir()))
