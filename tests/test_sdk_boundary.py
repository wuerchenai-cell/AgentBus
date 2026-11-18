"""Source: Codex, 2026-10-04. No-model SDK lifecycle/owned process tests."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.adapters import get_adapter


FIXTURE = r'''
import json, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, SOURCE)
from agentbus.codex_sdk import CodexSDKAdapter as RealAdapter
from agentbus import sdk_worker
stage, marker, calls = sys.argv[1:]
def note(text):
    with open(calls, 'a', encoding='utf-8') as f:
        f.write(text + '\n')
def hang(text):
    note(text)
    time.sleep(10)
    Path(marker).write_text('late write', encoding='utf-8')
class Turn:
    def __init__(self, prompt): self.prompt = prompt
    def run(self):
        if stage == 'normal':
            if self.prompt in sys.argv:
                raise RuntimeError('prompt leaked onto process command line')
            if any(Path(str(arg)).is_file() and 'secret prompt' in str(arg) for arg in sys.argv):
                raise RuntimeError('prompt leaked into command argument')
        if stage == 'early-root':
            subprocess.Popen([sys.executable, '-c',
                'import sys,time;from pathlib import Path;time.sleep(2);'
                'Path(sys.argv[1]).write_text("late descendant",encoding="utf-8")', marker])
            raise SystemExit(0)
        if stage == 'tree':
            subprocess.Popen([sys.executable, '-c',
                'import sys,time;from pathlib import Path;time.sleep(2);'
                'Path(sys.argv[1]).write_text("late descendant",encoding="utf-8")', marker])
        if stage in {'run', 'interrupt', 'tree'}: hang('run')
        return SimpleNamespace(final_response=self.prompt, error=None, status='completed')
    def interrupt(self):
        if stage == 'interrupt': hang('interrupt')
class Thread:
    id = 'durable-thread-1'
    def turn(self, prompt, **kwargs):
        if stage == 'turn': hang('turn')
        return Turn(prompt)
class Client:
    def __init__(self):
        if stage == 'secret-error':
            raise RuntimeError('Authorization: Bearer fixture_bearer_123; '
                               'api_key=fixture_api_456; Cookie: session=fixture_cookie_789')
        if stage == 'construct': hang('construct')
    def thread_start(self, **kwargs):
        note('new')
        if stage == 'start': hang('start')
        return Thread()
    def thread_resume(self, thread_id, **kwargs):
        note('resume:' + thread_id)
        return Thread()
    def close(self):
        if stage == 'close': hang('close')
class FixtureAdapter(RealAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, sdk_factory=Client, sandbox_resolver=lambda value: value,
                         approval_resolver=lambda: 'deny_all',
                         interrupt_grace_s=0.1)
    def run(self, prompt, cwd, timeout_s=1800, **kwargs):
        # An inner SDK timeout can occur before the parent's full lifecycle
        # deadline. Exercise the real interrupt call hanging in that window.
        if stage == 'interrupt': timeout_s = 0.1
        return super().run(prompt, cwd, timeout_s=timeout_s, **kwargs)
sdk_worker.CodexSDKAdapter = FixtureAdapter
raise SystemExit(sdk_worker.main())
'''


class SDKBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentbus-sdk-boundary-")
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name)
        self.marker = self.ws / "late-write.txt"
        self.calls = self.ws / "calls.txt"
        self.record = self.ws / "session.json"
        self.fixture = self.ws / "sdk_fixture.py"
        source = str(Path(__file__).resolve().parents[1])
        self.fixture.write_text("SOURCE = " + repr(source) + "\n" + FIXTURE,
                                encoding="utf-8")

    def command(self, stage):
        return [sys.executable, "-B", "-X", "utf8", str(self.fixture), stage,
                str(self.marker), str(self.calls)]

    def test_factory_places_real_sdk_behind_an_owned_process_deadline(self):
        adapter = get_adapter("codex", {"kind": "codex-sdk"})
        self.assertEqual(type(adapter).__name__, "CodexProcessAdapter")

    def test_startup_turn_and_cleanup_hangs_all_expire_without_late_writes(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        for stage in ("construct", "start", "turn", "run", "interrupt", "close"):
            self.calls.unlink(missing_ok=True)
            with self.subTest(stage=stage), patch(
                    "agentbus.sdk_boundary.worker_command", return_value=self.command(stage)):
                adapter = CodexProcessAdapter(session_record_path=self.record)
                started = time.monotonic()
                result = adapter.run("secret prompt", self.ws, timeout_s=0.9)
                self.assertEqual(result.exit_code, 124, result.output)
                self.assertLess(time.monotonic() - started, 3)
                self.assertIsNone(adapter.process_id)
                self.assertFalse(self.marker.exists())
                self.assertIn(stage, self.calls.read_text(encoding="utf-8").splitlines())
                self.record.unlink(missing_ok=True)
        time.sleep(0.2)
        self.assertFalse(self.marker.exists())

    def test_timeout_kills_owned_grandchild_but_preserves_unrelated_process(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(8)"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: unrelated.poll() is None and unrelated.kill())
        try:
            with patch("agentbus.sdk_boundary.worker_command", return_value=self.command("tree")):
                result = CodexProcessAdapter().run("bounded", self.ws, timeout_s=0.9)
            self.assertEqual(result.exit_code, 124)
            time.sleep(2.2)
            self.assertFalse(self.marker.exists())
            self.assertIsNone(unrelated.poll())
        finally:
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=3)

    def test_new_wrapper_resumes_durable_thread_and_keeps_utf8_prompt_off_argv(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        with patch("agentbus.sdk_boundary.worker_command", return_value=self.command("normal")):
            first = CodexProcessAdapter(session_record_path=self.record)
            self.assertEqual(first.start(self.ws), "durable-thread-1")
            result = first.run("中文\nsecret prompt", self.ws, timeout_s=5)
            self.assertTrue(result.ok, result.output)
            self.assertEqual(result.output, "中文\nsecret prompt")
            self.assertIsNone(first.process_id)
            second = CodexProcessAdapter(session_record_path=self.record)
            result = second.run("next step", self.ws, timeout_s=5)
        self.assertEqual(result.output, "next step")
        self.assertEqual(self.calls.read_text(encoding="utf-8").splitlines(),
                         ["new", "resume:durable-thread-1", "resume:durable-thread-1"])
        data = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(data["session_id"], "durable-thread-1")
        self.assertEqual(data["cwd"], str(self.ws.resolve()))

    def test_start_handshake_has_its_own_finite_deadline(self):
        from agentbus.adapters import AdapterError
        from agentbus.sdk_boundary import CodexProcessAdapter
        with patch("agentbus.sdk_boundary.worker_command", return_value=self.command("start")):
            adapter = CodexProcessAdapter(startup_timeout_s=0.9)
            with self.assertRaisesRegex(AdapterError, "超时"):
                adapter.start(self.ws)
        self.assertIsNone(adapter.process_id)

    def test_hidden_source_entry_rejects_malformed_request_without_model_call(self):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "agentbus",
                                 "_sdk_worker"], cwd=Path(__file__).resolve().parents[1],
                                input="{}\n", capture_output=True, text=True, encoding="utf-8",
                                timeout=5)
        self.assertEqual(result.returncode, 2)
        response = json.loads(result.stdout)
        self.assertEqual(response["type"], "error")
        self.assertIn("action", response["message"])

    def test_waiting_on_busy_adapter_uses_the_same_task_deadline(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        first_result = []
        with patch("agentbus.sdk_boundary.worker_command", return_value=self.command("run")):
            adapter = CodexProcessAdapter()
            thread = threading.Thread(target=lambda: first_result.append(
                adapter.run("first", self.ws, timeout_s=2)), daemon=True)
            thread.start()
            deadline = time.monotonic() + 2
            while not self.calls.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            started = time.monotonic()
            result = adapter.run("second", self.ws, timeout_s=0.3)
            elapsed = time.monotonic() - started
            thread.join(timeout=4)
        self.assertEqual(result.exit_code, 124)
        self.assertLess(elapsed, 0.8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.calls.read_text(encoding="utf-8").splitlines(), ["new", "run"])

    def test_close_cancels_active_worker_and_waiting_call_without_starting_another(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        first_result, second_result = [], []
        with patch("agentbus.sdk_boundary.worker_command", return_value=self.command("run")):
            adapter = CodexProcessAdapter()
            first = threading.Thread(target=lambda: first_result.append(
                adapter.run("first", self.ws, timeout_s=10)), daemon=True)
            first.start()
            deadline = time.monotonic() + 2
            while not self.calls.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            second = threading.Thread(target=lambda: second_result.append(
                adapter.run("second", self.ws, timeout_s=10)), daemon=True)
            second.start()
            time.sleep(0.05)
            adapter.close()
            first.join(timeout=2)
            second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(first_result[0].exit_code, 130)
        self.assertEqual(second_result[0].exit_code, 130)
        self.assertEqual(self.calls.read_text(encoding="utf-8").splitlines(), ["new", "run"])

    def test_repeat_start_is_idle_and_cannot_switch_workspace(self):
        from agentbus.adapters import AdapterError
        from agentbus.sdk_boundary import CodexProcessAdapter
        with patch("agentbus.sdk_boundary.worker_command", return_value=self.command("normal")):
            adapter = CodexProcessAdapter()
            self.assertEqual(adapter.start(self.ws), "durable-thread-1")
            self.assertEqual(adapter.start(self.ws), "durable-thread-1")
            with self.assertRaisesRegex(AdapterError, "工作区"):
                adapter.start(self.ws / "different-project")
        self.assertEqual(self.calls.read_text(encoding="utf-8").splitlines(), ["new"])

    def test_worker_result_rejects_wrong_types_instead_of_casting_them_to_success(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        good = {"exit_code": 0, "output": "ok", "duration_s": 0.01,
                "raw_tail": "", "retryable": False}
        for key, invalid in (("exit_code", False), ("exit_code", "0"),
                             ("output", {}), ("retryable", "false"), ("raw_tail", [])):
            frame = {"type": "result", "session_id": "fixture", "result": {**good, key: invalid}}
            command = [sys.executable, "-c", "print(" + repr(json.dumps(frame)) + ")"]
            with self.subTest(field=key), patch(
                    "agentbus.sdk_boundary.worker_command", return_value=command):
                result = CodexProcessAdapter().run("not sent on argv", self.ws, timeout_s=3)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("协议", result.output)

    def test_root_exit_cannot_leave_owned_descendant_running(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        with patch("agentbus.sdk_boundary.worker_command", return_value=self.command("early-root")):
            result = CodexProcessAdapter().run("bounded", self.ws, timeout_s=0.9)
        self.assertEqual(result.exit_code, 124)
        time.sleep(2.2)
        self.assertFalse(self.marker.exists())

    def test_result_and_start_reject_multiple_frames_or_nonstring_session_identity(self):
        from agentbus.adapters import AdapterError
        from agentbus.sdk_boundary import CodexProcessAdapter
        for session_id in (True, ["fake"]):
            frame = {"type": "session", "session_id": session_id}
            command = [sys.executable, "-c", "print(" + repr(json.dumps(frame)) + ")"]
            with patch("agentbus.sdk_boundary.worker_command", return_value=command):
                with self.assertRaisesRegex(AdapterError, "协议"):
                    CodexProcessAdapter().start(self.ws)
        frame = {"type": "session", "session_id": "bad"}
        script = "print(" + repr(json.dumps(frame)) + ");print(" + repr(json.dumps(frame)) + ")"
        with patch("agentbus.sdk_boundary.worker_command", return_value=[sys.executable, "-c", script]):
            with self.assertRaisesRegex(AdapterError, "协议"):
                CodexProcessAdapter().start(self.ws)

    def test_invalid_infinite_or_nan_deadline_never_starts_worker(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        for deadline in (float("nan"), float("inf"), -1, 0):
            result = CodexProcessAdapter().run("no call", self.ws, timeout_s=deadline)
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(result.retryable)

    def test_relative_session_record_is_resolved_in_caller_not_child_source_directory(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        adapter = CodexProcessAdapter(session_record_path=Path("temporary-record.json"))
        self.assertEqual(adapter.config["session_record_path"],
                         str((Path.cwd() / "temporary-record.json").resolve()))

    def test_worker_exception_diagnostics_do_not_publish_credentials(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        with patch("agentbus.sdk_boundary.worker_command", return_value=self.command("secret-error")):
            result = CodexProcessAdapter().run("read-only fixture", self.ws, timeout_s=3)
        self.assertEqual(result.exit_code, 1)
        for secret in ("fixture_bearer_123", "fixture_api_456", "fixture_cookie_789"):
            self.assertNotIn(secret, result.output + result.raw_tail)
        self.assertIn("[redacted]", result.output)

    def test_hostile_error_frame_and_stderr_are_scrubbed_at_parent_boundary(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        secrets = "Authorization: Bearer frame_secret; x-api-key: key_secret; Cookie: session=cookie_secret"
        frame = {"type": "error", "message": secrets}
        script = "import sys;sys.stderr.write(" + repr(secrets) + ");print(" + repr(json.dumps(frame)) + ");sys.exit(2)"
        with patch("agentbus.sdk_boundary.worker_command", return_value=[sys.executable, "-c", script]):
            result = CodexProcessAdapter().run("fixture", self.ws, timeout_s=3)
        self.assertEqual(result.exit_code, 1)
        for secret in ("frame_secret", "key_secret", "cookie_secret"):
            self.assertNotIn(secret, result.output + result.raw_tail)

    def test_success_body_remains_exact_but_runtime_diagnostics_are_scrubbed(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        body = "Example documentation: Authorization: Bearer example_should_remain\n中文"
        frame = {"type": "result", "session_id": "fixture", "result": {
            "exit_code": 0, "output": body, "duration_s": 0.1, "retryable": False,
            "raw_tail": "api_key=runtime_secret; password=hidden_password"}}
        command = [sys.executable, "-X", "utf8", "-c", "print(" + repr(json.dumps(frame, ensure_ascii=False)) + ")"]
        with patch("agentbus.sdk_boundary.worker_command", return_value=command):
            result = CodexProcessAdapter().run("fixture", self.ws, timeout_s=3)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, body)
        self.assertNotIn("runtime_secret", result.raw_tail)
        self.assertNotIn("hidden_password", result.raw_tail)

    def test_failed_sdk_result_diagnostics_are_scrubbed_without_turning_it_into_success(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        frame = {"type": "result", "session_id": "fixture", "result": {
            "exit_code": 1, "output": "failed token=failed_secret", "duration_s": 0.1,
            "retryable": False, "raw_tail": "Cookie: session=tail_secret"}}
        command = [sys.executable, "-c", "print(" + repr(json.dumps(frame)) + ")"]
        with patch("agentbus.sdk_boundary.worker_command", return_value=command):
            result = CodexProcessAdapter().run("fixture", self.ws, timeout_s=3)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.retryable)
        self.assertNotIn("failed_secret", result.output)
        self.assertNotIn("tail_secret", result.raw_tail)

    def test_stdout_flood_is_rejected_before_worker_can_perform_late_write(self):
        from agentbus.sdk_boundary import CodexProcessAdapter
        frame = {"type": "result", "session_id": "fixture", "result": {
            "exit_code": 0, "output": "x" * (128 * 1024), "duration_s": 0.1,
            "retryable": False, "raw_tail": ""}}
        frame["result"]["output"] = ""
        script = ("import json,sys,time;from pathlib import Path;frame=" + repr(frame)
                  + ";frame['result']['output']='x'*(128*1024);print(json.dumps(frame),flush=True);"
                    "time.sleep(0.2);Path(sys.argv[1]).write_text('late',encoding='utf-8')")
        command = [sys.executable, "-c", script, str(self.marker)]
        with patch("agentbus.process_boundary.MAX_PROTOCOL_BYTES", 8192, create=True), patch(
                "agentbus.sdk_boundary.worker_command", return_value=command):
            result = CodexProcessAdapter().run("fixture", self.ws, timeout_s=3)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("limit", result.output.lower())
        self.assertFalse(self.marker.exists())

    def test_stderr_flood_is_drained_with_a_fixed_tail_without_communicate(self):
        from agentbus.process_boundary import ProcessBoundary
        script = ("import sys;sys.stdin.readline();"
                  "[sys.stderr.buffer.write(b'x'*8192) for _ in range(256)];"
                  "sys.stderr.buffer.write(b'\\nTAIL_MARKER');sys.stderr.flush();"
                  "print('{\"type\":\"session\",\"session_id\":\"ok\"}',flush=True)")
        with patch.object(subprocess.Popen, "communicate", side_effect=AssertionError("unbounded collection")):
            result = ProcessBoundary().exchange([sys.executable, "-c", script], {},
                                                cwd=self.ws, timeout_s=3)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.frames, [{"type": "session", "session_id": "ok"}])
        self.assertLessEqual(len(result.stderr), 2000)
        self.assertTrue(result.stderr.endswith("TAIL_MARKER"))

    def test_continuous_stdout_flood_stops_the_owned_descendant_before_timeout(self):
        from agentbus.process_boundary import ProcessBoundary
        child = ("import sys,time;from pathlib import Path;time.sleep(0.8);"
                 "Path(sys.argv[1]).write_text('late',encoding='utf-8')")
        script = ("import subprocess,sys;subprocess.Popen([sys.executable,'-c',"
                  + repr(child) + ",sys.argv[1]]);\n"
                    "while True:\n sys.stdout.buffer.write(b'x'*8192);sys.stdout.flush()")
        started = time.monotonic()
        with patch("agentbus.process_boundary.MAX_PROTOCOL_BYTES", 16384):
            result = ProcessBoundary().exchange([sys.executable, "-c", script, str(self.marker)], {},
                                                cwd=self.ws, timeout_s=3)
        self.assertEqual(result.code, 1)
        self.assertEqual(result.frames, [])
        self.assertIn("limit", result.stderr)
        self.assertLess(time.monotonic() - started, 1.5)
        time.sleep(0.9)
        self.assertFalse(self.marker.exists())

    def test_truncated_stderr_drops_an_incomplete_credential_line(self):
        from agentbus.process_boundary import ProcessBoundary
        script = ("import sys;sys.stdin.readline();"
                  "sys.stderr.write('Cookie: session='+'SECRET'*20000+'\\npublic final line');"
                  "sys.stderr.flush();print('{\"type\":\"session\",\"session_id\":\"ok\"}')")
        result = ProcessBoundary().exchange([sys.executable, "-c", script], {},
                                            cwd=self.ws, timeout_s=3)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.stderr, "public final line")

    def test_public_errors_scrub_json_tokens_jwt_url_credentials_and_standalone_keys(self):
        from agentbus.process_boundary import public_error_text
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.c2lnbmF0dXJlX2ZpeHR1cmU"
        key = "sk-" + "proj-fixture_secret_12345"
        diagnostic = ('{"access_token":"json_fixture_secret","password":"password fixture"}\n'
                      + jwt + "\n" + key + "\nhttps://user:fixture_password@example.invalid/path")
        output = public_error_text(diagnostic)
        for secret in ("json_fixture_secret", "password fixture", jwt, key, "fixture_password"):
            self.assertNotIn(secret, output)
        self.assertIn("example.invalid/path", output)
        self.assertIn("[redacted]", output)

    def test_start_failure_uses_the_same_scrubbed_public_error_contract(self):
        from agentbus.adapters import AdapterError
        from agentbus.sdk_boundary import CodexProcessAdapter
        frame = {"type": "error", "message": "Authentication failed; api_key=start_secret"}
        command = [sys.executable, "-c", "print(" + repr(json.dumps(frame)) + ");raise SystemExit(2)"]
        with patch("agentbus.sdk_boundary.worker_command", return_value=command):
            with self.assertRaises(AdapterError) as raised:
                CodexProcessAdapter().start(self.ws)
        self.assertIn("Authentication failed", str(raised.exception))
        self.assertNotIn("start_secret", str(raised.exception))

    def test_error_text_is_bounded_after_scrubbing_even_if_value_spans_the_input_cap(self):
        from agentbus.process_boundary import public_error_text
        diagnostic = "access_token=" + "SECRET" * 10000 + "; public context"
        output = public_error_text(diagnostic)
        self.assertLessEqual(len(output), 2000)
        self.assertNotIn("SECRET", output)
        self.assertIn("[redacted]", output)


if __name__ == "__main__":
    unittest.main()
