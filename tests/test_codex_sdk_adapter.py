import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import os
import threading
import time
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentbus.codex_sdk import CodexSDKAdapter


class FakeResult:
    def __init__(self, response):
        self.final_response = response
        self.status = "completed"
        self.error = None


class FakeTurn:
    def __init__(self, prompt, delay=0):
        self.prompt = prompt
        self.delay = delay
        self.interrupted = False
        self.done = threading.Event()

    def run(self):
        if self.delay:
            self.done.wait(self.delay)
        return FakeResult("reply:" + self.prompt)

    def interrupt(self):
        self.interrupted = True
        self.done.set()


class FakeThread:
    def __init__(self, thread_id):
        self.id = thread_id
        self.prompts = []

    def turn(self, prompt, **kwargs):
        self.prompts.append((prompt, kwargs))
        return FakeTurn(prompt)


class FakeCodex:
    def __init__(self):
        self.started = []
        self.resumed = []
        self.closed = False

    def thread_start(self, **kwargs):
        self.started.append(kwargs)
        return FakeThread("thread-1")

    def thread_resume(self, thread_id, **kwargs):
        self.resumed.append((thread_id, kwargs))
        return FakeThread(thread_id)

    def close(self):
        self.closed = True


class CodexSDKAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.record = self.root / ".agentbus" / "sessions" / "codex.json"
        self.clients = []

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self):
        client = FakeCodex()
        self.clients.append(client)
        return client

    def test_two_turns_share_the_same_thread_and_persist_its_id(self):
        adapter = CodexSDKAdapter(sandbox="read-only", session_record_path=self.record,
                                  sdk_factory=self.factory,
                                  sandbox_resolver=lambda name: name,
                                  approval_resolver=lambda: "deny_all")
        try:
            one = adapter.run("one", self.root, timeout_s=2)
            thread = adapter.thread
            two = adapter.run("two", self.root, timeout_s=2)
            self.assertTrue(one.ok, one.output)
            self.assertTrue(two.ok, two.output)
            self.assertIs(adapter.thread, thread)
            self.assertEqual(adapter.thread_id, "thread-1")
            self.assertEqual(json_read(self.record)["session_id"], "thread-1")
            self.assertEqual(len(self.clients[0].started), 1)
        finally:
            adapter.close()

    def test_new_thread_explicitly_denies_extra_approval_and_preserves_sandbox(self):
        adapter = CodexSDKAdapter(sandbox="workspace-write", sdk_factory=self.factory,
                                  sandbox_resolver=lambda name: name,
                                  approval_resolver=lambda: "deny_all")
        try:
            adapter.start(self.root)
            options = self.clients[-1].started[0]
            self.assertEqual(options.get("approval_mode"), "deny_all")
            self.assertEqual(options["sandbox"], "workspace-write")
        finally:
            adapter.close()

    def test_resumed_thread_explicitly_denies_old_approval_escalation(self):
        first = CodexSDKAdapter(session_record_path=self.record, sdk_factory=self.factory,
                                sandbox_resolver=lambda name: name,
                                approval_resolver=lambda: "deny_all")
        first.start(self.root)
        first.close()
        second = CodexSDKAdapter(session_record_path=self.record, sdk_factory=self.factory,
                                 sandbox_resolver=lambda name: name,
                                 approval_resolver=lambda: "deny_all")
        try:
            second.start(self.root)
            self.assertEqual(self.clients[-1].resumed[0][1].get("approval_mode"), "deny_all")
        finally:
            second.close()

    def test_each_turn_explicitly_denies_extra_approval(self):
        adapter = CodexSDKAdapter(sandbox="read-only", sdk_factory=self.factory,
                                  sandbox_resolver=lambda name: name,
                                  approval_resolver=lambda: "deny_all")
        try:
            response = adapter.run("bounded task", self.root, timeout_s=1)
            self.assertTrue(response.ok, response.output)
            options = adapter.thread.prompts[0][1]
            self.assertEqual(options.get("approval_mode"), "deny_all")
            self.assertEqual(options["sandbox"], "read-only")
        finally:
            adapter.close()

    def test_new_adapter_resumes_saved_thread_without_reusing_other_workspace(self):
        first = CodexSDKAdapter(session_record_path=self.record, sdk_factory=self.factory,
                                sandbox_resolver=lambda name: name,
                                approval_resolver=lambda: "deny_all")
        first.start(self.root)
        first.close()

        second = CodexSDKAdapter(session_record_path=self.record, sdk_factory=self.factory,
                                 sandbox_resolver=lambda name: name,
                                 approval_resolver=lambda: "deny_all")
        try:
            second.start(self.root)
            self.assertEqual(self.clients[1].resumed[0][0], "thread-1")
            self.assertEqual(self.clients[1].started, [])
        finally:
            second.close()

    def test_timeout_interrupts_turn_before_returning(self):
        adapter = CodexSDKAdapter(sdk_factory=self.factory,
                                  sandbox_resolver=lambda name: name,
                                  approval_resolver=lambda: "deny_all",
                                  interrupt_grace_s=0.2)
        adapter.start(self.root)
        turn = FakeTurn("hang", delay=2)
        adapter.thread.turn = lambda prompt, **kwargs: turn
        result = adapter.run("hang", self.root, timeout_s=0.1)
        self.assertEqual(result.exit_code, 124)
        self.assertTrue(turn.interrupted)
        adapter.close()

    def test_failed_turn_preserves_runtime_diagnostic_tail(self):
        adapter = CodexSDKAdapter(sdk_factory=self.factory,
                                  sandbox_resolver=lambda name: name,
                                  approval_resolver=lambda: "deny_all")
        adapter.start(self.root)
        adapter.codex._client = SimpleNamespace(
            _stderr_tail=lambda: "runtime connection diagnostic")

        class FailedTurn:
            def run(self):
                raise RuntimeError("connection lost")

        adapter.thread.turn = lambda prompt, **kwargs: FailedTurn()
        try:
            result = adapter.run("one", self.root, timeout_s=1)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("connection lost", result.output)
            self.assertEqual(result.raw_tail, "runtime connection diagnostic")
        finally:
            adapter.close()

    def test_start_failure_preserves_runtime_diagnostic_before_cleanup(self):
        client = self.factory()
        client._client = SimpleNamespace(_stderr_tail=lambda: "startup connection diagnostic")

        def fail_start(**kwargs):
            raise RuntimeError("startup failed")

        client.thread_start = fail_start
        adapter = CodexSDKAdapter(sdk_factory=lambda: client,
                                  sandbox_resolver=lambda name: name,
                                  approval_resolver=lambda: "deny_all")
        result = adapter.run("one", self.root, timeout_s=1)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("startup connection diagnostic", result.output)
        self.assertEqual(result.raw_tail, "startup connection diagnostic")

    def test_timeout_exposes_interrupt_failure_and_closes_client(self):
        adapter = CodexSDKAdapter(sdk_factory=self.factory,
                                  sandbox_resolver=lambda name: name,
                                  approval_resolver=lambda: "deny_all",
                                  interrupt_grace_s=0.1)
        adapter.start(self.root)
        turn = FakeTurn("hang", delay=2)

        def fail_interrupt():
            raise RuntimeError("interrupt channel disconnected")

        turn.interrupt = fail_interrupt
        adapter.thread.turn = lambda prompt, **kwargs: turn
        try:
            result = adapter.run("one", self.root, timeout_s=0.1)
            self.assertEqual(result.exit_code, 124)
            self.assertIn("interrupt channel disconnected", result.output)
            self.assertIsNone(adapter.codex)
        finally:
            turn.done.set()
            adapter.close()

    def test_timeout_preserves_diagnostics_before_forced_client_cleanup(self):
        adapter = CodexSDKAdapter(sdk_factory=self.factory,
                                  sandbox_resolver=lambda name: name,
                                  approval_resolver=lambda: "deny_all",
                                  interrupt_grace_s=0.1)
        adapter.start(self.root)
        adapter.codex._client = SimpleNamespace(
            _stderr_tail=lambda: "runtime still reconnecting")
        turn = FakeTurn("hang", delay=2)
        turn.interrupt = lambda: None
        adapter.thread.turn = lambda prompt, **kwargs: turn
        try:
            result = adapter.run("one", self.root, timeout_s=0.1)
            self.assertEqual(result.exit_code, 124)
            self.assertIsNone(adapter.codex)
            self.assertEqual(result.raw_tail, "runtime still reconnecting")
        finally:
            turn.done.set()
            adapter.close()

    def test_official_sdk_config_includes_identity_scoped_agentbus_mcp(self):
        captured = {}

        class Config:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class Codex(FakeCodex):
            def __init__(self, config=None):
                super().__init__()
                captured["codex_config"] = config

        fake_sdk = SimpleNamespace(
            Codex=Codex,
            CodexConfig=Config,
            Sandbox=SimpleNamespace(workspace_write="workspace_write"),
            ApprovalMode=SimpleNamespace(deny_all="deny_all"),
        )
        adapter = CodexSDKAdapter(agent_name="codex", sandbox="workspace-write")
        try:
            with patch.dict(sys.modules, {"openai_codex": fake_sdk}):
                adapter.start(self.root)
            overrides = captured["config_overrides"]
            self.assertTrue(any(
                value.startswith("mcp_servers.agentbus.command=")
                for value in overrides))
            self.assertTrue(any(
                value.startswith("mcp_servers.agentbus.args=")
                and "--identity" in value and "codex" in value
                for value in overrides))
        finally:
            adapter.close()


