"""Tests for interactive wire connections and dynamic topology updates (Section 1.11 & 1.12)."""
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

from agentbus.config import load
from agentbus.task_tree import TaskForest, TaskTreeError


def sample_tree_structure():
    return {
        "roots": [{"id": "project", "title": "Test Project", "goal": "Project Goal"}],
        "branches": [
            {"id": "desktop", "parent_id": "project", "title": "Desktop Branch"},
            {"id": "reliability", "parent_id": "project", "title": "Reliability Branch"},
        ],
        "leaves": [
            {
                "id": "leaf-a",
                "parent_id": "desktop",
                "title": "Leaf Task A",
                "goal": "Task A Goal",
                "section_no": "1.1",
                "dependencies": [],
            },
            {
                "id": "leaf-b",
                "parent_id": "desktop",
                "title": "Leaf Task B",
                "goal": "Task B Goal",
                "section_no": "1.2",
                "dependencies": [],
            },
            {
                "id": "leaf-c",
                "parent_id": "reliability",
                "title": "Leaf Task C",
                "goal": "Task C Goal",
                "section_no": "2.1",
                "dependencies": ["leaf-a"],
            },
        ],
    }


class InteractiveWiringAndTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.cfg = load(self.workspace, create=True)
        self.forest = TaskForest(self.cfg)
        self.forest.replace_structure(sample_tree_structure())

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_dependencies_connect_and_disconnect(self):
        # 1. Connect leaf-a to leaf-b (leaf-b depends on leaf-a)
        snap = self.forest.update_leaf_topology("leaf-b", dependencies=["leaf-a"])
        leaf_b = next(l for l in snap["leaves"] if l["id"] == "leaf-b")
        self.assertEqual(leaf_b["dependencies"], ["leaf-a"])

        # 2. Add multiple dependencies with deduplication and self-reference filter
        snap = self.forest.update_leaf_topology("leaf-b", dependencies=["leaf-a", "leaf-c", "leaf-b", "leaf-a", "non-existent"])
        leaf_b = next(l for l in snap["leaves"] if l["id"] == "leaf-b")
        self.assertIn("leaf-a", leaf_b["dependencies"])
        self.assertIn("leaf-c", leaf_b["dependencies"])
        self.assertNotIn("leaf-b", leaf_b["dependencies"])  # self ignored
        self.assertNotIn("non-existent", leaf_b["dependencies"])  # non-existent ignored
        self.assertEqual(len(leaf_b["dependencies"]), 2)

        # 3. Disconnect all dependencies
        snap = self.forest.update_leaf_topology("leaf-b", dependencies=[])
        leaf_b = next(l for l in snap["leaves"] if l["id"] == "leaf-b")
        self.assertEqual(leaf_b["dependencies"], [])

    def test_update_parent_branch_group(self):
        # Move leaf-a from 'desktop' to 'reliability' branch
        snap = self.forest.update_leaf_topology("leaf-a", parent_id="reliability")
        leaf_a = next(l for l in snap["leaves"] if l["id"] == "leaf-a")
        self.assertEqual(leaf_a["parent_id"], "reliability")

        # Invalid parent branch raises error
        with self.assertRaises(TaskTreeError):
            self.forest.update_leaf_topology("leaf-a", parent_id="unknown-branch")


if __name__ == "__main__":
    unittest.main()
