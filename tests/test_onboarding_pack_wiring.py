"""
Tests for ONBOARDING-PACK-WIRING (P1, §4.4)
Source: Antigravity (2026-10-12)

Verifies:
1. Public CLI entry (`agentbus member pack --workspace ... --member ...`) produces leader instructions and newcomer instructions.
2. The generated newcomer instructions contain: project workspace, member ID, file task entry point, and execution boundary.
3. Deploying onboarding pack creates input file and posts sample task to ready inbox.
4. WebBridge API (`member_pack` and `onboarding_capsule`) exposes instructions and pack structure.
5. End-to-end file collaboration relay test: claim task, verify boundaries, write output file, generate artifacts manifest, commit result, verify receipt, and create reusable public capability card.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.onboarding_pack import (
    build_initial_capability_card,
    deploy_onboarding_pack,
    export_reusable_evidence,
    generate_onboarding_pack,
    import_card_to_second_project,
)
from agentbus.profiles import add_profile
from agentbus.web_bridge import WebBridge
from agentbus.workspace_files import WorkspaceFiles


class TestOnboardingPackWiring(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.cfg = load(self.workspace, create=True)
        # 默认只启用 codex 作为协调者，其他预置成员初始为 enabled=False
        for name, worker in self.cfg.data['workers'].items():
            worker['enabled'] = name == 'codex'
            worker['executor_enabled'] = name == 'codex'
        save(self.cfg)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_generate_onboarding_pack_structure_and_boundaries(self):
        # Register a new generic member
        add_profile(self.cfg, 'trae', 'ByteDance Trae', kind='gui')
        pack = generate_onboarding_pack(self.workspace, 'trae', coordinator='codex')

        self.assertIn('leader_instructions', pack)
        self.assertIn('newcomer_instructions', pack)
        self.assertIn('sample_task', pack)
        self.assertEqual(pack['member_id'], 'trae')
        self.assertEqual(pack['workspace'], str(self.workspace).replace('\\', '/'))

        # Check newcomer instructions contain workspace, member id, claim command and boundaries
        newcomer = pack['newcomer_instructions']
        self.assertIn(str(self.workspace).replace('\\', '/'), newcomer)
        self.assertIn('trae', newcomer)
        self.assertIn('agentbus claim', newcomer)
        self.assertIn('agentbus answer', newcomer)
        self.assertIn('--artifacts-file', newcomer)

        # Check leader instructions contain coordinator guidance and verification standard
        leader = pack['leader_instructions']
        self.assertIn('codex', leader)
        self.assertIn('trae', leader)
        self.assertIn('agentbus files verify', leader)

    def test_deploy_onboarding_pack_puts_task_in_inbox(self):
        add_profile(self.cfg, 'workbuddy', 'Tencent WorkBuddy', kind='gui')
        pack = deploy_onboarding_pack(self.workspace, 'workbuddy', coordinator='codex')
        self.assertTrue(pack.get('deployed'))

        # Check input file was created on disk
        input_file = self.workspace / pack['input_path']
        self.assertTrue(input_file.is_file())
        self.assertIn('AgentBus Onboarding Sample for workbuddy', input_file.read_text(encoding='utf-8'))

        # Check task was posted to workbuddy's ready mailbox
        ready_files = self.bus.pending('workbuddy')
        self.assertEqual(len(ready_files), 1)
        task_data = json.loads(ready_files[0].read_text(encoding='utf-8'))
        self.assertEqual(task_data['id'], pack['task_id'])
        self.assertEqual(task_data['to'], 'workbuddy')
        self.assertEqual(task_data['sender'], 'codex')

    def test_web_bridge_onboarding_pack_action(self):
        add_profile(self.cfg, 'trae', 'ByteDance Trae', kind='gui')
        bridge = WebBridge(self.workspace)

        # 1. Test member_pack action
        resp = bridge.call('member_pack', {'member': 'trae', 'coordinator': 'codex'})
        self.assertTrue(resp.get('ok'))
        data = resp['data']
        self.assertEqual(data['member_id'], 'trae')
        self.assertIn('leader_instructions', data)
        self.assertIn('newcomer_instructions', data)

        # 2. Test onboarding_capsule action
        capsule_resp = bridge.call('onboarding_capsule', {'agent': 'trae'})
        self.assertTrue(capsule_resp.get('ok'))
        c_data = capsule_resp['data']
        self.assertEqual(c_data['agent'], 'trae')
        self.assertIn('prompt', c_data)
        self.assertIn('pack', c_data)
        self.assertIn('leader_instructions', c_data)
        self.assertIn('newcomer_instructions', c_data)

    def test_full_onboarding_file_relay_and_evidence_export(self):
        """跑一次无凭据、有限产物的真实文件接力并校验回执与能力卡"""
        add_profile(self.cfg, 'trae', 'ByteDance Trae', kind='gui')
        pack = deploy_onboarding_pack(self.workspace, 'trae', coordinator='codex')
        task_id = pack['task_id']

        # 1. Trae 认领任务
        inbox_task_file = self.workspace / pack['inbox_path']
        claimed = self.bus.claim('trae', inbox_task_file)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, task_id)
        ticket = captured_ticket(claimed)
        self.bus.heartbeat('trae', claimed, ticket, note='test onboarding sample started')

        # 2. 读取 input.txt 并生成 output.txt
        input_file = self.workspace / pack['input_path']
        input_text = input_file.read_text(encoding='utf-8')
        output_file = self.workspace / pack['output_path']
        output_file.parent.mkdir(parents=True, exist_ok=True)
        # 写出首行
        first_line = input_text.splitlines()[0]
        output_file.write_text(first_line, encoding='utf-8')

        # 3. 产物清单并校验
        files = WorkspaceFiles(self.workspace)
        artifact_entry = files.describe_artifact(pack['output_path'])
        manifest_report = files.verify_artifacts([artifact_entry])
        self.assertEqual(manifest_report['verdict'], 'match')

        # 4. 提交回执与交付
        receipt = self.bus.commit_result(
            'trae', claimed, ticket,
            body='已严格按新成员指令完成输入文件首行提取与核对。',
            artifacts=[artifact_entry],
            exit_code=0
        )
        self.assertIsNotNone(receipt)
        self.assertTrue((self.workspace / f'.agentbus/agents/trae/done/{task_id}.json').is_file())
        self.assertTrue((self.workspace / f'.agentbus/agents/codex/ready/{receipt.id}.json').is_file())

        # 5. 生成初始公开能力卡并验证
        from agentbus.member_adapter import MemberAdapter
        adapter = MemberAdapter('trae', 'ByteDance Trae')
        card = build_initial_capability_card(adapter, 'project-test', {
            'task_id': task_id,
            'result_id': receipt.id,
            'verified': True,
            'artifact_sha256': artifact_entry['sha256']
        })
        self.assertTrue(card['samples'][0]['verified'])
        self.assertEqual(card['samples'][0]['result_id'], receipt.id)

        # 6. 导出可复用证据并在第二项目干净导入（确保无凭据或在途继承）
        exported = export_reusable_evidence(card)
        imported = import_card_to_second_project(exported, 'project-clean-target')
        self.assertEqual(imported['member_id'], 'trae')
        self.assertEqual(imported['samples'][0]['project_id'], 'project-test')
        self.assertNotIn('processing', imported)
        self.assertNotIn('credentials', imported)

    def test_cli_member_pack_invocation(self):
        """测试通过 CLI 真实调用 agentbus member pack"""
        from io import StringIO
        from unittest.mock import patch
        import sys
        from agentbus.cli import main

        add_profile(self.cfg, 'trae', 'ByteDance Trae', kind='gui')
        stdout = StringIO()
        with patch('sys.stdout', stdout):
            code = main(['member', 'pack', '--workspace', str(self.workspace), '--member', 'trae'])
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        parsed = json.loads(output)
        self.assertEqual(parsed['member_id'], 'trae')
        self.assertIn('leader_instructions', parsed)
        self.assertIn('newcomer_instructions', parsed)
        self.assertIn('agentbus claim', parsed['newcomer_instructions'])


if __name__ == '__main__':
    unittest.main()

