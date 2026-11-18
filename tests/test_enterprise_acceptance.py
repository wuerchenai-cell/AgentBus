"""Source: OpenCode, 2026-10-05. Black-box acceptance for the product-platform slice.

Task m-1791203447665-493734; adopted plan p-cdef881858336918798c18bb8f096f25,
content_sha256 c739eb9b1ed706c967c6cd2d7186eda88e6004da5a2fd839e4e93022180cc3b5,
plus DESIGN_RESOLUTIONS.md dispositions (B1/B2/B3 and the OpenCode response).

This file contains no business implementation. It asserts the frozen contract
against the supplier modules and the public core:

* WorkBuddy  invitation_copy.build_instructions / describe_invitation
* ZCode     capability_card.validate_card / assess_member
* Trae      enterprise_view.build_project_view
* Codex     member_library.MemberLibrary.save / get / store_project_card
* existing  agentbus.profiles / agentbus.onboarding / agentbus.config

Supplier modules that have not arrived are reported as pending, never skipped
silently and never counted as passed. Only the public core is exercised for real;
supplied pure modules are imported from this candidate directory or from the
integration tree once Codex copies them in.

Run:
    C:/Python314/python.exe -m unittest test_enterprise_acceptance -v

Transport note: the OpenCode session has no native agentbus reply tool. That is a
transport limitation of this reviewer, recorded separately, and is never reported
as "the registered CLI is unavailable".

Codex integration binding, 2026-10-05: this derived test copy imports the actual
integration package. The original OpenCode file remains unchanged; passing this
copy is not a new independent OpenCode endorsement.
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
sys.path.insert(0,str(INTEGRATION))


def _load(name: str):
    """Missing or externally bound integrated suppliers fail acceptance."""
    module=importlib.import_module('agentbus.'+name)
    if not Path(module.__file__).resolve().is_relative_to(INTEGRATION/'agentbus'):
        raise AssertionError('supplier bound outside tested integration: '+name)
    return module


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def member_card(member_id: str, **over) -> dict:
    card = {
        'schema_version': 1,
        'member_id': member_id,
        'display_name': member_id.title(),
        'host_version': '1.0',
        'model_id': 'm',
        'route': 'desktop',
        'tools': ['read_file', 'write_file'],
        'specialties': ['static review'],
        'limitations': ['cannot be verified offline'],
        'samples': [{
            'project_id': 'proj-alpha',
            'task_id': 'm-alpha-1',
            'result_id': 'm-alpha-1-result',
            'capability_family': 'read',
            'outcome': 'done',
            'verified': True,
            'artifact_sha256': 'a' * 64,
            'observed_at': '2026-10-05T10:00:00+08:00',
            'transport': 'desktop-transcription',
            'version': '1.0',
            'model': 'm',
            'route': 'desktop',
        }],
        'source': 'bounded acceptance fixture',
        'updated_at': '2026-10-05T10:00:00+08:00',
    }
    card.update(over)
    return card


class CoreFixture(unittest.TestCase):
    """Real temp workspaces on the public core; nothing touches the project bus."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='enterprise-acceptance-')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / 'project-alpha'
        self.workspace.mkdir(parents=True)
        self.config = importlib.import_module('agentbus.config')
        self.profiles = importlib.import_module('agentbus.profiles')
        self.onboarding = importlib.import_module('agentbus.onboarding')
        self.cfg = self.config.load(self.workspace, create=True)
        # Two existing peers so "add without removing" is observable.
        for name in ('codex', 'trae'):
            self.cfg.data['workers'][name] = {
                'display_name': name.title(), 'enabled': True,
                'executor_enabled': True, 'kind': 'gui',
                'can_spawn': False, 'human_in_loop': True,
            }
        self.cfg.data['director'] = 'codex'
        self.cfg = self.config.save(self.cfg, self.cfg.data)

    def registered(self):
        return sorted(self.config.load(self.workspace).enabled_workers())


