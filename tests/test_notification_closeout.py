"""2026-10-14: the delivery notification API must report real channel outcomes."""
import subprocess
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agentbus.config import load
from agentbus.desktop_wakeup import DesktopWakeupBridge
from agentbus.web_bridge import WebBridge


class NotificationCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        load(self.root, create=True)
        self.bridge = WebBridge(self.root)
        native = patch('agentbus.web_bridge.subprocess.Popen', side_effect=OSError('mocked native helper failure'))
        native.start()
        self.addCleanup(native.stop)
        sound = patch('winsound.Beep', side_effect=OSError('mocked sound failure'))
        sound.start()
        self.addCleanup(sound.stop)

    def test_requested_sound_failure_does_not_report_played(self):
        with patch.object(DesktopWakeupBridge, 'play_chime', return_value=(False, 'SOUND_FAILED')), patch.object(DesktopWakeupBridge, 'show_toast', return_value=(True, '')):
            result = self.bridge.call('show_desktop_notification', {'title': 'title', 'message': 'message', 'sound': True})
        self.assertTrue(result['ok'], result)
        data = result['data']
        self.assertFalse(data['sound_played'])
        self.assertTrue(data['notified'])
        self.assertEqual({'requested': True, 'completed': False, 'error': 'SOUND_FAILED'}, data['sound'])

    def test_toast_failure_is_reported_separately(self):
        with patch.object(DesktopWakeupBridge, 'show_toast', return_value=(False, 'TOAST_EXIT_5')):
            result = self.bridge.call('show_desktop_notification', {'title': 'title', 'message': 'message', 'sound': False, 'task_id': 'm-test'})
        self.assertTrue(result['ok'], result)
        data = result['data']
        self.assertFalse(data['notified'])
        self.assertFalse(data['sound_played'])
        self.assertEqual({'requested': True, 'completed': False, 'error': 'TOAST_EXIT_5'}, data['toast'])
        self.assertFalse(data['sound']['requested'])
        self.assertEqual('m-test', data['task_id'])

    def test_native_toast_timeout_becomes_failed_feedback(self):
        with patch.dict(os.environ), patch('agentbus.desktop_wakeup.subprocess.run', side_effect=subprocess.TimeoutExpired('powershell', 7)):
            os.environ.pop('AGENTBUS_DISABLE_DESKTOP_POPUP', None)
            result = self.bridge.call('show_desktop_notification', {'sound': False})
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['data']['notified'])
        self.assertIn('timed out', result['data']['toast']['error'])
