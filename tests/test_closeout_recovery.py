"""Owner 2026-10-14 closeout: isolated recovery and desktop feedback regressions."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agentbus import backup, gui_state as gs
from agentbus.config import load
from agentbus.desktop_wakeup import DesktopWakeupBridge
from agentbus.web_bridge import WebBridge


class CloseoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'ui.json'
        self.state_patch = patch.object(gs, 'state_path', return_value=self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.source = self.workspace('source')
        self.target = self.workspace('target')
        self.bundle = backup.create_backup_bundle(self.source)

    def workspace(self, name):
        root = self.root / name
        bus = root / '.agentbus'
        bus.mkdir(parents=True)
        (bus / 'config.json').write_text(json.dumps({'director': 'codex', 'workers': {'codex': {'enabled': True}}}), encoding='utf-8')
        (bus / 'task-tree.json').write_text(json.dumps({'schema_version': 1, 'roots': [{'id': 'root', 'title': name}], 'branches': [], 'leaves': [{'id': 'leaf', 'parent_id': 'root', 'title': name}]}), encoding='utf-8')
        return root

    def ticket(self, mode='merge'):
        return backup.preview_backup_restore(self.target, self.bundle, mode=mode)['preview_id']

    def bytes_before(self):
        files = [self.target / '.agentbus/task-tree.json', self.target / '.agentbus/config.json', self.state]
        return {str(p): p.read_bytes() if p.exists() else None for p in files}

    def test_declared_digest_detects_modified_bundle(self):
        broken = copy.deepcopy(self.bundle)
        broken['task_tree']['title'] = 'tampered title'
        with self.assertRaises(ValueError):
            backup.preview_backup_restore(self.target, broken)

    def test_boolean_schema_is_rejected(self):
        broken = copy.deepcopy(self.bundle)
        broken.pop('bundle_sha256')
        broken['schema_version'] = True
        with self.assertRaises(ValueError):
            backup.preview_backup_restore(self.target, broken)

    def test_missing_ticket_cannot_write(self):
        before = self.bytes_before()
        with self.assertRaises(ValueError):
            backup.apply_backup_restore(self.target, self.bundle)
        self.assertEqual(before, self.bytes_before())

    def test_ticket_is_bound_to_target(self):
        other = self.workspace('other-target')
        with self.assertRaises(ValueError):
            backup.apply_backup_restore(other, self.bundle, preview_id=self.ticket())

    def test_ticket_is_bound_to_mode(self):
        with self.assertRaises(ValueError):
            backup.apply_backup_restore(self.target, self.bundle, preview_id=self.ticket(), mode='append')

    def test_ticket_rejects_changed_target_bytes(self):
        ticket = self.ticket()
        tree = self.target / '.agentbus/task-tree.json'
        data = json.loads(tree.read_text(encoding='utf-8'))
        data['leaves'][0]['title'] = 'edited after preview'
        tree.write_text(json.dumps(data), encoding='utf-8')
        before = self.bytes_before()
        with self.assertRaises(ValueError):
            backup.apply_backup_restore(self.target, self.bundle, preview_id=ticket)
        self.assertEqual(before, self.bytes_before())

    def test_ticket_rejects_same_count_preference_change(self):
        note = {'id': 'note-1', 'x': 1, 'y': 2, 'w': 200, 'h': 100, 'text': 'before'}
        self.assertTrue(gs.save_notes(self.target, [note]))
        ticket = self.ticket()
        note['text'] = 'changed'
        self.assertTrue(gs.save_notes(self.target, [note]))
        with self.assertRaises(ValueError):
            backup.apply_backup_restore(self.target, self.bundle, preview_id=ticket)
        self.assertEqual('changed', gs.load_notes(self.target)[0]['text'])

    def test_ticket_expires(self):
        ticket = self.ticket()
        with patch('time.monotonic', return_value=time.monotonic() + 3600):
            with self.assertRaises(ValueError):
                backup.apply_backup_restore(self.target, self.bundle, preview_id=ticket)

    def test_preference_failure_preserves_exact_original_bytes(self):
        before = self.bytes_before()
        with patch.object(backup, 'save_notes', return_value=False):
            with self.assertRaises(ValueError):
                backup.apply_backup_restore(self.target, self.bundle, preview_id=self.ticket())
        self.assertEqual(before, self.bytes_before())

    def test_empty_preferences_clear_old_folds_and_priorities(self):
        self.assertTrue(gs.save_branch_folds(self.target, ['root']))
        self.assertTrue(gs.save_priorities(self.target, {'leaf': 'P1'}))
        backup.apply_backup_restore(self.target, self.bundle, preview_id=self.ticket())
        self.assertEqual([], gs.load_branch_folds(self.target))
        self.assertEqual({}, gs.load_priorities(self.target))

    def test_ui_lock_failure_never_changes_project_bytes(self):
        ticket = self.ticket()
        before = self.bytes_before()
        with patch.object(gs, '_acquire_interprocess_lock', return_value=None):
            with self.assertRaises(ValueError):
                backup.apply_backup_restore(self.target, self.bundle, preview_id=ticket)
        self.assertEqual(before, self.bytes_before())

    def test_large_tree_fit_scale_persists_and_reloads(self):
        view = {'version': 1, 'scale': 0.0125, 'panX': 5, 'panY': 6, 'width': 1200, 'height': 900}
        self.assertTrue(gs.save_viewport(self.target, view))
        self.assertEqual(0.0125, gs.load_viewport(self.target)['scale'])
        for scale in (0, 0.000001, float('nan'), float('inf'), 8.01):
            with self.subTest(scale=scale):
                self.assertFalse(gs.save_viewport(self.target, {**view, 'scale': scale}))

    def test_process_exit_mid_commit_is_recovered_on_next_preview(self):
        note = {'id': 'note-1', 'x': 1, 'y': 2, 'w': 200, 'h': 100, 'text': 'recovered note'}
        self.assertTrue(gs.save_notes(self.source, [note]))
        self.bundle = backup.create_backup_bundle(self.source)
        bundle_path = self.root / 'bundle.json'
        bundle_path.write_text(json.dumps(self.bundle), encoding='utf-8')
        code = '''import json, os, sys
from pathlib import Path
from agentbus import backup, gui_state
root, state, bundle_path = map(Path, sys.argv[1:])
gui_state.state_path = lambda: state
bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
ticket = backup.preview_backup_restore(root, bundle)["preview_id"]
original = os.replace
def interrupted(source, target):
    original(source, target)
    if Path(target).resolve() == (root / ".agentbus/task-tree.json").resolve():
        os._exit(71)
os.replace = interrupted
backup.apply_backup_restore(root, bundle, preview_id=ticket)
'''
        child = subprocess.run([sys.executable, '-c', code, str(self.target), str(self.state), str(bundle_path)], capture_output=True, timeout=20)
        self.assertEqual(71, child.returncode, child.stderr.decode('utf-8', errors='replace'))
        preview = backup.preview_backup_restore(self.target, self.bundle)
        self.assertTrue(preview['plan']['already_applied'])
        self.assertEqual('recovered note', gs.load_notes(self.target)[0]['text'])

    def test_bridge_preview_and_apply_use_same_restore_mode(self):
        bridge = WebBridge(self.target)
        response = bridge.call('restore_backup', {'bundle': self.bundle, 'mode': 'append'})
        self.assertTrue(response['ok'], response)
        ticket = response['data']['preview']['preview_id']
        self.assertEqual('append', response['data']['preview']['mode'])
        result = bridge.call('restore_backup', {'bundle': self.bundle, 'mode': 'append', 'confirm': True, 'preview_id': ticket})
        self.assertTrue(result['ok'], result)
        self.assertEqual('append', result['data']['result']['mode'])

    def test_bridge_replace_requires_strict_boolean_confirmation(self):
        bridge = WebBridge(self.target)
        preview = bridge.call('restore_backup', {'bundle': self.bundle, 'mode': 'replace'})
        self.assertTrue(preview['ok'], preview)
        ticket = preview['data']['preview']['preview_id']
        before = self.bytes_before()
        denied = bridge.call('restore_backup', {'bundle': self.bundle, 'mode': 'replace', 'confirm': True,
                                               'preview_id': ticket, 'confirm_replace': 'false'})
        self.assertFalse(denied['ok'], denied)
        self.assertEqual(before, self.bytes_before())
        allowed = bridge.call('restore_backup', {'bundle': self.bundle, 'mode': 'replace', 'confirm': True,
                                                 'preview_id': ticket, 'confirm_replace': True})
        self.assertTrue(allowed['ok'], allowed)
        self.assertEqual('replace', allowed['data']['result']['mode'])

    def test_bridge_string_false_only_previews_and_does_not_write(self):
        before = self.bytes_before()
        response = WebBridge(self.target).call('restore_backup', {'bundle': self.bundle, 'confirm': 'false'})
        self.assertTrue(response['ok'], response)
        self.assertIn('preview', response['data'])
        self.assertEqual(before, self.bytes_before())


class CloseoutDesktopFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.bridge = DesktopWakeupBridge.__new__(DesktopWakeupBridge)
        self.bridge.forest = Mock()
        self.bridge.forest.generate_takeover_capsule.return_value = {'prompt': 'isolated test prompt'}

    def test_toast_does_not_claim_clipboard_loaded_when_copy_failed(self):
        emitted = []
        with patch.object(self.bridge, 'copy_to_clipboard', return_value=(False, 'LOCKED')), patch.object(self.bridge, 'show_toast', side_effect=lambda title, message: (emitted.append(message) is None, '')):
            result = self.bridge.notify_task_wakeup({'id': 'leaf'}, 'codex', auto_copy=True, play_sound=False)
        self.assertFalse(result['success'])
        self.assertFalse(result['clipboard']['completed'])
        self.assertNotIn('已装载', emitted[0])
        self.assertNotIn('Ctrl+V', emitted[0])

    def test_clipboard_helpers_are_reaped_after_communicate_error(self):
        proc = Mock()
        proc.communicate.side_effect = OSError('broken pipe')
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with patch('agentbus.desktop_wakeup.os.name', 'nt'), patch('agentbus.desktop_wakeup.subprocess.Popen', return_value=proc):
            result = self.bridge.copy_to_clipboard('isolated Unicode prompt 测试')
        self.assertFalse(result[0])
        self.assertEqual(2, proc.kill.call_count)
        self.assertGreaterEqual(proc.wait.call_count, 2)

    def test_cleanup_failure_is_reported_without_throwing(self):
        proc = Mock()
        proc.communicate.side_effect = subprocess.TimeoutExpired('clip', 3)
        proc.poll.return_value = None
        proc.kill.side_effect = OSError('already gone')
        proc.wait.side_effect = subprocess.TimeoutExpired('clip', 1)
        with patch('agentbus.desktop_wakeup.os.name', 'nt'), patch('agentbus.desktop_wakeup.subprocess.Popen', return_value=proc):
            result = self.bridge.copy_to_clipboard('isolated prompt')
        self.assertFalse(result[0])
        self.assertTrue(result[1])
