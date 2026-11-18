import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import io
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from agentbus.config import DEFAULTS
from agentbus.cli import cmd_escalations, cmd_status
from agentbus.mcp_server import AgentBusMcp, TOOLS, serve, session_tools_for


class TestMcpRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        data = json.loads(json.dumps(DEFAULTS))
        data["workspace"] = str(self.workspace)
        config = self.workspace / ".agentbus" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(data), encoding="utf-8")
        self.app = AgentBusMcp(self.workspace)

    def tearDown(self):
        self.tmp.cleanup()

    def claim_for_delivery(self, agent, task):
        """Use the real claim/heartbeat path before a transport result commit."""
        self.app.next_task(agent)
        original = self.app.bus.read(self.app.bus.dirs(agent)['processing'] / (task.id + '.json'))
        ticket = self.app._tickets[(agent, task.id)]
        self.app.bus.heartbeat(agent, original, ticket, note='routing test executor progress')

    def test_send_schema_requires_explicit_result_to_for_dispatch_types(self):
        tool = next(t for t in TOOLS if t["name"] == "agentbus_send")
        schema = tool["inputSchema"]
        self.assertIn("result_to", schema["properties"])
        conditional = schema["allOf"][0]
        self.assertEqual(conditional["if"]["properties"]["type"]["enum"],
                         ["goal", "task", "review"])
        self.assertIn("result_to", conditional["then"]["required"])
        self.assertIn("goal", schema["properties"]["type"]["enum"])

    def test_session_tools_bind_sender_and_expose_only_peer_messaging(self):
        tools = session_tools_for("workbuddy")

        self.assertEqual([tool["name"] for tool in tools], ["agentbus_send"])
        schema = tools[0]["inputSchema"]
        self.assertNotIn("sender", schema["properties"])
        self.assertNotIn("sender", schema["required"])
        self.assertIn("workbuddy", tools[0]["description"])

    def test_bound_session_message_uses_the_bound_agent_identity(self):
        app = AgentBusMcp(self.workspace, identity="workbuddy", session_tools=True)
        app.session_send(to="trae", body="peer hello", type="info")
        message = app.bus.read(app.bus.pending("trae")[0])
        self.assertEqual(message.sender, "workbuddy")
        self.assertEqual(message.body, "peer hello")

    def test_bound_session_rejects_unselected_identity(self):
        with self.assertRaisesRegex(ValueError, "未在当前小组启用"):
            AgentBusMcp(self.workspace, identity="ghost", session_tools=True)

    def test_session_stdio_only_exposes_send_and_ignores_spoofed_sender(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "agentbus_send",
                "arguments": {"sender": "codex", "to": "trae",
                              "body": "hello", "type": "info"},
            }},
        ]
        output = io.StringIO()
        code = serve(self.workspace,
                     stdin=io.StringIO("\n".join(map(json.dumps, requests)) + "\n"),
                     stdout=output, identity="workbuddy", session_tools=True)

        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        tools = responses[1]["result"]["tools"]
        self.assertEqual(code, 0)
        self.assertEqual([tool["name"] for tool in tools], ["agentbus_send"])
        message = self.app.bus.read(self.app.bus.pending("trae")[0])
        self.assertEqual(message.sender, "workbuddy")
        self.assertEqual(message.body, "hello")

    def test_wait_task_tool_schema_is_bounded(self):
        tool = next(t for t in TOOLS if t["name"] == "agentbus_wait_task")
        schema = tool["inputSchema"]
        self.assertEqual(schema["required"], ["agent"])
        self.assertEqual(schema["properties"]["timeout_s"]["minimum"], 1)
        self.assertEqual(schema["properties"]["timeout_s"]["maximum"], 120)
        self.assertEqual(schema["properties"]["timeout_s"]["default"], 120)

    def test_empty_next_task_directs_agents_to_event_wait_not_polling(self):
        response = self.app.next_task("workbuddy")

        self.assertIn("agentbus_wait_task", response)
        self.assertIn("文件事件", response)
        self.assertNotIn("稍后再来领", response)
        self.assertIn("不要重复调用", response)

    def test_cli_status_renders_human_escalations_in_gbk_console(self):
        escalation = self.app.bus.make(
            "workbuddy", "human", "escalation", "route clarification", "choose recipient")
        self.app.bus.post(escalation)
        buffer = io.BytesIO()
        stdout = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
        with patch("sys.stdout", stdout):
            code = cmd_status(SimpleNamespace(workspace=self.workspace))
            stdout.flush()
        self.assertEqual(code, 0)
        rendered = buffer.getvalue().decode("gbk")
        self.assertIn(escalation.id, rendered)
        self.assertIn("需要你决定", rendered)
        stdout.detach()

    def test_cli_escalations_renders_nonempty_human_inbox(self):
        escalation = self.app.bus.make(
            "workbuddy", "human", "escalation", "route clarification", "choose recipient")
        self.app.bus.post(escalation)
        output = io.StringIO()

        with patch("sys.stdout", output):
            code = cmd_escalations(SimpleNamespace(workspace=self.workspace))

        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertIn(escalation.id, rendered)
        self.assertIn(escalation.title, rendered)
        self.assertIn(escalation.body, rendered)

    def test_wait_task_claims_actionable_task_and_leaves_info_alone(self):
        info = self.app.bus.make("codex", "workbuddy", "info", "FYI", "old notice")
        task = self.app.bus.make("codex", "workbuddy", "task", "wake me", "do work",
                                 result_to="codex")
        self.app.bus.post(info)
        self.app.bus.post(task)

        response = self.app.wait_task("workbuddy", 1)

        self.assertIn(task.id, response)
        self.assertIn("wake me", response)
        self.assertTrue(self.app.bus.dirs("workbuddy")["processing"].joinpath(
            task.id + ".json").exists())
        self.assertTrue(self.app.bus.dirs("workbuddy")["ready"].joinpath(
            info.id + ".json").exists())

    def test_wait_task_returns_result_receipt_to_coordinator(self):
        source = self.app.bus.make("codex", "trae", "review", "independent review",
                                   "check the implementation", result_to="codex")
        self.app.bus.post(source)
        result = self.app.bus.make("trae", "workbuddy", "result", "review complete",
                                   "review found one edge case", reply_to=source.id,
                                   result_to="workbuddy")
        self.app.bus.post(result)

        response = self.app.wait_task("workbuddy", 1)

        self.assertIn(result.id, response)
        self.assertIn("review found one edge case", response)
        self.assertTrue(self.app.bus.dirs("workbuddy")["done"].joinpath(
            result.id + ".json").exists())

    def test_wait_task_wakes_on_new_file_event_and_claims_once(self):
        entered_wait = threading.Event()
        original_wait = self.app.bus.wait_for_work

        def observed_wait(*args, **kwargs):
            entered_wait.set()
            return original_wait(*args, **kwargs)

        result = {}
        with patch.object(self.app.bus, "wait_for_work", side_effect=observed_wait):
            thread = threading.Thread(
                target=lambda: result.setdefault("text", self.app.wait_task("workbuddy", 5)),
                daemon=True)
            thread.start()
            self.assertTrue(entered_wait.wait(2), "waiter did not enter the event wait")
            task = self.app.bus.make("codex", "workbuddy", "task", "event task", "do it",
                                     result_to="codex")
            self.app.bus.post(task)
            thread.join(5)

        self.assertFalse(thread.is_alive(), "waiter did not resume after the file event")
        self.assertIn(task.id, result["text"])
        self.assertEqual(len(self.app.bus.processing("workbuddy")), 1)
        claims = [e for e in self.app.bus.events()
                  if e.get("event") == "claim" and e.get("id") == task.id]
        self.assertEqual(len(claims), 1)

    def test_wait_task_returns_clean_timeout_and_validates_bounds(self):
        with patch.object(self.app.bus, "wait_for_work", return_value=None):
            response = self.app.wait_task("workbuddy", 1)
        self.assertIn("等待超时", response)
        self.assertFalse(self.app.bus.processing("workbuddy"))
        for timeout in (0, 121, 1.5, True):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout_s"):
                    self.app.wait_task("workbuddy", timeout)

    def test_send_resolves_alias_and_persists_concrete_result_recipient(self):
        text = self.app.send("trae", "zcode", "please do", type="task", result_to="director")
        msg_id = text.split()[1]
        msg = self.app.bus.find_message(msg_id)
        self.assertEqual(msg.result_to, self.app.cfg.director)
        self.assertNotEqual(msg.result_to, "director")

    def test_send_result_requires_reply_to_and_uses_new_id(self):
        with self.assertRaisesRegex(ValueError, "reply_to"):
            self.app.send("trae", "codex", "done", type="result")
        source = self.app.bus.make("codex", "trae", "task", "source", "do work",
                                   result_to="codex")
        self.app.bus.post(source)
        self.claim_for_delivery('trae', source)
        text = self.app.send("trae", "codex", "done", type="result",
                             reply_to=source.id)
        result_id = text.split()[1]
        result = self.app.bus.find_message(result_id)
        self.assertEqual(result.reply_to, source.id)
        self.assertNotEqual(result.id, result.reply_to)
        schema = next(t for t in TOOLS if t["name"] == "agentbus_send")["inputSchema"]
        self.assertIn("reply_to", schema["properties"])
        self.assertIn("reply_to", schema["allOf"][1]["then"]["required"])

    def test_send_result_reuses_exact_duplicate_and_rejects_second_payload(self):
        source = self.app.bus.make("codex", "trae", "task", "source", "do work",
                                   result_to="codex")
        self.app.bus.post(source)
        self.claim_for_delivery('trae', source)
        first = self.app.send("trae", "codex", "done", type="result",
                              reply_to=source.id)
        result_id = first.split()[1]

        repeated = self.app.send("trae", "codex", "done", type="result",
                                 reply_to=source.id)

        self.assertIn(result_id, repeated)
        with self.assertRaisesRegex(ValueError, "该任务已存在回执"):
            self.app.send("trae", "codex", "different result", type="result",
                          reply_to=source.id)
        linked = [self.app.bus.read(path) for path in self.app.bus.pending("codex")
                  if self.app.bus.read(path).type == "result"
                  and self.app.bus.read(path).reply_to == source.id]
        self.assertEqual([item.id for item in linked], [result_id])

    def test_report_after_committed_direct_send_keeps_terminal_and_does_not_block(self):
        source = self.app.bus.make("codex", "trae", "task", "source", "do work",
                                   result_to="codex")
        self.app.bus.post(source)
        self.claim_for_delivery('trae', source)
        first = self.app.send("trae", "codex", "first result", type="result",
                              reply_to=source.id)
        first_id = first.split()[1]

        response = self.app.report("trae", source.id, "different report")

        self.assertIn("之前已经回投", response)
        self.assertFalse(self.app.bus.dirs("trae")["blocked"].joinpath(
            source.id + ".json").exists())
        self.assertTrue(self.app.bus.dirs("trae")["done"].joinpath(
            source.id + ".json").exists())
        linked = [self.app.bus.read(path) for path in self.app.bus.pending("codex")
                  if self.app.bus.read(path).type == "result"
                  and self.app.bus.read(path).reply_to == source.id]
        self.assertEqual([item.id for item in linked], [first_id])
        self.assertEqual(len(self.app.bus.pending("human")), 0)

    # This case exercises an active legacy migration window, regardless of the real date.
    @patch("agentbus.routing.LEGACY_BODY_COMPAT_UNTIL", float("inf"))
    def test_report_does_not_reuse_cited_result_with_wrong_legacy_recipient(self):
        source = self.app.bus.make("codex", "trae", "task", "legacy",
                                   "do work\n结果收件人：zcode")
        ready = self.app.bus.dirs("trae")["ready"] / f"{source.id}.json"
        ready.write_text(json.dumps(source.to_dict(), ensure_ascii=False),
                         encoding="utf-8")
        self.app.next_task("trae")
        # A historical orphan is an abnormal persisted record, not a new send.
        # Keep this recovery counterexample without recreating the old bug in
        # the public transport API.
        ticket = self.app._tickets[('trae', source.id)]
        posted = self.app.bus.make('trae', 'codex', 'result',
                                  body='result sent to codex', reply_to=source.id)
        posted.source_claim_epoch = ticket.claim_epoch
        posted.source_claim_id = ticket.claim_id
        self.app.bus.post(posted)
        result_id = posted.id

        response = self.app.report(
            "trae", source.id, f"Please reuse existing result {result_id}")

        self.assertIn("任务目标", response)
        self.assertTrue(self.app.bus.dirs("trae")["blocked"].joinpath(
            source.id + ".json").exists())
        self.assertFalse(self.app.bus.dirs("trae")["done"].joinpath(
            source.id + ".json").exists())
        linked = [self.app.bus.read(path) for path in self.app.bus.pending("codex")
                  if self.app.bus.read(path).type == "result"
                  and self.app.bus.read(path).reply_to == source.id]
        self.assertEqual([item.id for item in linked], [result_id])
        self.assertEqual(len(self.app.bus.pending("human")), 1)

    def test_send_result_rejects_missing_or_non_task_reply_to(self):
        with self.assertRaisesRegex(ValueError, "reply_to.*不存在"):
            self.app.send("trae", "codex", "done", type="result",
                          reply_to="missing-task")
        info = self.app.bus.make("codex", "trae", "info", body="not dispatch")
        self.app.bus.post(info)
        with self.assertRaisesRegex(ValueError, "reply_to.*task/goal/review"):
            self.app.send("trae", "codex", "done", type="result",
                          reply_to=info.id)
        self.assertFalse(self.app.bus.pending("codex"))

    # This case exercises an active legacy migration window, regardless of the real date.
    @patch("agentbus.routing.LEGACY_BODY_COMPAT_UNTIL", float("inf"))
    def test_manual_result_uses_and_audits_legacy_body_route(self):
        source = self.app.bus.make("codex", "trae", "task",
                                   body="work\n结果收件人：zcode")
        path = self.app.bus.dirs("trae")["ready"] / f"{source.id}.json"
        path.write_text(json.dumps(source.to_dict(), ensure_ascii=False),
                        encoding="utf-8")
        self.claim_for_delivery('trae', source)
        self.app.send("trae", "codex", "completed", type="result",
                      reply_to=source.id)
        result = self.app.bus.read(self.app.bus.pending("codex")[0])
        self.assertEqual(result.routed_by_fallback, "body")
        mismatch = [event for event in self.app.bus.events()
                    if event.get("event") == "route_mismatch"]
        self.assertEqual(len(mismatch), 1)
        self.assertEqual((mismatch[0]["declared"], mismatch[0]["actual"]),
                         ("zcode", "codex"))

    def test_manual_mcp_result_participates_in_peer_oversight(self):
        self.app.cfg.data["oversight"]["review_every_n"] = 1
        source = self.app.bus.make("codex", "trae", "task", "manual",
                                   "do work", result_to="codex")
        self.app.bus.post(source)
        self.claim_for_delivery('trae', source)
        self.app.send("trae", "codex", "completed", type="result",
                      reply_to=source.id)
        reviews = [self.app.bus.read(path)
                   for agent in self.app.cfg.enabled_workers()
                   for path in self.app.bus.pending(agent)
                   if self.app.bus.read(path).type == "review"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].reply_to, source.id)

    def test_send_requires_result_to_for_task_goal_review(self):
        for kind in ("task", "goal", "review"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "result_to"):
                    self.app.send("codex", "trae", "do it", type=kind)
                self.assertFalse(self.app.bus.pending("trae"))

    def test_send_rejects_unknown_sender_and_recipient(self):
        with self.assertRaisesRegex(ValueError, "sender"):
            self.app.send("ghost", "trae", "do it", type="task", result_to="codex")
        with self.assertRaisesRegex(ValueError, "recipient"):
            self.app.send("codex", "ghost", "do it", type="task", result_to="codex")
        self.assertFalse(self.app.bus.pending("trae"))
        self.assertFalse(self.app.bus.pending("ghost"))

    def test_dispatch_message_rejects_disabled_executor(self):
        self.app.cfg.data["workers"]["trae"]["executor_enabled"] = False
        with self.assertRaisesRegex(ValueError, "未启用执行"):
            self.app.send("codex", "trae", "do it", type="task",
                          result_to="codex")
        self.assertFalse(self.app.bus.pending("trae"))

    def test_goal_can_be_sent_to_non_executor_coordinator(self):
        self.app.cfg.data["director"] = "trae"
        self.app.cfg.data["workers"]["trae"]["executor_enabled"] = False
        response = self.app.send("codex", "director", "coordinate this goal",
                                 type="goal", result_to="human")
        goal = self.app.bus.find_message(response.split()[1])
        self.assertEqual((goal.to, goal.type, goal.result_to),
                         ("trae", "goal", "human"))

    def test_report_routes_result_to_original_target_with_new_id(self):
        task = self.app.bus.make("codex", "trae", "task", "task", "do it", result_to="zcode")
        self.app.bus.post(task)
        self.app.next_task("trae")
        with patch("agentbus.director.Director.schedule_peer_review", create=True) as review:
            response = self.app.report("trae", task.id, "done")
        review.assert_called_once()
        args = review.call_args.args
        self.assertEqual(args[0].id, task.id)
        self.assertEqual(args[1:], ("trae", "done"))
        self.assertIn("zcode", response)
        result = self.app.bus.find_message(task.id)
        self.assertIsNotNone(result)
        outbox = self.app.bus.pending("zcode")
        self.assertEqual(len(outbox), 1)
        posted = self.app.bus.read(outbox[0])
        self.assertNotEqual(posted.id, task.id)
        self.assertEqual(posted.reply_to, task.id)
        self.assertEqual(posted.to, "zcode")
        self.assertIn("agentbus_wait_task", response)
        self.assertEqual(self.app.bus.dirs("trae")["done"].joinpath(task.id + ".json").exists(), True)

    def _blocked_reuse_case(self):
        self.app.cfg.data["workers"]["codex"]["enabled"] = False
        task = self.app.bus.make(
            "codex", "trae", "task", "legacy",
            "此前工作已完成；结果投给 codex（to=codex）。")
        # Legacy messages may already exist in ready; claim them through the
        # real MCP path so report receives a valid current fencing ticket.
        task_path = self.app.bus.dirs("trae")["ready"] / f"{task.id}.json"
        task_path.write_text(json.dumps(task.to_dict(), ensure_ascii=False), encoding="utf-8")
        self.app.next_task("trae")
        ticket = self.app._tickets[("trae", task.id)]
        earlier = self.app.bus.make(
            "trae", "codex", "result", "早期回执", "另一条既有结果",
            reply_to=task.id, result_to="codex",
            source_claim_epoch=ticket.claim_epoch, source_claim_id=ticket.claim_id)
        self.app.bus.post(earlier)
        existing = self.app.bus.make(
            "trae", "codex", "result", "既有回执", "完整评审结果",
            reply_to=task.id, result_to="codex",
            source_claim_epoch=ticket.claim_epoch, source_claim_id=ticket.claim_id)
        self.app.bus.post(existing)
        report_text = f"此任务此前已经完成，请复用既有回执 {existing.id}。"
        claimed_path = self.app.bus.dirs("trae")["processing"] / f"{task.id}.json"
        claimed = self.app.bus.read(claimed_path)
        claimed.result = report_text
        self.app.bus.block("trae", claimed, "earlier route failure", ticket=ticket)
        escalation = self.app.bus.make(
            "workbuddy", "human", "escalation", "route clarification",
            "An earlier attempt was blocked.", reply_to=task.id)
        self.app.bus.post(escalation)
        return task, earlier, existing, report_text, escalation

    # This case exercises an active legacy migration window, regardless of the real date.
    @patch("agentbus.routing.LEGACY_BODY_COMPAT_UNTIL", float("inf"))
    def test_report_reuses_cited_result_among_multiple_linked_results(self):
        task, earlier, existing, report_text, escalation = self._blocked_reuse_case()

        response = self.app.report("trae", task.id, report_text)

        self.assertIn(existing.id, response)
        results = [self.app.bus.read(path) for path in self.app.bus.pending("codex")
                   if self.app.bus.read(path).type == "result"
                   and self.app.bus.read(path).reply_to == task.id]
        self.assertCountEqual([item.id for item in results], [earlier.id, existing.id])
        archived = self.app.bus.dirs("trae")["done"] / f"{task.id}.json"
        self.assertTrue(archived.exists())
        self.assertEqual(self.app.bus.read(archived).result, existing.body)
        self.assertEqual(self.app.bus.pending("human"), [])
        resolved = self.app.bus.dirs("human")["done"] / f"{escalation.id}.json"
        self.assertTrue(resolved.exists())
        self.assertTrue(any(event.get("event") == "escalation_resolved"
                            and event.get("source_task_id") == task.id
                            and event.get("result_id") == existing.id
                            for event in self.app.bus.events()))

    # This case exercises an active legacy migration window, regardless of the real date.
    @patch("agentbus.routing.LEGACY_BODY_COMPAT_UNTIL", float("inf"))
    def test_report_reuse_does_not_remove_human_claimed_escalation(self):
        task, _, existing, report_text, escalation = self._blocked_reuse_case()
        finish_if_ready = self.app.bus.finish_if_ready

        def claim_before_resolve(agent, message, status="done"):
            if agent == "human":
                source = self.app.bus.dirs("human")["ready"] / f"{escalation.id}.json"
                self.app.bus.claim("human", source)
            return finish_if_ready(agent, message, status)

        with patch.object(self.app.bus, "finish_if_ready", side_effect=claim_before_resolve):
            response = self.app.report("trae", task.id, report_text)

        self.assertIn(existing.id, response)
        processing = self.app.bus.dirs("human")["processing"] / f"{escalation.id}.json"
        done = self.app.bus.dirs("human")["done"] / f"{escalation.id}.json"
        self.assertTrue(processing.exists())
        self.assertFalse(done.exists())
        self.assertTrue(any(event.get("event") == "escalation_resolution_skipped"
                            and event.get("id") == escalation.id
                            for event in self.app.bus.events()))

    def test_repeated_unresolved_report_updates_blocked_result_without_duplicate_escalation(self):
        task = self.app.bus.make("codex", "trae", "task", "legacy", "no route")
        path = self.app.bus.dirs("trae")["ready"] / f"{task.id}.json"
        path.write_text(json.dumps(task.to_dict()), encoding="utf-8")
        self.app.next_task("trae")

        with patch("agentbus.director.Director.schedule_peer_review", create=True):
            self.app.report("trae", task.id, "first saved result")
            first_escalation = self.app.bus.read(self.app.bus.pending("human")[0])
            response = self.app.report("trae", task.id, "updated saved result")

        blocked = self.app.bus.dirs("trae")["blocked"] / f"{task.id}.json"
        self.assertIn(first_escalation.id, response)
        self.assertEqual(self.app.bus.read(blocked).result, "updated saved result")
        pending = self.app.bus.pending("human")
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.app.bus.read(pending[0]).id, first_escalation.id)
        self.assertEqual(sum(event.get("event") == "escalate"
                             and event.get("id") == task.id
                             for event in self.app.bus.events()), 1)

    def test_concurrent_reports_for_one_task_post_at_most_one_result(self):
        task = self.app.bus.make("codex", "trae", "task", "once", "do work",
                                 result_to="codex")
        self.app.bus.post(task)
        self.app.next_task("trae")
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []
        original_find = self.app.bus.find_results

        def pause_after_lock(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test synchronization timed out")
            return original_find(*args, **kwargs)

        def first_report():
            try:
                results.append(self.app.report("trae", task.id, "completed"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with patch.object(self.app.bus, "find_results", side_effect=pause_after_lock):
            worker = threading.Thread(target=first_report)
            worker.start()
            self.assertTrue(entered.wait(5), "first report did not acquire its guard")
            second = self.app.report("trae", task.id, "completed")
            release.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIn("其他调用处理", second)
        posted = [self.app.bus.read(path) for path in self.app.bus.pending("codex")
                  if self.app.bus.read(path).type == "result"
                  and self.app.bus.read(path).reply_to == task.id]
        self.assertEqual(len(posted), 1)

    # This case exercises an active legacy migration window, regardless of the real date.
    @patch("agentbus.routing.LEGACY_BODY_COMPAT_UNTIL", float("inf"))
    def test_report_legacy_body_route_is_marked(self):
        task = self.app.bus.make("codex", "trae", "task", "legacy", "结果收件人: zcode")
        # Simulate a v1/legacy message already in the bus: new publishing rejects it.
        self.app.bus.dirs("trae")["ready"].joinpath(task.id + ".json").write_text(
            json.dumps(task.to_dict()), encoding="utf-8")
        self.app.next_task("trae")
        with patch("agentbus.director.Director.schedule_peer_review", create=True):
            self.app.report("trae", task.id, "done")
        path = self.app.bus.pending("zcode")[0]
        result = self.app.bus.read(path)
        self.assertEqual(result.result_to, "zcode")
        self.assertEqual(result.routed_by_fallback, "body")

    # This case exercises an active legacy migration window, regardless of the real date.
    @patch("agentbus.routing.LEGACY_BODY_COMPAT_UNTIL", float("inf"))
    def test_report_legacy_parenthetical_to_route_is_marked(self):
        task = self.app.bus.make(
            "codex", "trae", "task", "legacy",
            "请通过 AgentBus 发一条自足 result 给 codex（to=codex）。")
        path = self.app.bus.dirs("trae")["ready"] / f"{task.id}.json"
        path.write_text(json.dumps(task.to_dict(), ensure_ascii=False), encoding="utf-8")
        self.app.next_task("trae")
        with patch("agentbus.director.Director.schedule_peer_review", create=True):
            reply = self.app.report("trae", task.id, "复用既有回执")

        self.assertIn("codex", reply)
        outbox = self.app.bus.pending("codex")
        self.assertEqual(len(outbox), 1)
        result = self.app.bus.read(outbox[0])
        self.assertEqual(result.to, "codex")
        self.assertEqual(result.reply_to, task.id)
        self.assertEqual(result.routed_by_fallback, "body")

    def test_ambiguous_legacy_route_blocks_and_escalates_without_requeue(self):
        task = self.app.bus.make("codex", "trae", "task", "legacy", "no route")
        source = self.app.bus.dirs("trae")["ready"] / f"{task.id}.json"
        source.write_text(json.dumps(task.to_dict()), encoding="utf-8")
        self.app.next_task("trae")
        reply = self.app.report("trae", task.id, "saved result")
        blocked = self.app.bus.dirs("trae")["blocked"] / f"{task.id}.json"
        self.assertIn("升级", reply)
        self.assertTrue(blocked.exists())
        self.assertEqual(self.app.bus.pending("trae"), [])
        self.assertEqual(self.app.bus.read(blocked).result, "saved result")
        self.assertFalse(self.app.bus.dirs("trae")["done"].joinpath(task.id + ".json").exists())
        escalation_paths = self.app.bus.pending("human")
        self.assertEqual(len(escalation_paths), 1)
        escalation = self.app.bus.read(escalation_paths[0])
        self.assertEqual(escalation.type, "escalation")
        self.assertEqual(escalation.reply_to, task.id)
        self.assertIn("saved result", escalation.body)
        self.assertEqual(sum(e.get("event") == "escalate"
                             for e in self.app.bus.events()), 1)

    def test_conflicting_legacy_body_route_blocks_and_escalates(self):
        task = self.app.bus.make("codex", "trae", "task", "legacy", "结果收件人: zcode\n回执投给: workbuddy")
        self.app.bus.dirs("trae")["ready"].joinpath(task.id + ".json").write_text(
            json.dumps(task.to_dict()), encoding="utf-8")
        self.app.next_task("trae")
        reply = self.app.report("trae", task.id, "done")
        self.assertIn("升级", reply)
        self.assertTrue(self.app.bus.dirs("trae")["blocked"].joinpath(task.id + ".json").exists())
        self.assertEqual(self.app.bus.pending("trae"), [])
        self.assertEqual(len(self.app.bus.pending("human")), 1)


if __name__ == "__main__":
    unittest.main()