class RegistrationStories(CoreFixture):
    """User story 1: an unknown member with no CLI still joins, removing nobody."""

    def test_new_member_without_cli_registers_and_keeps_existing_members(self):
        before = set(self.registered())
        self.assertLessEqual({'codex', 'trae'}, before)
        director_before = self.config.load(self.workspace).director
        cfg = self.config.load(self.workspace)
        self.profiles.add_profile(cfg, 'newcomer', 'Newcomer', kind='gui')
        after = set(self.config.load(self.workspace).enabled_workers())
        self.assertEqual(after - before, {'newcomer'},
                         'registration changed more than the new member')
        self.assertTrue(before.issubset(after), 'an existing member was dropped')
        self.assertEqual(self.config.load(self.workspace).director, director_before,
                         'registering a peer changed the coordinator')
        self.assertEqual(director_before, 'codex')

    def test_gui_member_needs_no_command_and_rejects_one(self):
        cfg = self.config.load(self.workspace)
        self.profiles.add_profile(cfg, 'newcomer', 'Newcomer', kind='gui')
        cfg2 = self.config.load(self.workspace)
        with self.assertRaises(self.profiles.ProfileError):
            self.profiles.add_profile(cfg2, 'newcomer2', 'X', kind='gui', command=['x'])

    def test_acp_member_requires_an_argument_array_not_a_shell_string(self):
        cfg = self.config.load(self.workspace)
        with self.assertRaises(self.profiles.ProfileError):
            self.profiles.add_profile(cfg, 'cliuser', 'CLI', kind='acp',
                                      command='agent --acp')

    def test_registering_an_existing_member_never_overwrites_it(self):
        cfg = self.config.load(self.workspace)
        self.profiles.add_profile(cfg, 'newcomer', 'Newcomer', kind='gui')
        cfg2 = self.config.load(self.workspace)
        with self.assertRaises(self.profiles.ProfileError):
            self.profiles.add_profile(cfg2, 'newcomer', 'Hijacked', kind='gui')

    def test_every_registered_member_gets_a_distinct_workflow_path(self):
        cfg = self.config.load(self.workspace)
        self.profiles.add_profile(cfg, 'newcomer', 'Newcomer', kind='gui')
        cfg2 = self.config.load(self.workspace)
        paths = self.profiles.project_skill_paths(cfg2)
        self.assertEqual(len(set(paths.values())), len(paths),
                         'two members share one workflow file')

    def test_member_id_must_be_a_portable_path_component(self):
        cfg = self.config.load(self.workspace)
        for bad in ('Newcomer', 'a/b', 'con', 'director', ''):
            with self.assertRaises(self.profiles.ProfileError):
                self.profiles.add_profile(cfg, bad, 'X', kind='gui')

    def test_public_library_requires_the_member_to_exist_in_this_project(self):
        library = _load('member_library')
        if library is None:
            self.skipTest(f'member_library pending: {PENDING["member_library"]}')
        store = library.store_project_card
        cfg = self.config.load(self.workspace)
        with self.assertRaises(ValueError) as caught:
            store(cfg, 'codex', member_card('ghost'), 'MISSING')
        self.assertTrue(str(caught.exception).startswith('UNKNOWN_MEMBER'),
                        f'expected UNKNOWN_MEMBER, got {caught.exception}')


