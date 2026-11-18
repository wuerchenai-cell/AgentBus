"""JR-20261108-R05: inspection must not initialize or rewrite the workspace."""
import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from agentbus.bus import Bus
from agentbus.cli import main
from agentbus.config import load
from agentbus.task_tree import TaskForest


class ReadOnlyStatusRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.cfg = load(self.workspace, create=True)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()
        self.bus.register('human')

    def observation(self):
        return {
            path.relative_to(self.workspace).as_posix(): (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
            for path in self.workspace.rglob('*')
        }

    def call(self, *args, workspace=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = main([*args, '--workspace', str(workspace or self.workspace)])
        return rc, output.getvalue()

    def test_status_does_not_register_workers_or_touch_events(self):
        before = self.observation()
        rc, text = self.call('status')
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.observation(), before)

    def test_escalations_are_read_without_cleanup_or_registration(self):
        notice = self.bus.make('codex', 'human', 'escalation', body='Needs a decision')
        self.bus.post(notice)
        before = self.observation()
        rc, text = self.call('escalations')
        self.assertEqual(rc, 0, text)
        self.assertIn(notice.id, text)
        self.assertEqual(self.observation(), before)

    def test_tree_status_and_show_do_not_create_mailboxes(self):
        forest = TaskForest(self.cfg)
        forest.seed_leaf({'id': 'leaf', 'title': 'Inspect tree'}, branch_id='work')
        for args in (('tree', 'status'), ('tree', 'show', '--leaf', '1.1')):
            with self.subTest(args=args):
                before = self.observation()
                rc, text = self.call(*args)
                self.assertEqual(rc, 0, text)
                self.assertEqual(self.observation(), before)

    def test_reading_notices_ignores_terminal_duplicates_without_deleting_them(self):
        notice = self.bus.make('codex', 'human', 'escalation', body='Already resolved')
        self.bus.post(notice)
        terminal = self.bus.finish_if_ready('human', notice, 'done')
        duplicate = self.bus.dirs('human')['ready'] / f'{notice.id}.json'
        duplicate.write_bytes(terminal.read_bytes())
        before = self.observation()
        rc, text = self.call('escalations')
        self.assertEqual(rc, 0, text)
        self.assertNotIn(notice.id, text)
        self.assertTrue(duplicate.exists())
        self.assertEqual(self.observation(), before)

    def test_unconfigured_status_fails_without_creating_project_state(self):
        empty = self.workspace / 'unconfigured'
        empty.mkdir()
        before = self.observation()
        rc, _ = self.call('status', workspace=empty)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.observation(), before)
