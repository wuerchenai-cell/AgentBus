from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.cli import (cmd_answer, cmd_claim, cmd_handoff, cmd_heartbeat, cmd_inbox,
                           cmd_post, main as cli_main)
from agentbus.config import ConfigError, load


class ClaimAndInboxCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.cfg = load(self.ws, create=True)
        self.bus = Bus(self.ws / ".agentbus")
        self.bus.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, title):
        message = self.bus.make("codex", "workbuddy", "task", title=title,
                                body=title, result_to="codex")
        self.bus.post(message)
        return message

    def test_claim_by_id_moves_one_message_atomically_to_processing(self):
        task = self._post("指定认领")

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_claim(SimpleNamespace(workspace=str(self.ws),
                                              agent="workbuddy", id=task.id))

        self.assertEqual(code, 0, output.getvalue())
        self.assertIn(task.id, output.getvalue())
        self.assertEqual(self.bus.pending("workbuddy"), [])
        claimed = self.bus.read(self.bus.dirs("workbuddy")["processing"] /
                                f"{task.id}.json")
        self.assertEqual((claimed.status, claimed.claimed_by), ("claimed", "workbuddy"))

    def test_claim_without_id_takes_first_ready_message_only(self):
        first = self._post("第一条")
        second = self._post("第二条")
        first_path, second_path = self.bus.pending("workbuddy")

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_claim(SimpleNamespace(workspace=str(self.ws),
                                              agent="workbuddy", id=None))

        self.assertEqual(code, 0, output.getvalue())
        self.assertIn(first_path.stem, output.getvalue())
        self.assertEqual(self.bus.pending("workbuddy"), [second_path])
        self.assertTrue((self.bus.dirs("workbuddy")["processing"] /
                         first_path.name).exists())

    def test_claim_with_no_ready_message_reports_empty_status(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_claim(SimpleNamespace(workspace=str(self.ws),
                                              agent="workbuddy", id=None))

        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("没有待处理消息可认领", output.getvalue())

    def test_claim_rejects_unregistered_member(self):
        with self.assertRaises(ConfigError):
            cmd_claim(SimpleNamespace(workspace=str(self.ws),
                                      agent="not-registered", id=None))

    def test_answer_works_after_claim_moved_task_to_processing(self):
        """Source: Codex dual-directory verification, 2026-10-07.

        公共契约是"先 claim 再 answer"。claim 把任务移进 processing/，
        但 answer 以前只在 ready/ 和 blocked/ 找源文件，导致认领后
        永远无法回执，文件路线在真实新项目上直接断掉。
        """
        task = self._post("先认领再回执")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            claim_code = cmd_claim(SimpleNamespace(workspace=str(self.ws),
                                                   agent="workbuddy", id=task.id))
        self.assertEqual(claim_code, 0, output.getvalue())
        self.assertTrue((self.bus.dirs("workbuddy")["processing"] /
                         f"{task.id}.json").exists())

        claim_lines = [l for l in output.getvalue().splitlines() if l.startswith('{')]
        claim_ticket = json.loads(claim_lines[0]) if claim_lines else {}
        heartbeat_output = io.StringIO()
        with contextlib.redirect_stdout(heartbeat_output), contextlib.redirect_stderr(heartbeat_output):
            heartbeat_code = cmd_heartbeat(SimpleNamespace(
                workspace=str(self.ws), agent="workbuddy", id=task.id,
                claim_epoch=claim_ticket.get('claim_epoch'),
                claim_id=claim_ticket.get('claim_id'), note="active file workflow"))
        self.assertEqual(heartbeat_code, 0, heartbeat_output.getvalue())
        answer_output = io.StringIO()
        with contextlib.redirect_stdout(answer_output), contextlib.redirect_stderr(answer_output):
            answer_code = cmd_answer(SimpleNamespace(
                workspace=str(self.ws), agent="workbuddy", id=task.id,
                claim_epoch=claim_ticket.get('claim_epoch'), claim_id=claim_ticket.get('claim_id'),
                body="认领后回执正文", body_file=None, expected_body_sha256=None,
                artifacts_file=None, failed=False, result_to=None))

        self.assertEqual(answer_code, 0, answer_output.getvalue())
        self.assertIn("已回投结果给 codex", answer_output.getvalue())
        self.assertEqual(self.bus.read(self.bus.pending("codex")[0]).body,
                         "认领后回执正文")
        self.assertFalse((self.bus.dirs("workbuddy")["processing"] /
                          f"{task.id}.json").exists())
        self.assertTrue((self.bus.dirs("workbuddy")["done"] /
                         f"{task.id}.json").exists())

    def test_inbox_mine_caller_matches_explicit_agent(self):
        task = self._post("我的待办")
        explicit = io.StringIO()
        with contextlib.redirect_stdout(explicit):
            explicit_code = cmd_inbox(SimpleNamespace(workspace=str(self.ws),
                                                      agent="workbuddy", mine=False,
                                                      caller=None, body=True))
        mine = io.StringIO()
        with contextlib.redirect_stdout(mine):
            mine_code = cmd_inbox(SimpleNamespace(workspace=str(self.ws),
                                                  agent=None, mine=True,
                                                  caller="workbuddy", body=True))

        self.assertEqual(explicit_code, mine_code)
        self.assertEqual(explicit.getvalue(), mine.getvalue())
        self.assertIn(task.id, mine.getvalue())

    def test_main_exposes_claim_and_inbox_mine_commands(self):
        task = self._post("入口命令")
        inbox_output = io.StringIO()
        with contextlib.redirect_stdout(inbox_output):
            inbox_code = cli_main(["inbox", "--workspace", str(self.ws),
                                   "--mine", "--caller", "workbuddy"])
        self.assertEqual(inbox_code, 0, inbox_output.getvalue())
        self.assertIn(task.id, inbox_output.getvalue())

        claim_output = io.StringIO()
        with contextlib.redirect_stdout(claim_output):
            claim_code = cli_main(["claim", "--workspace", str(self.ws),
                                   "--agent", "workbuddy", "--id", task.id])
        self.assertEqual(claim_code, 0, claim_output.getvalue())
        self.assertIn(task.id, claim_output.getvalue())


class AnswerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.cfg = load(self.ws, create=True)
        self.bus = Bus(self.ws / ".agentbus")
        self.bus.init()
        self.claim_tickets = {}

    def tearDown(self):
        self.tmp.cleanup()

    def run_answer(self, task, body="完成", result_to=None, failed=False):
        ticket = self.claim_tickets.get(task.id)
        if ticket is None:
            claim_output = io.StringIO()
            with contextlib.redirect_stdout(claim_output), contextlib.redirect_stderr(claim_output):
                claim_code = cmd_claim(SimpleNamespace(
                    workspace=str(self.ws), agent=task.to, id=task.id))
            self.assertEqual(claim_code, 0, claim_output.getvalue())
            claim_lines = [line for line in claim_output.getvalue().splitlines()
                           if line.startswith('{')]
            self.assertTrue(claim_lines, claim_output.getvalue())
            ticket = json.loads(claim_lines[0])
            self.claim_tickets[task.id] = ticket

            heartbeat_output = io.StringIO()
            with contextlib.redirect_stdout(heartbeat_output), contextlib.redirect_stderr(heartbeat_output):
                heartbeat_code = cmd_heartbeat(SimpleNamespace(
                    workspace=str(self.ws), agent=task.to, id=task.id,
                    claim_epoch=ticket['claim_epoch'], claim_id=ticket['claim_id'],
                    note="active answer test"))
            self.assertEqual(heartbeat_code, 0, heartbeat_output.getvalue())
        args = SimpleNamespace(workspace=str(self.ws), agent=task.to,
                               id=task.id, body=body, result_to=result_to,
                               failed=failed,
                               claim_epoch=ticket['claim_epoch'],
                               claim_id=ticket['claim_id'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_answer(args)
        return code, output.getvalue()

    def write_legacy(self, task):
        path = self.bus.dirs(task.to)["ready"] / f"{task.id}.json"
        path.write_text(json.dumps(task.to_dict(), ensure_ascii=False),
                        encoding="utf-8")

    def claim_for_direct_result(self, task):
        claimed = self.bus.claim(task.to, self.bus.dirs(task.to)['ready'] / f'{task.id}.json')
        ticket = captured_ticket(claimed)
        self.bus.heartbeat(task.to, claimed, ticket, note='routing test executor progress')
        return {'claim_epoch': ticket.claim_epoch, 'claim_id': ticket.claim_id}

    def test_answer_routes_to_declared_recipient_with_new_id(self):
        task = self.bus.make("trae", "workbuddy", "task", body="任务",
                             result_to="codex")
        self.bus.post(task)
        code, output = self.run_answer(task)
        self.assertEqual(code, 0, output)
        results = [self.bus.read(p) for p in self.bus.pending("codex")]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].type, "result")
        self.assertNotEqual(results[0].id, task.id)
        self.assertEqual(results[0].reply_to, task.id)
        self.assertEqual(results[0].to, task.result_to)
        self.assertEqual(len(list(self.bus.dirs("workbuddy")["done"].glob("*.json"))), 1)

    def test_manual_failure_is_not_reported_or_archived_as_success(self):
        task = self.bus.make("codex", "workbuddy", "task", body="执行受限任务",
                             result_to="codex")
        self.bus.post(task)
        code, output = self.run_answer(task, body="WorkBuddy 实际回复：权限拒绝，未完成",
                                       failed=True)
        self.assertEqual(code, 0, output)  # delivery succeeded, execution failed
        result = next(self.bus.read(p) for p in self.bus.pending("codex")
                      if self.bus.read(p).reply_to == task.id)
        self.assertEqual(result.exit_code, 1)
        archived = self.bus.read(self.bus.dirs("workbuddy")["done"] / f"{task.id}.json")
        self.assertEqual((archived.status, archived.exit_code), ("failed", 1))
        self.assertTrue(any(self.bus.read(p).type == "review"
                            for agent in self.cfg.enabled_workers()
                            for p in self.bus.pending(agent)))

    def test_manual_success_receipt_carries_explicit_outcome(self):
        task = self.bus.make("codex", "workbuddy", "review", body="只读评审",
                             result_to="codex")
        self.bus.post(task)
        code, output = self.run_answer(task)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.bus.read(self.bus.pending("codex")[0]).exit_code, 0)

    def test_failed_manual_answer_preserves_existing_human_escalation(self):
        from agentbus.director import Director
        task = self.bus.make("codex", "workbuddy", "task", body="仍需用户裁决",
                             result_to="codex")
        self.bus.post(task)
        escalation = Director(self.cfg, bus=self.bus).escalate_for_human(task, "权限未批准")
        code, output = self.run_answer(task, body="未获权限，任务失败", failed=True)
        self.assertEqual(code, 0, output)
        self.assertTrue(any(self.bus.read(p).id == escalation.id
                            for p in self.bus.pending("human")))
        self.assertFalse(any(e.get("event") == "escalation_resolved"
                             and e.get("source_task_id") == task.id
                             for e in self.bus.events()))

    def test_frozen_handoff_command_uses_exe_and_protects_mailbox_files(self):
        task = self.bus.make("codex", "workbuddy", "task", body="只创建一个测试产物",
                             result_to="codex")
        self.bus.post(task)
        args = SimpleNamespace(workspace=str(self.ws), agent="workbuddy")
        output = io.StringIO()
        exe = r"C:\Program Files\AgentBus\AgentBus.exe"

        with (patch.object(sys, "executable", exe),
              patch.object(sys, "frozen", True, create=True),
              contextlib.redirect_stdout(output)):
            code = cmd_handoff(args)

        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertIn('"C:\\Program Files\\AgentBus\\AgentBus.exe" answer', rendered)
        self.assertNotIn("python -m agentbus answer", rendered)
        self.assertIn("不要直接编辑或移动 .agentbus 消息文件", rendered)

    def test_workbuddy_and_trae_handoffs_include_route_and_receipt_command(self):
        tasks = {}
        for agent in ("workbuddy", "trae"):
            task = self.bus.make("codex", agent, "task", body=f"给 {agent} 的任务",
                                 result_to="codex")
            self.bus.post(task)
            tasks[agent] = task

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cmd_handoff(SimpleNamespace(workspace=str(self.ws), agent=None))

        self.assertEqual(code, 0)
        rendered = output.getvalue()
        for agent, task in tasks.items():
            self.assertIn(f"给 {agent} 的任务  [{task.id}]", rendered)
            self.assertIn("结果收件人：codex", rendered)
            self.assertIn(f"--agent {agent}", rendered)
            self.assertIn(f"--id {task.id}", rendered)
        self.assertIn("不要直接编辑或移动 .agentbus 消息文件", rendered)

    def test_trae_manual_answer_closes_task_and_routes_result_to_codex(self):
        task = self.bus.make("codex", "trae", "task", body="检查部署风险",
                             result_to="codex")
        self.bus.post(task)
        code, output = self.run_answer(task, body="Trae 报告：发现一个风险")

        self.assertEqual(code, 0, output)
        result = next(self.bus.read(path) for path in self.bus.pending("codex")
                      if self.bus.read(path).type == "result"
                      and self.bus.read(path).reply_to == task.id)
        self.assertEqual(result.to, "codex")
        self.assertEqual(result.body, "Trae 报告：发现一个风险")
        self.assertTrue((self.bus.dirs("trae")["done"] / f"{task.id}.json").exists())

    def test_ambiguous_legacy_answer_blocks_and_retries_with_explicit_route(self):
        task = self.bus.make("trae", "workbuddy", "task", body="没有写结果收件人")
        self.write_legacy(task)
        code, output = self.run_answer(task)
        self.assertEqual(code, 1)
        self.assertIn("blocked/", output)
        blocked = self.bus.dirs("workbuddy")["blocked"] / f"{task.id}.json"
        self.assertTrue(blocked.exists())
        self.assertEqual(self.bus.read(blocked).result, "完成")
        self.assertEqual(self.bus.pending("workbuddy"), [])
        self.assertEqual(len(self.bus.pending("human")), 1)
        self.assertFalse(any(self.bus.read(path).type == "result"
                             and self.bus.read(path).reply_to == task.id
                             for path in self.bus.pending("codex")))

        code, output = self.run_answer(task, result_to="codex")
        self.assertEqual(code, 0, output)
        result = next(self.bus.read(path) for path in self.bus.pending("codex")
                      if self.bus.read(path).type == "result"
                      and self.bus.read(path).reply_to == task.id)
        self.assertEqual((result.to, result.result_to, result.reply_to),
                         ("codex", "codex", task.id))
        self.assertEqual(self.bus.pending("human"), [])
        self.assertTrue(any(e.get("event") == "escalation_resolved"
                            and e.get("source_task_id") == task.id
                            for e in self.bus.events()))

    @patch("agentbus.routing.LEGACY_BODY_COMPAT_UNTIL", float("inf"))
    def test_body_compatibility_is_audited(self):
        task = self.bus.make("trae", "workbuddy", "task",
                             body="任务\n结果收件人：trae")
        self.write_legacy(task)
        code, output = self.run_answer(task)
        self.assertEqual(code, 0, output)
        result = self.bus.read(self.bus.pending("trae")[0])
        self.assertEqual(result.routed_by_fallback, "body")
        self.assertEqual(result.reply_to, task.id)

    def test_cli_task_requires_explicit_route_and_resolves_director_alias(self):
        base = dict(workspace=str(self.ws), sender="trae", to="workbuddy",
                    type="task", intent="做一件事", title=None, refs=None,
                    hop=0, reply_to=None)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            missing = cmd_post(SimpleNamespace(**base, result_to=None))
        self.assertEqual(missing, 2)
        self.assertEqual(self.bus.pending("workbuddy"), [])

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_post(SimpleNamespace(**base, result_to="director"))
        self.assertEqual(code, 0, output.getvalue())
        task = self.bus.read(self.bus.pending("workbuddy")[0])
        self.assertEqual(task.result_to, "codex")
        self.assertNotEqual(task.result_to, "director")

    def test_cli_rejects_unknown_sender_and_recipient(self):
        base = dict(workspace=str(self.ws), sender="ghost", to="workbuddy",
                    type="task", intent="做一件事", title=None, refs=None,
                    hop=0, reply_to=None, result_to="codex")
        for changes in ({}, {"sender": "codex", "to": "ghost"}):
            with self.subTest(changes=changes):
                output = io.StringIO()
                args = SimpleNamespace(**(base | changes))
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = cmd_post(args)
                self.assertEqual(code, 2, output.getvalue())
        self.assertEqual(self.bus.pending("workbuddy"), [])
        self.assertEqual(self.bus.pending("ghost"), [])

    def test_manual_cli_result_participates_in_peer_oversight(self):
        data = json.loads(self.cfg.path.read_text(encoding="utf-8"))
        data["oversight"]["review_every_n"] = 1
        self.cfg.path.write_text(json.dumps(data), encoding="utf-8")
        self.cfg = load(self.ws)
        source = self.bus.make("codex", "trae", "task", body="do work",
                               result_to="codex")
        self.bus.post(source)
        args = SimpleNamespace(
            workspace=str(self.ws), sender="trae", to="codex", type="result",
            intent="completed", title=None, refs=None, hop=0,
            reply_to=source.id, result_to=None, **self.claim_for_direct_result(source))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_post(args)
        self.assertEqual(code, 0, output.getvalue())
        reviews = [self.bus.read(path) for agent in self.cfg.enabled_workers()
                   for path in self.bus.pending(agent)
                   if self.bus.read(path).type == "review"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].reply_to, source.id)

    def test_manual_cli_result_rejects_missing_or_non_task_reply_to(self):
        base = dict(
            workspace=str(self.ws), sender="trae", to="codex", type="result",
            intent="completed", title=None, refs=None, hop=0, result_to=None)
        for reply_to in ("missing-task",):
            output = io.StringIO()
            args = SimpleNamespace(**(base | {"reply_to": reply_to}))
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = cmd_post(args)
            self.assertEqual(code, 2, output.getvalue())
            self.assertEqual(self.bus.pending("codex"), [])

        info = self.bus.make("codex", "trae", "info", body="not dispatch")
        self.bus.post(info)
        output = io.StringIO()
        args = SimpleNamespace(**(base | {"reply_to": info.id}))
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_post(args)
        self.assertEqual(code, 2, output.getvalue())
        self.assertEqual(len(self.bus.pending("codex")), 0)

    def test_cli_dispatch_rejects_disabled_executor(self):
        data = json.loads(self.cfg.path.read_text(encoding="utf-8"))
        data["workers"]["trae"]["executor_enabled"] = False
        self.cfg.path.write_text(json.dumps(data), encoding="utf-8")
        args = SimpleNamespace(
            workspace=str(self.ws), sender="codex", to="trae", type="task",
            intent="do it", title=None, refs=None, hop=0, reply_to=None,
            result_to="codex")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_post(args)
        self.assertEqual(code, 2, output.getvalue())
        self.assertEqual(self.bus.pending("trae"), [])

    def test_cli_can_send_goal_to_non_executor_coordinator(self):
        data = json.loads(self.cfg.path.read_text(encoding="utf-8"))
        data["director"] = "trae"
        data["workers"]["trae"]["executor_enabled"] = False
        self.cfg.path.write_text(json.dumps(data), encoding="utf-8")
        args = SimpleNamespace(
            workspace=str(self.ws), sender="codex", to="director", type="goal",
            intent="coordinate this goal", title=None, refs=None, hop=0,
            reply_to=None, result_to="human")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_post(args)
        self.assertEqual(code, 0, output.getvalue())
        goal = self.bus.read(self.bus.pending("trae")[0])
        self.assertEqual((goal.type, goal.result_to), ("goal", "human"))

    @patch("agentbus.routing.LEGACY_BODY_COMPAT_UNTIL", float("inf"))
    def test_manual_result_uses_and_audits_legacy_body_route(self):
        source = self.bus.make("codex", "trae", "task", body="work\n结果收件人：zcode")
        self.write_legacy(source)
        args = SimpleNamespace(
            workspace=str(self.ws), sender="trae", to="codex", type="result",
            intent="completed", title=None, refs=None, hop=0,
            reply_to=source.id, result_to=None, **self.claim_for_direct_result(source))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_post(args)
        self.assertEqual(code, 0, output.getvalue())
        result = self.bus.read(self.bus.pending("codex")[0])
        self.assertEqual(result.routed_by_fallback, "body")
        mismatch = [event for event in self.bus.events()
                    if event.get("event") == "route_mismatch"]
        self.assertEqual(len(mismatch), 1)
        self.assertEqual((mismatch[0]["declared"], mismatch[0]["actual"]),
                         ("zcode", "codex"))

    def test_cli_post_result_reuses_exact_duplicate_and_rejects_second_payload(self):
        source = self.bus.make("codex", "trae", "task", body="do work",
                               result_to="codex")
        self.bus.post(source)
        base = dict(
            workspace=str(self.ws), sender="trae", to="codex", type="result",
            intent="completed", title=None, refs=None, hop=0,
            reply_to=source.id, result_to=None, **self.claim_for_direct_result(source))
        outputs = []
        for _ in range(2):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = cmd_post(SimpleNamespace(**base))
            self.assertEqual(code, 0, output.getvalue())
            outputs.append(output.getvalue())
        self.assertIn("已复用既有回执", outputs[1])

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cmd_post(SimpleNamespace(**(base | {"intent": "different result"})))
        self.assertEqual(code, 2, output.getvalue())
        linked = [self.bus.read(path) for path in self.bus.pending("codex")
                  if self.bus.read(path).type == "result"
                  and self.bus.read(path).reply_to == source.id]
        self.assertEqual(len(linked), 1)

    def test_answer_blocks_when_historical_orphan_has_a_different_result(self):
        source = self.bus.make("codex", "trae", "task", body="do work",
                               result_to="codex")
        self.bus.post(source)
        historical = self.bus.make('trae', 'codex', 'result', body='first result',
                                   reply_to=source.id, result_to='codex')
        self.bus.post(historical)
        first_id = historical.id

        code, output_text = self.run_answer(source, body="different answer")

        self.assertEqual(code, 1, output_text)
        self.assertIn("已有不同回执", output_text)
        self.assertTrue((self.bus.dirs("trae")["blocked"] /
                         f"{source.id}.json").exists())
        linked = [self.bus.read(path) for path in self.bus.pending("codex")
                  if self.bus.read(path).type == "result"
                  and self.bus.read(path).reply_to == source.id]
        self.assertEqual([item.id for item in linked], [first_id])
        self.assertEqual(len(self.bus.pending("human")), 1)


if __name__ == "__main__":
    unittest.main()
