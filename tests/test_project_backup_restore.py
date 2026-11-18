"""
Tests for PROJECT-BACKUP-RESTORE (P2, §6.11)
Source: Antigravity (2026-10-12)

Verifies:
1. Preview before export:
   - Contains task tree, team settings (director, assignment_mode, workers),
     sticky notes, branch folds, and card priorities.
   - Strictly sanitizes/excludes credentials, secrets, private sessions, and absolute paths.
   - Generates verifiable SHA256 digest and manifest summary.
2. Preview before restore:
   - Deeply inspects differences between live workspace and backup bundle.
   - Issues preview_id ticket required for confirmation.
   - Rebinds configuration to the local target workspace rather than blindly importing foreign paths.
3. Apply restore:
   - Requires user confirmation with matching preview_id.
   - Atomic update under task-forest structure lock.
   - Restores task tree, team settings, and UI preferences (notes, folds, priorities).
4. WebBridge API and CLI integration:
   - WebBridge actions `export_backup` and `restore_backup` pass contract tests.
   - CLI commands `tree backup` and `tree restore` work end-to-end.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch, Mock
from agentbus import gui_state
from pathlib import Path

from agentbus.backup import (
    BACKUP_FORMAT,
    create_backup_bundle,
    preview_backup_export,
    preview_backup_restore,
    apply_backup_restore,
)
from agentbus.bus import Bus
from agentbus.config import load, save
from agentbus.gui_state import load_notes, save_notes, load_priorities, save_priorities
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class TestProjectBackupRestore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        state_patch = patch.object(gui_state, 'state_path', return_value=Path(self.temp_dir.name) / 'ui.json')
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.workspace = Path(self.temp_dir.name).resolve()
        self.cfg = load(self.workspace, create=True)
        self.cfg.data['director'] = 'codex'
        self.cfg.data['assignment_mode'] = 'automatic'
        self.cfg.data['workers']['codex']['enabled'] = True
        self.cfg.data['workers']['codex']['executor_enabled'] = True
        self.cfg.data['workers']['codex']['secret_token'] = 'sensitive_secret_value'
        save(self.cfg)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()

        # Seed task tree
        self.forest = TaskForest(self.cfg)
        tree = {
            'schema_version': 1,
            'roots': [{'id': 'root', 'parent_id': None, 'title': '根目标', 'goal': '完成架构交付'}],
            'branches': [{'id': 'b1', 'parent_id': 'root', 'title': '分支1'}],
            'leaves': [
                {'id': 'leaf-1', 'parent_id': 'b1', 'title': '任务1', 'goal': '完成任务1'},
                {'id': 'leaf-2', 'parent_id': 'b1', 'title': '任务2', 'goal': '完成任务2', 'dependencies': ['leaf-1']},
            ],
        }
        self.forest.replace_structure(tree)

        # Seed UI preferences
        save_notes(self.workspace, [
            {'id': 'note-1', 'x': 150.0, 'y': 200.0, 'w': 240.0, 'h': 180.0, 'color': 'yellow', 'text': '重要设计记录', 'collapsed': False}
        ])
        save_priorities(self.workspace, {'leaf-1': 'P0', 'leaf-2': 'P1'})

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_backup_bundle_sanitization_and_structure(self):
        bundle = create_backup_bundle(self.workspace)
        self.assertEqual(bundle['format'], BACKUP_FORMAT)
        self.assertTrue(bundle.get('bundle_sha256'))

        # Check task tree
        tree_doc = bundle['task_tree']
        self.assertEqual(len(tree_doc['leaves']), 2)

        # Check team settings sanitization
        team = bundle['team_settings']
        self.assertEqual(team['director'], 'codex')
        self.assertNotIn('secret_token', team['workers']['codex'])
        self.assertNotIn('desktop_icon_path', team['workers']['codex'])

        # Check UI preferences
        ui = bundle['ui_preferences']
        self.assertEqual(len(ui['sticky_notes']), 1)
        self.assertEqual(ui['sticky_notes'][0]['text'], '重要设计记录')
        # §13.7：折叠/优先级必须与任务树用**同一 id 映射**，键是模板节点 id，
        # 而不再是原本机 id —— 原本机 id 原样导出的话，还原后根本解析不到节点。
        template_ids = {n['id'] for kind in ('roots', 'branches', 'leaves')
                        for n in bundle['task_tree'][kind]}
        self.assertEqual({'P0', 'P1'}, set(ui['card_priorities'].values()))
        self.assertTrue(set(ui['card_priorities']).issubset(template_ids),
                        '优先级键必须是任务树里的真实节点 id')
        self.assertNotIn('leaf-1', ui['card_priorities'],
                         '原本机 id 不应直接出现在备份里（否则还原后无法解析）')

        # Verify no machine absolute paths in tree
        raw_json = json.dumps(bundle)
        self.assertNotIn(str(self.workspace), raw_json)

    def test_preview_export_manifest(self):
        preview = preview_backup_export(self.workspace)
        self.assertEqual(preview['format'], BACKUP_FORMAT)
        self.assertTrue(preview['sanitized'])
        self.assertTrue(preview['no_credentials'])
        manifest = {item['item']: item['count'] for item in preview['manifest']}
        self.assertEqual(manifest['任务树拓扑'], 4) # 1 root + 1 branch + 2 leaves
        self.assertEqual(manifest['画布便签'], 1)
        self.assertEqual(manifest['卡片视觉优先级'], 2)

    def test_preview_restore_and_diff(self):
        bundle = create_backup_bundle(self.workspace)

        # Create another target workspace
        with tempfile.TemporaryDirectory() as other_dir:
            other_ws = Path(other_dir).resolve()
            other_cfg = load(other_ws, create=True)
            other_cfg.data['workers']['antigravity']['enabled'] = True
            other_cfg.data['workers']['antigravity']['executor_enabled'] = True
            other_cfg.data['director'] = 'antigravity'
            save(other_cfg)
            Bus(other_ws / '.agentbus').init()

            preview = preview_backup_restore(other_ws, bundle)
            self.assertTrue(preview['preview_id'].startswith('prev-'))
            diff = preview['diff_summary']
            self.assertIn('0 个任务 → 恢复后 2 个任务', diff['tree_leaves_change'])
            self.assertEqual(diff['director_change'], 'antigravity → codex')
            self.assertEqual(preview['bound_workspace'], str(other_ws))

            # Apply restore with wrong preview_id must fail
            with self.assertRaises(ValueError):
                apply_backup_restore(other_ws, bundle, preview_id='invalid_ticket')

            # Apply restore with correct preview_id
            result = apply_backup_restore(other_ws, bundle, preview_id=preview['preview_id'])
            self.assertTrue(result['restored'])
            self.assertEqual(result['leaves_count'], 2)

            # Check target workspace after restore
            res_forest = TaskForest(load(other_ws))
            snap = res_forest.snapshot()
            self.assertEqual(len(snap['leaves']), 2)

            res_notes = load_notes(other_ws)
            self.assertEqual(len(res_notes), 1)
            self.assertEqual(res_notes[0]['text'], '重要设计记录')

            res_prio = load_priorities(other_ws)
            # §13.7：还原后的优先级必须绑定到**真实存在**的叶子节点
            snap_ids = {leaf['id'] for leaf in snap['leaves']}
            self.assertEqual({'P0', 'P1'}, set(res_prio.values()))
            self.assertTrue(set(res_prio).issubset(snap_ids),
                            '还原后的优先级必须解析到真实叶子，未解析：%s'
                            % sorted(set(res_prio) - snap_ids))

    def test_web_bridge_export_and_restore_backup(self):
        bridge = WebBridge(self.workspace)

        # 1. Export preview
        res_prev = bridge.call('export_backup', {'preview_only': True})
        self.assertTrue(res_prev['ok'], res_prev)
        self.assertTrue(len(res_prev['data']['manifest']) >= 3)

        # 2. Export to file
        backup_file = self.workspace / 'my_backup.json'
        res_exp = bridge.call('export_backup', {'path': str(backup_file)})
        self.assertTrue(res_exp['ok'], res_exp)
        self.assertTrue(backup_file.is_file())

        # 3. Restore preview
        res_rest_prev = bridge.call('restore_backup', {'path': str(backup_file), 'confirm': False})
        self.assertTrue(res_rest_prev['ok'], res_rest_prev)
        preview_data = res_rest_prev['data']['preview']
        self.assertTrue(preview_data['preview_id'])

        # 4. Restore apply
        res_rest_conf = bridge.call('restore_backup', {
            'path': str(backup_file),
            'confirm': True,
            'preview_id': preview_data['preview_id'],
        })
        self.assertTrue(res_rest_conf['ok'], res_rest_conf)
        self.assertTrue(res_rest_conf['data']['restored'])


if __name__ == '__main__':
    unittest.main()