class CodexSDKManagedBinaryTests(unittest.TestCase):
    """Source: Codex internal regression review, 2026-10-04; no model calls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local = self.root / "local-app-data"
        self.binroot = self.local / "OpenAI" / "Codex" / "bin"
        self.captured = {}
        captured = self.captured

        class Config:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class Client:
            def __init__(self, config=None):
                pass

        self.sdk = SimpleNamespace(CodexConfig=Config, Codex=Client)

    def tearDown(self):
        self.tmp.cleanup()

    def binary(self, folder):
        path = self.binroot / folder / "codex.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"controlled fixture; never executed")
        return path

    def construct(self, configured, *, path_result=None):
        adapter = CodexSDKAdapter(codex_bin=configured)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}), \
                patch("shutil.which", return_value=path_result), \
                patch.dict(sys.modules, {"openai_codex": self.sdk}):
            adapter._new_codex(self.root)
        return self.captured.get("codex_bin")

    def test_stale_official_managed_binary_moves_to_new_install_without_config_reads(self):
        current = self.binary("8aaf1547b825b104")
        stale = self.binroot / "a51e250fa15c740a" / "codex.exe"
        with patch.object(Path, "read_text", side_effect=AssertionError("private config read")):
            self.assertEqual(self.construct(stale), str(current))

    def test_valid_explicit_custom_binary_is_kept(self):
        custom = self.root / "user-selected" / "codex.exe"
        custom.parent.mkdir()
        custom.write_bytes(b"controlled fixture; never executed")
        self.binary("8aaf1547b825b104")
        self.assertEqual(self.construct(custom), str(custom))

    def test_missing_custom_binary_never_switches_to_managed_install(self):
        current = self.binary("8aaf1547b825b104")
        missing = self.root / "custom" / "codex.exe"
        from agentbus.adapters import AdapterError
        with self.assertRaises(AdapterError):
            self.construct(missing, path_result=str(current))

    def test_missing_managed_install_reports_error(self):
        stale = self.binroot / "a51e250fa15c740a" / "codex.exe"
        from agentbus.adapters import AdapterError
        with self.assertRaises(AdapterError):
            self.construct(stale)

    def test_current_managed_path_on_path_precedes_other_installed_versions(self):
        current = self.binary("8aaf1547b825b104")
        other = self.binary("f1e5e36960c35938")
        os.utime(current, (100, 100))
        os.utime(other, (200, 200))
        stale = self.binroot / "a51e250fa15c740a" / "codex.exe"
        self.assertEqual(self.construct(stale, path_result=str(current)), str(current))

    def test_foreign_path_binary_does_not_replace_removed_managed_binary(self):
        current = self.binary("8aaf1547b825b104")
        foreign = self.root / "foreign" / "codex.exe"
        foreign.parent.mkdir()
        foreign.write_bytes(b"controlled fixture; never executed")
        stale = self.binroot / "a51e250fa15c740a" / "codex.exe"
        self.assertEqual(self.construct(stale, path_result=str(foreign)), str(current))

    def test_managed_scan_chooses_newest_binary_and_ignores_non_hash_folder(self):
        old = self.binary("1111111111111111")
        current = self.binary("2222222222222222")
        non_managed = self.binary("user-custom")
        os.utime(old, (100, 100))
        os.utime(current, (200, 200))
        os.utime(non_managed, (300, 300))
        stale = self.binroot / "a51e250fa15c740a" / "codex.exe"
        self.assertEqual(self.construct(stale), str(current))

    def test_missing_non_hash_directory_is_not_treated_as_managed(self):
        self.binary("8aaf1547b825b104")
        missing = self.binroot / "user-custom" / "codex.exe"
        from agentbus.adapters import AdapterError
        with self.assertRaises(AdapterError):
            self.construct(missing)

    def test_no_explicit_binary_preserves_sdk_default_resolution(self):
        self.binary("8aaf1547b825b104")
        self.assertIsNone(self.construct(None))


def json_read(path):
    import json
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