class InvitationStories(CoreFixture):
    """User stories 1 and 2: paired, executable instructions; no phantom task id."""

    @classmethod
    def setUpClass(cls):
        cls.invitation = _load('invitation_copy')

    def test_both_instructions_carry_real_paths_and_task_id(self):
        module = self.invitation
        if module is None:
            self.skipTest(f'invitation_copy pending: {PENDING["invitation_copy"]}')
        workspace = str(self.workspace.resolve())
        context = {
            'workspace': workspace, 'member': 'newcomer',
            'display_name': 'Newcomer', 'coordinator': 'codex',
            'task_id': 'm-invite-1',
            'inbox_path': str(Path(workspace) / '.agentbus/agents/newcomer/ready'),
            'input_path': str(Path(workspace) / '.agentbus/invites/in.txt'),
            'output_path': str(Path(workspace) / '.agentbus/invites/out.txt'),
        }
        built = module.build_instructions(context)
        for role in ('leader', 'newcomer'):
            text = built[role]
            self.assertIn(workspace, text, f'{role} lacks the absolute workspace')
            self.assertIn('newcomer', text)
            self.assertIn('codex', text)
            self.assertIn('m-invite-1', text, f'{role} lacks the real task id')
            self.assertIn(context['output_path'], text)
            self.assertTrue(text.strip(), f'{role} instruction is blank')

    def test_missing_required_context_is_rejected_not_guessed(self):
        module = self.invitation
        if module is None:
            self.skipTest(f'invitation_copy pending: {PENDING["invitation_copy"]}')
        base = {
            'workspace': str(self.workspace.resolve()), 'member': 'newcomer',
            'display_name': 'Newcomer', 'coordinator': 'codex',
            'task_id': 'm-invite-1', 'inbox_path': 'a', 'input_path': 'b',
            'output_path': 'c',
        }
        for key in tuple(base):
            broken = dict(base)
            broken.pop(key)
            with self.assertRaises(ValueError, msg=f'{key} was silently defaulted'):
                module.build_instructions(broken)

    def test_default_transport_disclaims_native_delivery(self):
        module = self.invitation
        if module is None:
            self.skipTest(f'invitation_copy pending: {PENDING["invitation_copy"]}')
        built = module.build_instructions({
            'workspace': str(self.workspace.resolve()), 'member': 'newcomer',
            'display_name': 'Newcomer', 'coordinator': 'codex',
            'task_id': 'm-invite-1', 'inbox_path': 'a', 'input_path': 'b',
            'output_path': 'c',
        })
        joined = built['newcomer']
        self.assertTrue(('非原生投递' in joined) or ('忠实转录' in joined),
                        'default transport does not disclose non-native delivery')

    def test_readonly_member_gets_readonly_criteria_and_no_write_claim(self):
        module = self.invitation
        if module is None:
            self.skipTest(f'invitation_copy pending: {PENDING["invitation_copy"]}')
        built = module.build_instructions({
            'workspace': str(self.workspace.resolve()), 'member': 'newcomer',
            'display_name': 'Newcomer', 'coordinator': 'codex',
            'task_id': 'm-invite-1', 'inbox_path': 'a', 'input_path': 'b',
            'output_path': 'c', 'writable': False,
        })
        text = built['newcomer']
        self.assertTrue(('未验证' in text) or ('只读' in text),
                        'a readonly member is not told the write capability is unverified')

    def test_states_do_not_jump_from_copied_to_received(self):
        module = self.invitation
        if module is None:
            self.skipTest(f'invitation_copy pending: {PENDING["invitation_copy"]}')
        copied = module.describe_invitation({'copied': True})
        self.assertTrue(copied['copied'])
        for later in ('sent', 'self_reported', 'receipt_channel_verified', 'capability_passed'):
            self.assertFalse(copied[later], f'copy alone advanced {later}')

    def test_failed_receipt_never_lights_the_capability(self):
        module = self.invitation
        if module is None:
            self.skipTest(f'invitation_copy pending: {PENDING["invitation_copy"]}')
        described = module.describe_invitation({
            'task_id': 'm-invite-1', 'receipt_id': 'm-result-1',
            'linked_task_id': 'm-invite-1', 'outcome': 'failed',
            'capability_sample': {
                'task_id': 'm-invite-1', 'result_id': 'm-result-1',
                'project_id': 'proj-alpha', 'outcome': 'passed',
                'transport': 'native', 'verified': True,
                'acceptance_verified': True,
            },
        })
        self.assertTrue(described['receipt_channel_verified'],
                        'a legal failed receipt still proves the channel')
        self.assertFalse(described['capability_passed'],
                         'a failed receipt lit the capability')

    def test_claiming_a_task_is_not_execution(self):
        module = self.invitation
        if module is None:
            self.skipTest(f'invitation_copy pending: {PENDING["invitation_copy"]}')
        described = module.describe_invitation({'claimed_by': 'newcomer'})
        self.assertIn('已认领·运行未确认', described['next_action'])
        payload = json.dumps(described, ensure_ascii=False)
        self.assertNotIn('正在执行', payload)
        self.assertNotIn('"在线"', payload)

    def test_zero_records_is_not_offline(self):
        summary = importlib.import_module('agentbus.onboarding').connection_summary(
            self.config.load(self.workspace))
        for member in summary['members']:
            if member['name'] == 'codex':
                self.assertIn('未确认', member['indicator']['label'])
                self.assertNotIn('离线', member['indicator']['label'])
                self.assertFalse(member['current_session_verified'])
        self.assertFalse(summary['current_sessions_verified'])
        self.assertFalse(summary['continuous_relay_verified'])


