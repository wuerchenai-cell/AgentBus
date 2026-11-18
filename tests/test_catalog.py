"""Read-only recommendations. Source: Codex, 2026-10-03.

Exercises the CLI and dialog callbacks without opening a desktop window,
installing an agent, or reading another product's configuration.
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import contextlib
import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentbus import cli, gui


class Value:
    def __init__(self, value="", **_kwargs):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.bindings = {}
        self.text = ""

    def pack(self, **_kwargs):
        pass

    def title(self, *_args):
        pass

    def transient(self, *_args):
        pass

    def geometry(self, *_args):
        pass

    def destroy(self):
        pass

    def bind(self, event, callback):
        self.bindings[event] = callback

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)

    def delete(self, *_args):
        self.text = ""

    def insert(self, _where, text):
        self.text += text


class CatalogTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.find_spec("agentbus.catalog")
        self.assertIsNotNone(spec, "recommendations need a standalone read-only catalog")
        return importlib.import_module("agentbus.catalog")

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_catalog_preserves_identity_and_never_certifies_the_readers_machine(self):
        entries = self.module().catalog()
        expected_ids = {"codex", "workbuddy", "trae", "zcode", "opencode",
                        "antigravity",
                        "gemini", "github-copilot-cli", "cursor", "cline",
                        "qwen-code", "kimi", "mistral-vibe", "factory-droid",
                        "auggie", "goose", "claude-acp", "pi-acp"}
        self.assertEqual({entry["id"] for entry in entries}, expected_ids)
        self.assertEqual(len(entries), len(expected_ids))
        forbidden = {"command", "candidate_command", "env", "package", "download",
                     "archive", "args", "can_spawn", "enabled", "auto_approve"}
        for entry in entries:
            with self.subTest(agent=entry["id"]):
                self.assertTrue({"name", "integration", "source_url", "checked_at",
                                 "local_validation", "description"} <= entry.keys())
                self.assertEqual(entry["local_validation"], "not_checked")
                self.assertTrue(entry["source_url"].startswith("https://"))
                self.assertRegex(entry["checked_at"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertTrue(any("\u4e00" <= char <= "\u9fff"
                                    for char in entry["description"]))
                self.assertFalse(forbidden & entry.keys())
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(by_id["claude-acp"]["integration"], "acp-bridge")
        self.assertEqual(by_id["pi-acp"]["integration"], "acp-bridge")
        self.assertEqual(by_id["codex"]["integration"], "codex-sdk")
        self.assertEqual(by_id["workbuddy"]["integration"], "manual")
        self.assertIn("归档", by_id["kimi"]["description"])

    def test_callers_cannot_mutate_the_shared_recommendations(self):
        module = self.module()
        entries = module.catalog()
        entries[0]["name"] = "changed by caller"
        entries.append({"id": "injected"})
        entry = module.get_entry("gemini")
        entry["description"] = "changed by caller"
        self.assertEqual(module.get_entry("codex")["name"], "Codex")
        self.assertEqual(module.get_entry("gemini")["name"], "Gemini CLI")
        self.assertNotEqual(module.get_entry("gemini")["description"], entry["description"])
        self.assertEqual(len(module.catalog()), 18)

    def test_unknown_catalog_identity_returns_a_clear_error(self):
        module = self.module()
        with self.assertRaisesRegex(ValueError, "不存在|未找到|未知"):
            module.get_entry("../missing")

    def test_cli_catalog_needs_no_workspace_and_creates_no_project(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "never-created"
            for suffix in ([], ["--workspace", str(missing)]):
                with self.subTest(argv=suffix), \
                     patch("agentbus.cli._load", side_effect=AssertionError("catalog read config")), \
                     patch("subprocess.Popen", side_effect=AssertionError("catalog started a process")), \
                     patch("socket.create_connection", side_effect=AssertionError("catalog used network")):
                    code, out, err = self.run_cli(["member", "catalog", *suffix])
                    self.assertEqual(code, 0, err)
                    self.assertEqual(len(json.loads(out)), 18)
                    self.assertFalse(missing.exists())

    def test_cli_catalog_id_filters_without_loading_configuration(self):
        with patch("agentbus.cli._load", side_effect=AssertionError("catalog read config")):
            code, out, err = self.run_cli(["member", "catalog", "--id", "claude-acp"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["integration"], "acp-bridge")
        self.assertEqual(json.loads(out)["local_validation"], "not_checked")

    def test_cli_unknown_recommendation_does_not_initialize_a_project(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "never-created"
            code, out, err = self.run_cli(["member", "catalog", "--id", "unknown-agent",
                                          "--workspace", str(missing)])
            self.assertNotEqual(code, 0)
            self.assertEqual(out, "")
            self.assertIn("unknown-agent", err)
            self.assertFalse(missing.exists())

    def test_cli_add_still_requires_identity_before_loading_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "never-created"
            with patch("agentbus.cli._load", side_effect=AssertionError("missing ID read config")):
                code, _out, err = self.run_cli(["member", "add", "--workspace", str(missing)])
            self.assertNotEqual(code, 0)
            self.assertIn("--id", err)
            self.assertFalse(missing.exists())

    def test_cli_add_still_validates_identity_and_preserves_saved_configuration(self):
        from agentbus.config import load
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            original = cfg.path.read_bytes()
            code, _out, err = self.run_cli(["member", "add", "--workspace", temp,
                                           "--id", "../outside", "--name", "Unknown"])
            self.assertNotEqual(code, 0)
            self.assertIn("成员标识", err)
            self.assertEqual(cfg.path.read_bytes(), original)

    def test_gui_recommendation_is_custom_by_default_and_clears_stale_launch_command(self):
        created = {kind: [] for kind in ("Label", "Entry", "Button", "Combobox", "Text")}

        def widget_factory(kind):
            def create(*_args, **kwargs):
                widget = Widget(**kwargs)
                created[kind].append(widget)
                return widget
            return create

        app = gui.Launcher.__new__(gui.Launcher)
        app.root = object()
        app._workflow_cfg = lambda: SimpleNamespace(workspace=Path("unused"))
        registrations = []
        app._register_member = lambda *args, **kwargs: registrations.append((args, kwargs))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("agentbus.gui.tk.Toplevel", side_effect=lambda *_: Widget()))
            stack.enter_context(patch("agentbus.gui.tk.StringVar", side_effect=lambda **kw: Value(**kw)))
            for kind in ("Label", "Entry", "Button", "Combobox"):
                stack.enter_context(patch(f"agentbus.gui.ttk.{kind}", side_effect=widget_factory(kind)))
            stack.enter_context(patch("agentbus.gui.scrolledtext.ScrolledText",
                                      side_effect=widget_factory("Text")))
            stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("dialog started process")))
            stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("dialog used network")))
            app._add_member()
            self.assertEqual(len(created["Combobox"]), 1, "dialog needs a read-only recommendation selector")
            selector = created["Combobox"][0]
            self.assertEqual(selector.kwargs["state"], "readonly")
            selected = selector.kwargs["textvariable"]
            self.assertEqual(selected.get(), "自定义成员")
            fields = [widget.kwargs["textvariable"] for widget in created["Entry"]]
            self.assertEqual([field.get() for field in fields], ["", "", ""])
            fields[2].set('["old-agent", "acp"]')
            selected.set("Gemini CLI（gemini）")
            selector.bindings["<<ComboboxSelected>>"]()
            self.assertEqual([field.get() for field in fields], ["gemini", "Gemini CLI", ""])
            self.assertEqual(registrations, [])
            details = created["Text"][0]
            self.assertEqual(details.kwargs["state"], "disabled")
            self.assertIn("https://", details.text)
            self.assertIn("acp-native", details.text)
            self.assertIn("未验", details.text)


if __name__ == "__main__":
    unittest.main()
