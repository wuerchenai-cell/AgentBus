"""
Automated Regression Tests for §5.9 FIVE-HOST-FILE-HANDOFF
Source: Antigravity (2026-10-12)

Verifies:
1. Five hosts (Codex, WorkBuddy, Trae, ZCode, OpenCode) + Generic adapter paths.
2. Full onboarding pack generation: leader instructions, newcomer instructions, sample task.
3. End-to-end file collaboration lifecycle: claim -> file execution -> artifacts manifest -> answer -> verify.
4. UTF-8 file integrity round-trip (preventing pipeline question-mark replacement).
5. Path isolation & safety boundary enforcement (forbidden private paths like .workbuddy, forbidden traversals).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.host_skill_adapters import (
    HOST_ADAPTERS,
    GENERIC_ADAPTER,
    SCOPE_PROJECT,
    list_hosts,
    resolve_target,
)
from agentbus.member_adapter import MemberAdapter
from agentbus.onboarding_pack import (
    build_initial_capability_card,
    generate_onboarding_pack,
    deploy_onboarding_pack,
    export_reusable_evidence,
    import_card_to_second_project,
)
from agentbus.profiles import add_profile
from agentbus.workspace_files import WorkspaceFiles


class TestFiveHostFileHandoff(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.cfg = load(self.workspace, create=True)
        # Enable codex as coordinator
        for name, worker in self.cfg.data['workers'].items():
            worker['enabled'] = (name == 'codex')
            worker['executor_enabled'] = (name == 'codex')
        save(self.cfg)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_five_hosts_adapter_registry(self):
        """Verify registered adapters exist for the 5 hosts + generic."""
        hosts = list_hosts()
        expected_five = ('codex', 'workbuddy', 'trae', 'zcode', 'opencode')
        for h in expected_five:
            self.assertIn(h, hosts, f"Host {h} must be registered in host_skill_adapters")
            rel = HOST_ADAPTERS[h]["project_relative"]
            self.assertTrue(rel, f"Host {h} must define project_relative")
            res = resolve_target(h, skill_name="agentbus-team", scope=SCOPE_PROJECT, project_root=self.workspace)
            self.assertIn(rel, res["target_dir"].replace("\\", "/"))

        # Check generic adapter resolution for unknown hosts
        res_generic = resolve_target("unknown_custom_agent", skill_name="agentbus-team", scope=SCOPE_PROJECT, project_root=self.workspace)
        self.assertTrue(res_generic["used_generic"])
        self.assertIn(".agentbus/skills", res_generic["target_dir"].replace("\\", "/"))

    def test_onboarding_pack_generated_for_each_host(self):
        """Verify onboarding pack instructions generated cleanly for all 5 hosts."""
        hosts = ('codex', 'workbuddy', 'trae', 'zcode', 'opencode')
        for h in hosts:
            if h not in self.cfg.data['workers']:
                add_profile(self.cfg, h, h.capitalize(), kind='gui')
            pack = generate_onboarding_pack(self.workspace, h, coordinator='codex')
            self.assertIn('leader_instructions', pack)
            self.assertIn('newcomer_instructions', pack)
            self.assertIn('sample_task', pack)
            self.assertIn(h, pack['newcomer_instructions'])
            self.assertIn('agentbus claim', pack['newcomer_instructions'])
            self.assertIn('agentbus answer', pack['newcomer_instructions'])

    def test_end_to_end_file_handoff_lifecycle(self):
        """Simulate an end-to-end file handoff cycle for a newcomer host."""
        member = "workbuddy"
        if member not in self.cfg.data['workers']:
            add_profile(self.cfg, member, "WorkBuddy", kind='gui')

        # 1. Coordinator deploys onboarding pack
        pack = deploy_onboarding_pack(self.workspace, member, coordinator="codex")
        task_id = pack["task_id"]
        inbox_file = self.workspace / pack["inbox_path"]
        self.assertTrue(inbox_file.is_file(), "Task JSON must be deployed in ready inbox")

        # 2. Member claims task via Bus
        claimed = self.bus.claim(member, inbox_file)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, task_id)
        ticket = captured_ticket(claimed)
        self.bus.heartbeat(member, claimed, ticket, note="test host relay started")

        # 3. Member reads input, writes output
        input_file = self.workspace / pack["input_path"]
        self.assertTrue(input_file.is_file(), "Input file must exist")
        input_content = input_file.read_text(encoding="utf-8").strip()

        output_file = self.workspace / pack["output_path"]
        output_file.parent.mkdir(parents=True, exist_ok=True)
        first_line = input_content.splitlines()[0]
        output_file.write_text(first_line, encoding="utf-8")

        # 4. Member creates artifacts manifest
        ws_files = WorkspaceFiles(self.workspace)
        rel_output = str(output_file.relative_to(self.workspace)).replace("\\", "/")
        art_desc = ws_files.describe_artifact(rel_output)
        manifest_file = self.workspace / pack["manifest_path"]
        manifest_data = {
            "task_id": task_id,
            "agent": member,
            "artifacts": [art_desc],
        }
        manifest_file.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 5. Member finishes task and posts result (commit_result)
        receipt = self.bus.commit_result(
            member, claimed, ticket,
            body="已按规范生成产物并核验 UTF-8 清单",
            artifacts=[art_desc],
            exit_code=0
        )
        self.assertIsNotNone(receipt)
        self.assertTrue((self.workspace / f".agentbus/agents/{member}/done/{task_id}.json").is_file())
        self.assertTrue((self.workspace / f".agentbus/agents/codex/ready/{receipt.id}.json").is_file())

        # 6. Coordinator verifies output artifact
        result_desc = ws_files.describe_artifact(rel_output)
        self.assertEqual(result_desc["sha256"], art_desc["sha256"])
        self.assertEqual(result_desc["bytes"], art_desc["bytes"])

        # 7. Export reusable capability card
        adapter = MemberAdapter(member, "WorkBuddy")
        card = build_initial_capability_card(adapter, "project-test", {
            "task_id": task_id,
            "result_id": receipt.id,
            "verified": True,
            "artifact_sha256": art_desc["sha256"]
        })
        exported = export_reusable_evidence(card)
        self.assertEqual(exported["member_id"], member)
        imported = import_card_to_second_project(exported, "clean-target")
        self.assertEqual(imported["member_id"], member)

    def test_utf8_pipeline_safety_roundtrip(self):
        """Ensure UTF-8 multibyte characters (Chinese / emojis) survive without corruption."""
        sample_text = "测试中文回执：跨平台多 Agent 协作，保证字节完全一致！🚀✨"
        file_path = self.workspace / "成果/测试/utf8_test.txt"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(sample_text, encoding="utf-8")

        raw_bytes = file_path.read_bytes()
        calculated_sha = hashlib.sha256(raw_bytes).hexdigest()

        read_back = file_path.read_text(encoding="utf-8")
        self.assertEqual(read_back, sample_text)
        self.assertNotIn("?", read_back)
        self.assertEqual(hashlib.sha256(read_back.encode("utf-8")).hexdigest(), calculated_sha)

    def test_sandbox_path_traversal_forbidden(self):
        """Ensure boundary prevents traversal outside workspace or into private host folders."""
        ws_files = WorkspaceFiles(self.workspace)
        with self.assertRaises(Exception):
            ws_files.describe_artifact("../outside.txt")
        with self.assertRaises(Exception):
            ws_files.describe_artifact(".workbuddy/secrets.json")
        with self.assertRaises(Exception):
            ws_files.describe_artifact(".agentbus/config.json")


if __name__ == "__main__":
    unittest.main()