class CapabilityStories(CoreFixture):
    """User story 5: cards validate, recommend, and never inherit local state."""

    @classmethod
    def setUpClass(cls):
        cls.card = _load('capability_card')

    def test_unknown_member_id_is_rejected(self):
        module = self.card
        if module is None:
            self.skipTest(f'capability_card pending: {PENDING["capability_card"]}')
        with self.assertRaises(ValueError):
            module.validate_card(member_card('Bad Id'))

    def test_permission_and_tools_gate_recommendation(self):
        module = self.card
        if module is None:
            self.skipTest(f'capability_card pending: {PENDING["capability_card"]}')
        context = {'permission_allowed': False, 'available_tools': ['read_file'],
                   'route': 'desktop', 'project_id': 'proj-alpha',
                   'host_version': '1.0', 'model_id': 'm'}
        assessed = module.assess_member(member_card('newcomer'),
                                        {'capability_family': 'write',
                                         'tools': ['write_file']}, context,
                                        '2026-10-05T20:00:00+08:00')
        self.assertFalse(assessed['eligible'],
                         'a member was recommended without permission or the required tool')

    def test_history_only_assists_ranking_not_eligibility(self):
        module = self.card
        if module is None:
            self.skipTest(f'capability_card pending: {PENDING["capability_card"]}')
        context = {'permission_allowed': True, 'available_tools': ['read_file'],
                   'route': 'desktop', 'project_id': 'proj-alpha',
                   'host_version': '1.0', 'model_id': 'm'}
        assessed = module.assess_member(
            member_card('newcomer'), {'capability_family': 'write',
                                      'tools': ['write_file']}, context,
            '2026-10-05T20:00:00+08:00')
        self.assertFalse(assessed['eligible'],
                         'historical read samples unlocked a write task')

    def test_version_change_requests_retest_without_deleting_history(self):
        module = self.card
        if module is None:
            self.skipTest(f'capability_card pending: {PENDING["capability_card"]}')
        context = {'permission_allowed': True, 'available_tools': ['read_file', 'write_file'],
                   'route': 'desktop', 'project_id': 'proj-alpha',
                   'host_version': '2.0', 'model_id': 'm'}
        card = member_card('newcomer')
        assessed = module.assess_member(card, {'capability_family': 'read',
                                               'tools': ['read_file']}, context,
                                        '2026-10-05T20:00:00+08:00')
        self.assertIn('read', assessed.get('pending_retest', []),
                        'a host version change did not request retest')
        self.assertEqual(card['samples'], member_card('newcomer')['samples'],
                         'history was mutated instead of flagged')


