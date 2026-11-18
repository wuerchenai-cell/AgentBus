"""Checkpoint authorization for formal task-tree leaves."""
import sys
from pathlib import Path
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tempfile
import unittest

from agentbus.bus import BusError
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.task_tree import TaskForest, TaskTreeError


class CheckpointTicketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = load(Path(self.temp.name), create=True)
        cfg.data["workers"]["alpha"] = {
            "enabled": True,
            "executor_enabled": True,
            "kind": "gui",
            "human_in_loop": True,
        }
        self.cfg = save(cfg)
        self.forest = TaskForest(self.cfg)
        self.forest.replace_structure({
            "schema_version": 1,
            "roots": [{"id": "project", "title": "Project", "goal": "Ship work"}],
            "branches": [{"id": "build", "title": "Build", "parent_id": "project"}],
            "leaves": [{"id": "first", "title": "First task", "goal": "Make output",
                        "parent_id": "build"}],
        })

    def test_ready_formal_task_cannot_be_checkpointed_and_tree_bytes_stay_unchanged(self):
        self.forest.dispatch_leaf("first", "alpha")
        before = self.forest.path.read_bytes()

        with self.assertRaises(TaskTreeError):
            self.forest.checkpoint_leaf("first", "alpha", "checkpoint before claim")

        self.assertEqual(self.forest.path.read_bytes(), before)

    def test_processing_formal_task_requires_its_original_ticket(self):
        message = self.forest.claim_leaf("first", "alpha")
        ticket = captured_ticket(message)
        before = self.forest.path.read_bytes()

        with self.assertRaises((BusError, TaskTreeError)):
            self.forest.checkpoint_leaf("first", "alpha", "missing ticket")
        self.assertEqual(self.forest.path.read_bytes(), before)

        forged = replace(ticket, claim_id="0" * 32)
        with self.assertRaises((BusError, TaskTreeError)):
            self.forest.checkpoint_leaf("first", "alpha", "wrong ticket", forged)
        self.assertEqual(self.forest.path.read_bytes(), before)

        view = self.forest.checkpoint_leaf(
            "first", "alpha", "valid checkpoint", ticket, progress=35)
        self.assertEqual(view["handoff"], "valid checkpoint")
        self.assertEqual(view["progress_percent"], 35)

    def test_terminal_formal_task_cannot_be_checkpointed_and_tree_bytes_stay_unchanged(self):
        message = self.forest.claim_leaf("first", "alpha")
        ticket = captured_ticket(message)
        self.forest.bus.finish("alpha", message, "done", ticket=ticket)
        before = self.forest.path.read_bytes()

        with self.assertRaises(TaskTreeError):
            self.forest.checkpoint_leaf("first", "alpha", "late checkpoint", ticket)

        self.assertEqual(self.forest.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
