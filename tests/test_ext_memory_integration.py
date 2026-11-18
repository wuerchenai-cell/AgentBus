"""Codex integration regressions, 2026-10-08; explicitly use this runtime."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME))

from agentbus import profiles, member_adapter, onboarding_pack, continuation_pack, project_guard
from agentbus.config import load


class IntegrationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.cfg = load(self.root, create=True)
        self.adapter = member_adapter.MemberAdapter('mock-agent-alpha', 'Mock Alpha')
        self.write('PROJECT.md', '## 产品目标\n构建平级文件协作工作台\n')
        self.write('STATE.md', '更新时间：2026-10-08\n## 下一步\n- 核验真实回执\n')
        self.write('MEMORY.md', '- 日期自报 2026-10-08 没有来源\n- 来源：LOG.md 2026-10-08；交付已核验\n')

    def write(self, rel, content):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
        return target

    def plan(self):
        rev = {'schema_version': 1, 'kind': 'revision', 'workspace': str(self.root.resolve()),
               'id': 'r-integration-01', 'revision_id': 'r-integration-01',
               'content_md': '当前采用计划', 'content_sha256': hashlib.sha256('当前采用计划'.encode()).hexdigest()}
        target = self.write('.agentbus/project-plan/revisions/r-integration-01.json', rev)
        self.write('.agentbus/project-plan/current.json', {'revision_id': rev['id'],
                   'revision_sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        return rev

    def test_runtime_modules_are_all_in_integration(self):
        for module in (profiles, member_adapter, onboarding_pack, continuation_pack, project_guard):
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(RUNTIME))

    def test_profile_legacy_transport_remains_supported(self):
        self.assertEqual(profiles.add_profile(self.cfg, 'peer-new', 'New', transport='file').worker('peer-new')['transport'], 'file')

    def test_profile_generic_fields_survive_reload(self):
        profiles.add_profile(self.cfg, 'peer-new', 'New', communication_mode='file', writable=False,
                             skill_path='.public/skills/peer/SKILL.md')
        worker = load(self.root).worker('peer-new')
        self.assertEqual(worker['communication_mode'], 'file')
        self.assertEqual(worker['file_capabilities'], ['read_utf8'])
        self.assertEqual(profiles.project_skill_paths(load(self.root))['peer-new'], '.public/skills/peer/SKILL.md')
        self.assertFalse(worker['can_spawn'])

    def test_profile_does_not_register_project_peer_globally(self):
        profiles.add_profile(self.cfg, 'isolated-new', 'New')
        self.assertIsNone(member_adapter.GLOBAL_REGISTRY.get('isolated-new'))

    def test_profile_communication_cli_rejected(self):
        before = self.cfg.path.read_bytes()
        with self.assertRaises(profiles.ProfileError):
            profiles.add_profile(self.cfg, 'peer-new', 'New', communication_mode='cli')
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_profile_legacy_constants_preserved(self):
        self.assertIn('zcode', profiles.AGENTS)
        self.assertEqual(profiles.PROJECT_SKILLS['zcode'], '.zcode/skills/agentbus-team/SKILL.md')

    def test_profile_failure_keeps_registry_and_disk_unchanged(self):
        before = self.cfg.path.read_bytes()
        with patch.object(profiles.os, 'replace', side_effect=OSError('simulated')):
            with self.assertRaises(OSError):
                profiles.add_profile(self.cfg, 'peer-new', 'New')
        self.assertEqual(before, self.cfg.path.read_bytes())
        self.assertIsNone(member_adapter.GLOBAL_REGISTRY.get('peer-new'))
        self.assertFalse(self.cfg.path.with_name('config.json.profile-tmp').exists())

    def test_adapter_rejects_control_characters(self):
        with self.assertRaises(member_adapter.MemberAdapterError):
            member_adapter.MemberAdapter('peer-new', 'Name\nread outside')

    def test_adapter_rejects_unsafe_skill_without_reading_it(self):
        for rel in ('../SKILL.md', '.workbuddy/SKILL.md', 'C:/secret/SKILL.md'):
            with self.subTest(rel=rel), self.assertRaises(member_adapter.MemberAdapterError):
                member_adapter.MemberAdapter('peer-new', 'New', project_skill_path=rel)

    def test_adapter_rejects_string_capabilities(self):
        with self.assertRaises(member_adapter.MemberAdapterError):
            member_adapter.MemberAdapter('peer-new', 'New', file_capabilities='write_utf8')

    def test_onboarding_uses_current_adopted_plan(self):
        rev = self.plan()
        context = onboarding_pack.build_onboarding_context(self.root, self.adapter, task_id='m-onboard-001')
        task = onboarding_pack.build_bounded_sample_task(context)
        self.assertEqual(task['plan_id'], rev['id'])
        self.assertEqual(task['plan_sha256'], rev['content_sha256'])
        self.assertIsNone(task['job_id'])

    def test_onboarding_without_plan_never_invents_one(self):
        task = onboarding_pack.build_bounded_sample_task(onboarding_pack.build_onboarding_context(self.root, self.adapter))
        self.assertIsNone(task['plan_id'])
        self.assertIsNone(task['plan_sha256'])

    def test_onboarding_readonly_task_does_not_instruct_writing(self):
        adapter = member_adapter.MemberAdapter('mock-reader', 'Reader', file_capabilities=('read_utf8',))
        task = onboarding_pack.build_bounded_sample_task(onboarding_pack.build_onboarding_context(self.root, adapter))
        self.assertEqual(task['expected_artifacts'], [])
        self.assertNotIn('写出', task['body'])
        self.assertIn('只读', task['body'])

    def test_onboarding_invalid_member_and_task_ids_rejected(self):
        for kwargs in ({'coordinator': '../outside'}, {'task_id': '../outside'}):
            with self.assertRaises(ValueError):
                onboarding_pack.build_onboarding_context(self.root, self.adapter, **kwargs)

    def test_onboarding_instructions_include_manifest_for_output(self):
        text = onboarding_pack.build_newcomer_instructions(onboarding_pack.build_onboarding_context(self.root, self.adapter))
        self.assertIn('--artifacts-file', text)
        self.assertIn('SHA256', text)

    def test_card_missing_verification_stays_unverified(self):
        card = onboarding_pack.build_initial_capability_card(self.adapter, 'project-a', {'task_id': 'm-sample-01'})
        self.assertFalse(card['samples'][0]['verified'])
        self.assertIsNone(card['samples'][0]['result_id'])

    def test_card_verified_without_receipt_rejected(self):
        with self.assertRaises(ValueError):
            onboarding_pack.build_initial_capability_card(self.adapter, 'project-a', {'task_id': 'm-sample-01', 'verified': True})

    def test_evidence_nested_private_fields_rejected(self):
        card = onboarding_pack.build_initial_capability_card(self.adapter, 'project-a', {'task_id': 'm-sample-01'})
        card['samples'][0]['credentials'] = {'token': 'SYNTHETIC-NEVER-EXPORT'}
        with self.assertRaises(ValueError):
            onboarding_pack.export_reusable_evidence(card)

    def test_evidence_import_requires_strict_metadata(self):
        pack = onboarding_pack.export_reusable_evidence(onboarding_pack.build_initial_capability_card(self.adapter, 'project-a'))
        pack['processing'] = ['m-owned-old']
        with self.assertRaises(ValueError):
            onboarding_pack.import_card_to_second_project(pack, 'project-b')

    def test_evidence_import_deep_copies_history_and_stays_project_scoped(self):
        card = onboarding_pack.build_initial_capability_card(self.adapter, 'project-a', {'task_id': 'm-sample-01'})
        pack = onboarding_pack.export_reusable_evidence(card)
        imported = onboarding_pack.import_card_to_second_project(pack, 'project-b')
        imported['samples'][0]['task_id'] = 'changed'
        self.assertEqual(card['samples'][0]['task_id'], 'm-sample-01')
        self.assertEqual(pack['reusable_samples'][0]['project_id'], 'project-a')
        self.assertIsNone(imported['host_version'])

    def test_guard_understands_formal_revision_directory(self):
        self.plan()
        self.assertEqual(project_guard.ProjectGuard(self.root).check_multiple_authorities(), [])

    def test_guard_rejects_revision_path_traversal(self):
        self.write('.agentbus/project-plan/current.json', {'revision_id': '../../outside'})
        codes = [x.code for x in project_guard.ProjectGuard(self.root).check_multiple_authorities()]
        self.assertIn('MALFORMED_CURRENT_JSON', codes)

    def test_guard_safety_negations_are_not_path_access(self):
        self.write('AGENTS.md', '不访问 `.workbuddy/`、凭据或用户禁止路径。\nDo not read credentials.json.\n')
        self.assertEqual(project_guard.ProjectGuard(self.root).check_private_path_references(), [])

    def test_guard_positive_private_access_still_rejected(self):
        self.write('STATE.md', '读取 credentials.json 获取鉴权信息。\n禁止扩大权限；下一步读取 .workbuddy/private/config.json。')
        violations = project_guard.ProjectGuard(self.root).check_private_path_references()
        self.assertGreaterEqual(len(violations), 2)

    def test_guard_date_only_state_and_aware_done_do_not_crash(self):
        self.write('.agentbus/agents/codex/done/m-done-001.json', {'id': 'm-done-001', 'status': 'done', 'completed_at': '2026-10-08T12:30:00+08:00'})
        codes = [x.code for x in project_guard.ProjectGuard(self.root).check_state_freshness()]
        self.assertIn('STATE_FRESHNESS_UNVERIFIED', codes)
        self.assertNotIn('STALE_STATE', codes)

    def test_guard_old_claim_time_does_not_hide_recent_finish(self):
        self.write('STATE.md', '更新时间：2026-10-08T10:00:00+08:00')
        self.write('.agentbus/agents/codex/done/m-done-001.json', {'id': 'm-done-001', 'status': 'done', 'claimed_at': '2026-10-01T00:00:00+08:00', 'completed_at': '2026-10-08T12:30:00+08:00'})
        self.assertIn('STALE_STATE', [x.code for x in project_guard.ProjectGuard(self.root).check_state_freshness()])

    def test_guard_similar_prefix_topics_not_same_goal(self):
        self.write('LOG.md', '\n'.join('反复: ' + '共同前缀' * 8 + str(n) for n in range(3)))
        self.assertNotIn('REPETITIVE_THEME_LOOPING', [x.code for x in project_guard.ProjectGuard(self.root).check_goal_drift_and_looping()])

    def test_guard_same_topic_resolved_is_not_active_loop(self):
        self.write('LOG.md', '反复: 样式问题\n反复: 样式问题\n反复: 样式问题\n已收敛: 样式问题\n')
        self.assertNotIn('REPETITIVE_THEME_LOOPING', [x.code for x in project_guard.ProjectGuard(self.root).check_goal_drift_and_looping()])

    def test_continuation_product_goal_and_l0_log_semantics(self):
        text = continuation_pack.build_continuation_pack(self.root, member_id='mock-agent-alpha')
        self.assertIn('构建平级文件协作工作台', text)
        self.assertNotIn('L0 顶层宗旨', text)
        self.assertIn('L0', text)
        self.assertIn('日志', text)

    def test_continuation_never_upgrades_bare_date_to_verified(self):
        data = continuation_pack.extract_pack_data(self.root, 'mock-agent-alpha')
        self.assertEqual(len(data['verified_facts']), 1)
        self.assertIn('来源', data['verified_facts'][0])

    def test_continuation_member_path_traversal_rejected(self):
        with self.assertRaises(continuation_pack.ContinuationPackError):
            continuation_pack.extract_pack_data(self.root, '../outside')

    def test_continuation_budget_has_hard_limit_even_tiny_values(self):
        for n in (1, 12, 1200):
            self.assertLessEqual(len(continuation_pack.build_continuation_pack(self.root, 'mock-agent-alpha', max_chars=n)), n)
        with self.assertRaises(continuation_pack.ContinuationPackError):
            continuation_pack.build_continuation_pack(self.root, 'mock-agent-alpha', max_chars=-1)

    def test_continuation_missing_blocker_state_stays_unverified(self):
        self.write('STATE.md', '## 下一步\n- 检查文件')
        self.assertIn('未验证', continuation_pack.build_continuation_pack(self.root, 'mock-agent-alpha'))

    def test_continuation_rejects_oversized_current_without_read_bytes(self):
        self.write('.agentbus/project-plan/current.json', '{' + ' ' * (70 * 1024) + '}')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded read')):
            with self.assertRaises(continuation_pack.ContinuationPackError):
                continuation_pack.extract_pack_data(self.root, 'mock-agent-alpha')

    def test_continuation_deterministic_tasks_include_deadline(self):
        for suffix in ('002', '001'):
            self.write(f'.agentbus/agents/mock-agent-alpha/processing/m-task-{suffix}.json',
                       {'id': f'm-task-{suffix}', 'title': '任务', 'deadline_ts': 1800000000})
        data = continuation_pack.extract_pack_data(self.root, 'mock-agent-alpha')
        self.assertIn('m-task-001', data['member_active_task'])
        self.assertIn('1800000000', data['member_active_task'])
        self.assertEqual(continuation_pack.build_continuation_pack(self.root, 'mock-agent-alpha'),
                         continuation_pack.build_continuation_pack(self.root, 'mock-agent-alpha'))

    def test_guard_oversized_public_doc_report_is_unverified(self):
        self.write('PROJECT.md', 'a' * (70 * 1024))
        report = project_guard.ProjectGuard(self.root).check_all()
        self.assertFalse(report['passed'])
        self.assertIn('PUBLIC_INPUT_UNVERIFIED', [x['code'] for x in report['violations']])

    def test_reader_ancestor_reparse_rejected_before_read(self):
        from agentbus.public_files import PublicFiles, PublicFileError
        public = PublicFiles(self.root)
        original = Path.lstat
        class LinkStat:
            st_mode = 0o40755
            st_file_attributes = 0x400
        def fake(node):
            if node == self.root / '.agentbus': return LinkStat()
            return original(node)
        with patch.object(Path, 'lstat', fake):
            with self.assertRaises(PublicFileError):
                public.read_text('.agentbus/config.json')

    def test_generic_member_real_fenced_receipt_and_cross_project_history(self):
        from agentbus.bus import Bus, Message
        from agentbus.claim_fencing import captured_ticket
        from agentbus.workspace_files import WorkspaceFiles
        profiles.add_profile(self.cfg, self.adapter.member_id, self.adapter.display_name)
        context = onboarding_pack.build_onboarding_context(self.root, self.adapter)
        bus = Bus(self.root / '.agentbus')
        task = Message.from_dict(onboarding_pack.build_bounded_sample_task(context))
        bus.post(task)
        claimed = bus.claim(self.adapter.member_id, self.root / context['inbox_path'])
        self.assertIsNotNone(claimed)
        ticket = captured_ticket(claimed)
        bus.heartbeat(self.adapter.member_id, claimed, ticket,
                      note='test generic member started')
        target = self.root / context['output_path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'GENERIC-REAL-DELIVERY')
        files = WorkspaceFiles(self.root)
        artifact = files.describe_artifact(context['output_path'])
        self.assertEqual(files.verify_artifacts([artifact])['verdict'], 'match')
        result = bus.commit_result(self.adapter.member_id, claimed, ticket,
                                   body='真实文件已核验', artifacts=[artifact], exit_code=0)
        self.assertTrue((self.root / f'.agentbus/agents/{self.adapter.member_id}/done/{task.id}.json').is_file())
        self.assertTrue((self.root / f'.agentbus/agents/codex/ready/{result.id}.json').is_file())
        card = onboarding_pack.build_initial_capability_card(self.adapter, 'project-a', {
            'task_id': task.id, 'result_id': result.id, 'verified': True,
            'artifact_sha256': artifact['sha256']})
        imported = onboarding_pack.import_card_to_second_project(onboarding_pack.export_reusable_evidence(card), 'project-b')
        self.assertEqual(imported['samples'][0]['project_id'], 'project-a')
        self.assertNotIn('claim_id', json.dumps(imported))
        self.assertNotIn('processing', imported)

    def test_guard_audit_done_time_overrides_task_creation_time(self):
        self.write('STATE.md', '更新时间：2026-10-08T10:00:00+08:00')
        self.write('.agentbus/agents/codex/done/m-done-001.json', {'id': 'm-done-001', 'status': 'done', 'type': 'task', 'ts': '2026-10-01T00:00:00+08:00'})
        self.write('.agentbus/events.jsonl', json.dumps({'event': 'done', 'id': 'm-done-001', 'type': 'task', 'ts': '2026-10-08T12:00:00+08:00'}) + '\n')
        self.assertIn('STALE_STATE', [x.code for x in project_guard.ProjectGuard(self.root).check_state_freshness()])

    def test_guard_working_environment_is_not_env_secret_file(self):
        self.write('PROJECT.md', '使用 os.environ 和 .environment-notes 描述环境。\n禁止读取 .env 文件。')
        self.assertEqual(project_guard.ProjectGuard(self.root).check_private_path_references(), [])

    def test_guard_negation_does_not_exempt_later_positive_access(self):
        self.write('STATE.md', 'Do not access .workbuddy, but read credentials.json.\n禁止扩大权限，但下一步读取 .workbuddy/private/config.json。')
        self.assertEqual(len(project_guard.ProjectGuard(self.root).check_private_path_references()), 2)

    def test_guard_same_root_prefix_collision_is_external_project(self):
        self.write('HANDOFF.md', f'下一步读取 {self.root.as_posix()}-other-workspace/STATE.md')
        self.assertIn('CROSS_PROJECT_PATH_POLLUTION', [x.code for x in project_guard.ProjectGuard(self.root).check_cross_project_pollution()])

    def test_reader_private_lexical_path_never_touches_filesystem(self):
        from agentbus.public_files import PublicFiles, PublicFileError
        reader = PublicFiles(self.root)
        with patch.object(Path, 'lstat', side_effect=AssertionError('must reject lexically')):
            with self.assertRaises(PublicFileError):
                reader.read_text('.workbuddy/secret.json')

    def test_reader_rejects_huge_file_before_open(self):
        from agentbus.public_files import PublicFiles, PublicFileError
        reader = PublicFiles(self.root)
        self.write('HUGE.md', 'x' * (70 * 1024))
        with patch.object(Path, 'open', side_effect=AssertionError('must bound before open')):
            with self.assertRaises(PublicFileError):
                reader.read_text('HUGE.md')

    def test_plan_corruption_never_becomes_onboarding_evidence(self):
        self.plan()
        self.write('.agentbus/project-plan/revisions/r-integration-01.json', '{}')
        with self.assertRaises(ValueError):
            onboarding_pack.build_onboarding_context(self.root, self.adapter)

    def test_guard_result_messages_are_not_task_completion(self):
        self.write('STATE.md', '更新时间：2026-10-08T10:00:00+08:00\n## 下一步\n- 执行 m-result-001\n')
        self.write('.agentbus/agents/codex/done/m-result-001.json', {'id': 'm-result-001', 'status': 'done', 'type': 'result', 'completed_at': '2026-10-08T12:00:00+08:00'})
        guard = project_guard.ProjectGuard(self.root)
        self.assertEqual(guard.check_state_freshness(), [])
        self.assertEqual(guard.check_ghost_tasks(), [])

    def test_guard_quoted_workspace_with_spaces_is_current_root(self):
        other = self.root / '中文 互通'
        other.mkdir()
        (other / 'HANDOFF.md').write_text(f'--workspace "{other}"', encoding='utf-8')
        self.assertEqual(project_guard.ProjectGuard(other).check_cross_project_pollution(), [])

    def test_guard_configured_external_output_root_is_valid_business_target(self):
        payload = json.loads(self.cfg.path.read_text(encoding='utf-8'))
        payload['output_root'] = 'J:/Business_Workspace/delivery'
        self.cfg.path.write_text(json.dumps(payload), encoding='utf-8')
        self.write('HANDOFF.md', '业务写入 J:/Business_Workspace/delivery/out.txt')
        self.assertEqual(project_guard.ProjectGuard(self.root).check_cross_project_pollution(), [])

    def test_continuation_unique_next_step_and_boundary_headings(self):
        self.write('STATE.md', '## 唯一下一步\n- 集成候选\n## 边界\n- 仅写隔离目录\n')
        data = continuation_pack.extract_pack_data(self.root, 'mock-agent-alpha')
        self.assertEqual(data['single_next_step'], '集成候选')
        self.assertEqual(data['blockers'], ['仅写隔离目录'])

    def test_adapter_cannot_carry_credentials_in_metadata(self):
        with self.assertRaises(member_adapter.MemberAdapterError):
            member_adapter.MemberAdapter('peer-new', 'New', metadata={'credentials': {'token': 'synthetic'}})

    def test_continuation_long_goal_still_preserves_next_action_and_sources(self):
        self.write('PROJECT.md', '## 产品目标\n' + '保持总体目标并逐项收敛。' * 250)
        self.write('STATE.md', '## 唯一下一步\n- 提交已核验产物\n')
        text = continuation_pack.build_continuation_pack(self.root, 'mock-agent-alpha')
        self.assertIn('提交已核验产物', text)
        self.assertIn('证据溯源', text)
        self.assertLessEqual(len(text), 1200)

    def test_continuation_inline_next_step_is_compatible(self):
        self.write('STATE.md', '更新时间：2026-10-08\n唯一下一步：集成真实候选并核对回执\n')
        self.assertEqual(continuation_pack.extract_pack_data(self.root, 'mock-agent-alpha')['single_next_step'],
                         '集成真实候选并核对回执')

    def test_adapter_metadata_cannot_be_mutated_through_original_dict(self):
        meta = {'vendor': 'Public Vendor'}
        adapter = member_adapter.MemberAdapter('peer-new', 'New', metadata=meta)
        meta['credentials'] = 'synthetic'
        self.assertNotIn('credentials', adapter.to_dict()['metadata'])
        with self.assertRaises(TypeError):
            adapter.metadata['credentials'] = 'synthetic'

    def test_project_registry_loads_generic_metadata_without_session_state(self):
        profiles.add_profile(self.cfg, 'generic-reviewer', 'Reviewer', writable=False)
        current = load(self.root)
        current.worker('generic-reviewer')['processing'] = ['old-owned-task']
        registry = profiles.adapters_for_project(current)
        adapter = registry.get('generic-reviewer')
        self.assertFalse(adapter.writable)
        self.assertNotIn('processing', adapter.to_dict())
        self.assertNotIn('can_spawn', adapter.to_dict())

    def test_project_registries_do_not_share_custom_peers(self):
        profiles.add_profile(self.cfg, 'only-project-a', 'A')
        other = load(Path(self.temp.name) / 'second-project', create=True)
        self.assertIsNotNone(profiles.adapters_for_project(load(self.root)).get('only-project-a'))
        self.assertIsNone(profiles.adapters_for_project(other).get('only-project-a'))


if __name__ == '__main__':
    unittest.main()
