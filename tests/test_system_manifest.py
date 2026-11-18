"""Safe, bounded project quick-start manifest and onboarding capsule tests."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pathlib import Path
import tempfile
import unittest

from agentbus.system_manifest import (
    QUICKSTART_NAME,
    ensure_quickstart,
    render_onboarding_capsule,
    render_quickstart,
)


def sample_context(workspace: Path):
    return {
        "workspace": str(workspace),
        "output_root": "D:/AgentBus output",
        "target": "Ship the AgentBus desktop application",
        "tree_id": "tree-release",
        "tree_title": "AgentBus release",
        "coordinator": "codex",
        "members": [
            {"id": "codex", "name": "Codex", "mode": "sdk"},
            {"id": "workbuddy", "name": "WorkBuddy", "mode": "file"},
        ],
        "tasks": [
            {"id": "task-ui", "section": "§1.2", "title": "Improve the task canvas",
             "state": "working", "assignee": "workbuddy"},
            {"id": "task-release", "section": "§4.1", "title": "Verify the release package",
             "state": "ready", "assignee": None},
        ],
        "inbox_counts": {
            "codex": {"ready": 1, "processing": 0, "blocked": 0},
            "workbuddy": {"ready": 0, "processing": 1, "blocked": 0},
        },
    }


class QuickstartManifestTests(unittest.TestCase):
    def test_render_is_deterministic_and_contains_only_the_explicit_project_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            context = sample_context(workspace)

            first = render_quickstart(context)
            second = render_quickstart(context)

        self.assertEqual(first, second)
        self.assertIn("AgentBus 快速上手", first)
        self.assertIn("Ship the AgentBus desktop application", first)
        self.assertIn(str(workspace), first)
        self.assertIn("D:/AgentBus output", first)
        self.assertIn("§1.2", first)
        self.assertIn("task-ui", first)
        self.assertIn("WorkBuddy", first)
        self.assertIn("ready 0", first)
        self.assertIn("不修改控制目录", first)
        self.assertNotIn("token", first.casefold())
        self.assertNotIn("api_key", first.casefold())

    def test_render_rejects_unknown_fields_and_unsafe_nested_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = sample_context(Path(temporary))
            context["environment"] = {"API_KEY": "never render this"}
            with self.assertRaises(ValueError):
                render_quickstart(context)

            context = sample_context(Path(temporary))
            context["tasks"][0]["private_notes"] = "must be rejected"
            with self.assertRaises(ValueError):
                render_quickstart(context)

    def test_render_sanitizes_multiline_display_values_and_limits_collection_sizes(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = sample_context(Path(temporary))
            context["target"] = "first line\nsecond line"
            rendered = render_quickstart(context)
            self.assertNotIn("first line\nsecond line", rendered)

            context = sample_context(Path(temporary))
            context["tasks"] = context["tasks"] * 1000
            with self.assertRaises(ValueError):
                render_quickstart(context)

    def test_capsule_is_short_and_scoped_to_the_selected_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = sample_context(Path(temporary))
            capsule = render_onboarding_capsule(context, "workbuddy")

        self.assertLessEqual(len(capsule), 1800)
        self.assertIn("平级", capsule)
        self.assertIn("§1.2", capsule)
        self.assertIn("WorkBuddy", capsule)
        self.assertIn("output_root", capsule)
        self.assertNotIn("task-release", capsule)
        self.assertIn("待处理 0 条", capsule)
        self.assertIn("产物路径", capsule)
        self.assertIn("未验证项", capsule)

    def test_ensure_creates_then_leaves_unchanged_agentbus_owned_file_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            context = sample_context(workspace)

            created = ensure_quickstart(workspace, context)
            path = workspace / QUICKSTART_NAME
            first_bytes = path.read_bytes()
            unchanged = ensure_quickstart(workspace, context)
            self.assertEqual(first_bytes, path.read_bytes())

        self.assertEqual(created["status"], "created")
        self.assertTrue(created["created_or_updated"])
        self.assertFalse(created["conflict"])
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertFalse(unchanged["created_or_updated"])
        self.assertTrue(first_bytes.decode("utf-8").startswith("<!-- AgentBus-managed"))

    def test_ensure_refreshes_agentbus_owned_file_when_context_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first_context = sample_context(workspace)
            ensure_quickstart(workspace, first_context)
            old = (workspace / QUICKSTART_NAME).read_bytes()

            next_context = sample_context(workspace)
            next_context["target"] = "A revised project goal"
            refreshed = ensure_quickstart(workspace, next_context)
            new = (workspace / QUICKSTART_NAME).read_bytes()

        self.assertEqual(refreshed["status"], "updated")
        self.assertTrue(refreshed["created_or_updated"])
        self.assertNotEqual(old, new)
        self.assertIn("A revised project goal", new.decode("utf-8"))

    def test_ensure_preserves_user_edits_and_unmanaged_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            context = sample_context(workspace)
            path = workspace / QUICKSTART_NAME
            path.write_text("User-authored guide\n", encoding="utf-8")

            result = ensure_quickstart(workspace, context)
            self.assertEqual(path.read_text(encoding="utf-8"), "User-authored guide\n")

        self.assertEqual(result["status"], "conflict")
        self.assertTrue(result["conflict"])

    def test_ensure_detects_edits_inside_a_managed_file_without_overwriting_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            context = sample_context(workspace)
            ensure_quickstart(workspace, context)
            path = workspace / QUICKSTART_NAME
            original = path.read_bytes()
            changed = original.replace(b"AgentBus", b"EditedByUser", 1)
            path.write_bytes(changed)

            next_context = sample_context(workspace)
            next_context["target"] = "Changed target"
            result = ensure_quickstart(workspace, next_context)
            self.assertEqual(path.read_bytes(), changed)

        self.assertEqual(result["status"], "conflict")

    def test_ensure_requires_an_existing_non_link_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            absent = Path(temporary) / "not-created"
            result = ensure_quickstart(absent, sample_context(absent))
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(absent.exists())

    def test_ensure_rejects_context_for_another_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            other = workspace / "other"
            other.mkdir()
            result = ensure_quickstart(workspace, sample_context(other))
        self.assertEqual(result["status"], "conflict")
        self.assertFalse((workspace / QUICKSTART_NAME).exists())


if __name__ == "__main__":
    unittest.main()