class CrossProjectStories(unittest.TestCase):
    """User story 5: two real temp workspaces, save then import, no inheritance."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='enterprise-library-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = _load('member_library')
        self.config = importlib.import_module('agentbus.config')
        self.profiles = importlib.import_module('agentbus.profiles')

    def workspace(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        cfg = self.config.load(path, create=True)
        cfg.data['workers']['codex'] = {
            'display_name': 'Codex', 'enabled': True, 'executor_enabled': True,
            'kind': 'gui', 'can_spawn': False, 'human_in_loop': True,
        }
        cfg.data['director'] = 'codex'
        return self.config.save(cfg, cfg.data).workspace

    def test_public_card_saved_then_imported_into_a_second_workspace(self):
        module = self.library
        if module is None:
            self.skipTest(f'member_library pending: {PENDING["member_library"]}')
        alpha = self.workspace('project-alpha')
        beta = self.workspace('project-beta')
        store = module.MemberLibrary(self.root / 'member-library')
        self.profiles.add_profile(self.config.load(alpha), 'newcomer', 'Newcomer', kind='gui')
        self.profiles.add_profile(self.config.load(beta), 'newcomer', 'Newcomer', kind='gui')
        module.store_project_card(self.config.load(alpha), 'codex',
                                  member_card('newcomer'), 'MISSING')
        saved = store.save(member_card('newcomer'), 'MISSING')
        self.assertEqual(saved['member_id'], 'newcomer')
        self.assertRegex(saved['sha256'], r'^[0-9a-f]{64}$')
        fetched = store.get('newcomer')
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched['member_id'], 'newcomer')
        # Second workspace imports it; local state must not be inherited.
        result = module.store_project_card(self.config.load(beta), 'codex',
                                           store.get('newcomer'), 'MISSING')
        self.assertEqual(result['member_id'], 'newcomer')
        stored = json.loads(
            (Path(beta) / '.agentbus/member-cards/newcomer.json').read_text(encoding='utf-8'))
        for forbidden in ('current_session', 'permission', 'license', 'processing_owner',
                          'online', 'admission'):
            self.assertNotIn(forbidden, stored,
                             f'the imported card carries local state: {forbidden}')
        self.assertNotIn('state', {k for k in stored if k in ('verified', 'online')})

    def test_save_refuses_to_overwrite_without_the_current_digest(self):
        module = self.library
        if module is None:
            self.skipTest(f'member_library pending: {PENDING["member_library"]}')
        store = module.MemberLibrary(self.root / 'member-library')
        first = store.save(member_card('newcomer'), 'MISSING')
        original = (self.root / 'member-library' / 'newcomer.json')
        if not original.exists():
            candidates = list((self.root / 'member-library').rglob('newcomer*'))
            self.assertTrue(candidates, 'the saved card left no file to re-check')
            original = candidates[0]
        kept = sha256_file(original)
        with self.assertRaises(ValueError) as caught:
            store.save(member_card('newcomer', display_name='Hijack'), '0' * 64)
        self.assertTrue(str(caught.exception).startswith('CAS_MISMATCH'),
                        f'expected CAS_MISMATCH, got {caught.exception}')
        self.assertEqual(sha256_file(original), kept,
                         'a refused CAS_MISMATCH still modified the stored card')
        self.assertRegex(first['sha256'], r'^[0-9a-f]{64}$')

    def test_oversized_card_is_refused(self):
        module = self.library
        if module is None:
            self.skipTest(f'member_library pending: {PENDING["member_library"]}')
        store = module.MemberLibrary(self.root / 'member-library')
        with self.assertRaises(ValueError):
            store.save(member_card('newcomer', limitations=['x' * 70000]), 'MISSING')


class WorkbenchViewStories(CoreFixture):
    """User story 6: the home view is data, never invented activity."""

    @classmethod
    def setUpClass(cls):
        cls.view = _load('enterprise_view')

    def test_bad_input_does_not_infer_empty_tasks_or_online(self):
        module = self.view
        if module is None:
            self.skipTest(f'enterprise_view pending: {PENDING["enterprise_view"]}')
        view = module.build_project_view(self.config.load(self.workspace).data, None, None)
        self.assertTrue(view.get('records_incomplete'),
                        'missing health/jobs were treated as complete empty records')
        self.assertEqual(view.get('headline'), '任务记录需要检查')
        self.assertEqual(view.get('action_id'), 'tasks')
        for member in view.get('members', []):
            self.assertNotIn(member.get('state_label'), ('在线', '正在执行'))
        cards = view.get('cards')
        self.assertIsInstance(cards, list)
        self.assertLessEqual(len(cards), 4)

    def test_member_count_is_not_the_headline(self):
        module = self.view
        if module is None:
            self.skipTest(f'enterprise_view pending: {PENDING["enterprise_view"]}')
        view = module.build_project_view(self.config.load(self.workspace).data, None, None)
        self.assertIn('goal', view)
        self.assertNotEqual(view.get('headline'), view.get('project_title'))


if __name__ == '__main__':
    unittest.main()
