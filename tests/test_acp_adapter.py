import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agentbus.acp import ACPAdapter
from agentbus.config import load
from agentbus.director import Director


FAKE_ACP = r'''
import json, sys
session_id = "fake-session"
while True:
    line = sys.stdin.readline()
    if not line:
        break
    msg = json.loads(line)
    print("IN=" + json.dumps(msg), file=sys.stderr, flush=True)
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialized":
        continue
    if method == "initialize":
        response = {"jsonrpc":"2.0", "id":rid, "result":{
            "protocolVersion":1,
            "agentCapabilities":{"loadSession":True},
            "agentInfo":{"name":"fake","version":"1"}}}
    elif method == "session/new":
        response = {"jsonrpc":"2.0", "id":rid, "result":{"sessionId":session_id}}
    elif method == "session/load":
        session_id = msg["params"]["sessionId"]
        response = {"jsonrpc":"2.0", "id":rid, "result":{"sessionId":session_id}}
    elif method == "session/prompt":
        text = msg["params"]["prompt"][0]["text"]
        if text == "hang":
            continue
        if text == "test-auth-required":
            print(json.dumps({"jsonrpc":"2.0", "id":rid, "result":{
                "stopReason":"refusal", "_meta":{
                    "codebuddy.ai/errorMessage":"Authentication required. Please sign in.",
                    "codebuddy.ai/privateField":"must-not-be-exposed"}}}), flush=True)
            continue
        if "test-refusal" in text:
            print(json.dumps({"jsonrpc":"2.0", "id":rid,
                              "result":{"stopReason":"refusal"}}), flush=True)
            continue
        if text in {"test-missing-stop", "test-null-stop", "test-unknown-stop", "test-cancelled"}:
            result = {} if text == "test-missing-stop" else {"stopReason": {
                "test-null-stop": None, "test-unknown-stop": "vendor_unknown",
                "test-cancelled": "cancelled"}[text]}
            print(json.dumps({"jsonrpc":"2.0", "id":rid, "result":result}), flush=True)
            continue
        if text == "needs-permission":
            print(json.dumps({"jsonrpc":"2.0", "id":99,
                "method":"session/request_permission", "params":{
                    "sessionId":session_id,
                    "toolCall":{"toolCallId":"tool-1","title":"Write file","kind":"edit"},
                    "options":[
                        {"optionId":"allow-once","name":"Allow once","kind":"allow_once"},
                        {"optionId":"reject-once","name":"Reject","kind":"reject_once"}]}}), flush=True)
            answer = json.loads(sys.stdin.readline())
            text = answer["result"]["outcome"].get("optionId", "denied")
        print(json.dumps({"jsonrpc":"2.0", "method":"session/update", "params":{
            "sessionId":session_id,
            "update":{"sessionUpdate":"agent_message_chunk",
                      "content":{"type":"text","text":"reply:" + text}}}}), flush=True)
        response = {"jsonrpc":"2.0", "id":rid, "result":{"stopReason":"end_turn"}}
    else:
        response = {"jsonrpc":"2.0", "id":rid,
                    "error":{"code":-32601,"message":"unknown method"}}
    if response is not None:
        print(json.dumps(response), flush=True)
'''


class ACPAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake_acp.py"
        self.fake.write_text(textwrap.dedent(FAKE_ACP), encoding="utf-8")
        self.adapter = ACPAdapter([sys.executable, "-u", str(self.fake)],
                                  startup_timeout_s=3)

    def tearDown(self):
        self.adapter.close()
        self.tmp.cleanup()

    def test_two_prompts_reuse_one_live_acp_session(self):
        first = self.adapter.run("first", cwd=self.root, timeout_s=3)
        pid = self.adapter.process_id
        session = self.adapter.session_id
        second = self.adapter.run("second", cwd=self.root, timeout_s=3)

        self.assertEqual(first.exit_code, 0)
        self.assertEqual(first.output, "reply:first")
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(second.output, "reply:second")
        self.assertEqual(self.adapter.process_id, pid)
        self.assertEqual(self.adapter.session_id, session)
        self.assertIsNotNone(pid)

    def test_agentbus_mcp_is_available_for_new_and_loaded_sessions(self):
        record = self.root / ".agentbus" / "sessions" / "workbuddy.json"
        adapter = ACPAdapter(
            [sys.executable, "-u", str(self.fake)], startup_timeout_s=3,
            session_record_path=record, agent_name="workbuddy",
            mcp_servers=[{"name": "agentbus", "command": "agentbus-cli",
                          "args": ["mcp", "--identity", "workbuddy"], "env": []}],
        )
        try:
            session = adapter.start(self.root)
            first_requests = [json.loads(line[3:]) for line in adapter._stderr
                              if line.startswith("IN=")]
            created = next(item for item in first_requests
                           if item.get("method") == "session/new")
            self.assertEqual(created["params"]["mcpServers"][0]["name"], "agentbus")

            adapter.process.terminate()
            adapter.process.wait(timeout=2)
            self.assertEqual(adapter.start(self.root), session)
            loaded_requests = [json.loads(line[3:]) for line in adapter._stderr
                               if line.startswith("IN=")]
            loaded = next(item for item in loaded_requests
                          if item.get("method") == "session/load")
            self.assertEqual(loaded["params"]["mcpServers"][0]["name"], "agentbus")
        finally:
            adapter.close()

    def test_permission_request_is_forwarded_to_explicit_decider(self):
        requests = []
        adapter = ACPAdapter(
            [sys.executable, "-u", str(self.fake)],
            startup_timeout_s=3,
            permission_handler=lambda request: requests.append(request) or "allow-once",
        )
        try:
            result = adapter.run("needs-permission", cwd=self.root, timeout_s=3)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.output, "reply:allow-once")
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["toolCall"]["title"], "Write file")
        finally:
            adapter.close()

    def test_permission_defaults_to_reject_once(self):
        result = self.adapter.run("needs-permission", cwd=self.root, timeout_s=3)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "reply:reject-once")

    def test_refusal_is_not_retryable(self):
        result = self.adapter.run("test-refusal", cwd=self.root, timeout_s=3)

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.output, "ACP turn finished (refusal)")
        self.assertIs(getattr(result, "retryable", None), False)

    def test_missing_or_unknown_stop_reason_is_not_success_or_retryable(self):
        for prompt in ("test-missing-stop", "test-null-stop", "test-unknown-stop"):
            with self.subTest(prompt=prompt):
                result = self.adapter.run(prompt, cwd=self.root, timeout_s=3)
                self.assertNotEqual(result.exit_code, 0)
                self.assertIs(result.retryable, False)

    def test_refusal_preserves_public_error_without_dumping_private_metadata(self):
        result = self.adapter.run("test-auth-required", cwd=self.root, timeout_s=3)
        self.assertEqual(result.exit_code, 1)
        self.assertIs(result.retryable, False)
        self.assertIn("Authentication required. Please sign in.", result.output)
        self.assertNotIn("must-not-be-exposed", result.output)

    def test_cancelled_turn_is_not_retryable(self):
        result = self.adapter.run("test-cancelled", cwd=self.root, timeout_s=3)
        self.assertEqual(result.exit_code, 130)
        self.assertIs(result.retryable, False)

    def test_director_escalates_acp_refusal_without_redispatch(self):
        cfg = load(self.root, create=True)
        director = Director(cfg, adapter_factory=lambda _name, _conf: self.adapter)
        task = director.bus.make("human", "codex", "task", "Refusal regression",
                                 "test-refusal", result_to="human")
        director.bus.post(task)

        summary = director.run(seconds=5, review=False, idle_timeout_s=0.1)

        events = director.bus.events()
        self.assertEqual(sum(e.get("event") == "failed" for e in events), 1)
        self.assertFalse(any(e.get("event") == "redispatch" for e in events))
        self.assertEqual(summary.failed, 1)
        self.assertEqual(len(summary.escalated), 1)
        self.assertIn("refusal", director.escalations()[0].body)
        receipts = [director.bus.read(p) for p in director.bus.pending("human")
                    if director.bus.read(p).type == "result"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].reply_to, task.id)
        self.assertEqual(receipts[0].body, "ACP turn finished (refusal)")

    def test_prompt_timeout_kills_agent_process_before_returning(self):
        result = self.adapter.run("hang", cwd=self.root, timeout_s=0.1)
        self.assertEqual(result.exit_code, 124)
        self.assertIsNone(self.adapter.process)

    def test_restarts_by_loading_the_same_session_when_agent_supports_it(self):
        first = self.adapter.run("before-restart", cwd=self.root, timeout_s=3)
        old_pid = self.adapter.process_id
        session = self.adapter.session_id
        self.adapter.process.terminate()
        self.adapter.process.wait(timeout=2)

        second = self.adapter.run("after-restart", cwd=self.root, timeout_s=3)

        self.assertEqual(first.exit_code, 0)
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(second.output, "reply:after-restart")
        self.assertEqual(self.adapter.session_id, session)
        self.assertNotEqual(self.adapter.process_id, old_pid)


if __name__ == "__main__":
    unittest.main()
