"""Source: Codex, 2026-10-02. Unknown members must not escape inboxes."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
from pathlib import Path
from copy import deepcopy
from agentbus.bus import Bus, BusError
from agentbus.config import DEFAULTS, ConfigError, validate


class AgentIdSafetyTests(unittest.TestCase):
    def test_default_supervision_is_explicitly_disabled(self):
        data = deepcopy(DEFAULTS)
        self.assertEqual(data['supervision'], {
            'strict_supervision': False, 'designated_auditor': '', 'checks': []})
        validate(data)

    def test_strict_supervision_requires_another_enabled_member_and_a_check(self):
        data = deepcopy(DEFAULTS)
        data['workers']['auditor'] = {
            'enabled': True, 'executor_enabled': True,
            'can_spawn': False, 'kind': 'gui', 'transport': 'file'}
        data['supervision'] = {
            'strict_supervision': True, 'designated_auditor': 'auditor',
            'checks': [{'name': 'syntax', 'argv': ['python', '-m', 'compileall', '-q', 'agentbus'],
                        'timeout_seconds': 120}]}
        validate(data)
        same_as_coordinator = deepcopy(data)
        same_as_coordinator['supervision']['designated_auditor'] = 'codex'
        with self.assertRaises(ConfigError):
            validate(same_as_coordinator)
        for change in ('missing_auditor', 'disabled_auditor', 'missing_check', 'shell'):
            candidate = deepcopy(data)
            if change == 'missing_auditor':
                candidate['supervision']['designated_auditor'] = ''
            elif change == 'disabled_auditor':
                candidate['workers']['auditor']['enabled'] = False
            elif change == 'missing_check':
                candidate['supervision']['checks'] = []
            else:
                candidate['supervision']['checks'][0]['command'] = 'python -m compileall'
            with self.subTest(change=change), self.assertRaises(ConfigError):
                validate(candidate)

    def test_inbox_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            bus = Bus(Path(temp) / '.agentbus')
            with self.assertRaises(BusError):
                bus.register('../outside')
            self.assertFalse((Path(temp) / 'outside').exists())

    def test_config_rejects_unsafe_custom_member(self):
        data = deepcopy(DEFAULTS)
        data['workers']['../outside'] = {'enabled': True, 'can_spawn': False}
        with self.assertRaises(ConfigError):
            validate(data)

    def test_system_mailboxes_remain_usable(self):
        with tempfile.TemporaryDirectory() as temp:
            bus = Bus(Path(temp) / '.agentbus')
            self.assertTrue(bus.register('human').is_dir())
            self.assertTrue(bus.register('director').is_dir())


if __name__ == '__main__':
    unittest.main()
