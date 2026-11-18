"""JR-20261108-U01: desktop onboarding must work under the shipped CSP."""
from pathlib import Path
import unittest


ASSETS = Path(__file__).resolve().parents[1] / 'agentbus' / 'web_assets'


class OnboardingButtonRegression(unittest.TestCase):
    def test_wizard_uses_registered_listeners_under_the_shipped_csp(self):
        html = (ASSETS / 'index.html').read_text(encoding='utf-8')
        script = (ASSETS / 'trigger_onboarding.js').read_text(encoding='utf-8')
        self.assertIn("script-src 'self'", html)
        self.assertNotIn('onclick=', script)
        self.assertNotIn('window.wizardInstance', script)
        self.assertIn("addEventListener('click'", script)
        for action in ('confirm_identity', 'choose_file_collaboration', 'select_software',
                       'confirm_scope', 'apply_install', 'copy_checkin', 'check_bound',
                       'bind_chat', 'run_verification', 'enable_collaboration', 'close'):
            self.assertIn(f'data-wizard-action="{action}"', script)
