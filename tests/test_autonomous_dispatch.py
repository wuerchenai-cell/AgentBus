"""Tests for Autonomous Pull Scheduling (自由调度 / 自主接单模式).

Tests coverage:
1. assignment_mode configuration: support 'autonomous' mode
2. claim_next core functionality: atomic allocation and claim
3. in-flight limit enforcement: max 1 active processing task per agent
4. dependency gate: blocked tasks cannot be claimed until predecessors are delivered
5. multi-agent concurrency: mutual exclusivity under atomic locks
6. CLI claim-next and claim --next integration
"""
import sys
from pathlib import Path
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.cli import cmd_claim, cmd_claim_next
from agentbus.config import load, save
from agentbus.task_assignment import assignment_mode, MODES
from agentbus.task_tree import TaskForest, TaskTreeError


class AutonomousDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = load(self.root, create=True)
        for worker in self.cfg.data['workers'].values():
            worker['enabled'] = worker['executor_enabled'] = False
        self.cfg.data['workers'].update({
            agent: {
                'enabled': True,
                'executor_enabled': True,
                'can_spawn': False,
                'kind': 'gui',
                'transport': 'file',
                'communication_mode': 'file',
                'host_version': '1',
                'model_id': 'model-1'
            } for agent in ('alpha', 'beta', 'gamma')
        })
        self.cfg.data['director'] = 'alpha'
        self.cfg.data['assignment_mode'] = 'autonomous'
        self.cfg = save(self.cfg)
        self.forest = TaskForest(self.cfg)
        self.bus = self.forest.bus

        # 构建包含依赖链的任务树:
        # leaf-1 (无依赖)
        # leaf-2 (无依赖，多工位可做)
        # leaf-3 (依赖 leaf-1)
        self.structure = {
            'schema_version': 1,
            'roots': [{'id': 'project', 'parent_id': None, 'title': 'Project', 'goal': 'Ship project'}],
            'branches': [{'id': 'feature', 'parent_id': 'project', 'title': 'Feature'}],
            'leaves': [
                {
                    'id': 'leaf-1',
                    'parent_id': 'feature',
                    'title': 'Task One',
                    'goal': 'First goal',
                    'handoff': 'Handoff 1',
                    'dependencies': []
                },
                {
                    'id': 'leaf-2',
                    'parent_id': 'feature',
                    'title': 'Task Two',
                    'goal': 'Second goal',
                    'handoff': 'Handoff 2',
                    'dependencies': []
                },
                {
                    'id': 'leaf-3',
                    'parent_id': 'feature',
                    'title': 'Task Three',
                    'goal': 'Third goal',
                    'handoff': 'Handoff 3',
                    'dependencies': ['leaf-1']
                }
            ]
        }

        self.forest.replace_structure(self.structure)

    def test_assignment_mode_autonomous_is_supported(self):
        """测试 assignment_mode 完备支持 autonomous 自由调度模式"""
        self.assertIn('autonomous', MODES)
        self.assertEqual(assignment_mode({'assignment_mode': 'autonomous'}), 'autonomous')
        self.assertEqual(assignment_mode({'assignment_mode': 'automatic'}), 'automatic')
        self.assertEqual(assignment_mode({'assignment_mode': 'manual'}), 'manual')
        with self.assertRaises(ValueError):
            assignment_mode({'assignment_mode': 'invalid_mode'})

    def test_claim_next_atomic_pull(self):
        """测试自主接单：空闲 Agent 能够原子认领第一个可领取的就绪任务并直接进入 processing/"""
        # alpha 处于空闲状态，调用 claim_next
        res = self.forest.claim_next('alpha')
        self.assertEqual(res['status'], 'claimed')
        msg = res['message']
        self.assertIsNotNone(msg)
        self.assertEqual(msg.claimed_by, 'alpha')
        self.assertEqual(msg.status, 'claimed')
        self.assertTrue((self.bus.agents_dir / 'alpha/processing' / f"{msg.id}.json").is_file())

        # 检查工位此时有在途任务
        self.assertEqual(len(self.bus.processing('alpha')), 1)

    def test_in_flight_limit_enforced(self):
        """测试单工位在途上限（In-flight limit = 1）：已有任务时拒绝重复接单"""
        # 第一次抢单成功
        res1 = self.forest.claim_next('alpha')
        self.assertEqual(res1['status'], 'claimed')
        msg1 = res1['message']

        # 第二次抢单应当被限制
        res2 = self.forest.claim_next('alpha')
        self.assertEqual(res2['status'], 'already_has_processing')
        self.assertEqual(res2['message'].id, msg1.id)
        self.assertIn('已有在途任务', res2['detail'])

        # 只有交付完成之后，才能接下一个
        ticket = captured_ticket(msg1)
        self.bus.heartbeat('alpha', msg1, ticket, note='working')
        self.bus.commit_result('alpha', msg1, ticket, body='Task 1 finished', exit_code=0)

        # 交付后再次接单，成功认领 leaf-2
        res3 = self.forest.claim_next('alpha')
        self.assertEqual(res3['status'], 'claimed')
        self.assertEqual(res3['leaf']['id'], 'leaf-2')

    def test_dependency_gate_for_autonomous_pull(self):
        """测试依赖门禁：依赖未满足的叶子绝不会被提前抢走"""
        # alpha 接了 leaf-1，beta 接了 leaf-2
        self.forest.claim_next('alpha')
        self.forest.claim_next('beta')

        # 此时只剩下 leaf-3，但 leaf-3 依赖 leaf-1（尚未完成）
        # gamma（也是空闲工位）尝试抢单，由于没有无前置依赖的任务，应当返回 no_available_tasks
        res_gamma = self.forest.claim_next('gamma')
        self.assertEqual(res_gamma['status'], 'no_available_tasks')

    def test_multi_agent_concurrency_no_double_claim(self):
        """测试多 Agent 并发互斥：两个 Agent 同时抢同一个单任务时，原子锁保证互斥"""
        # 建立只包含一个可做任务的树
        single_tree = {
            'schema_version': 1,
            'roots': [{'id': 'project', 'parent_id': None, 'title': 'Project', 'goal': 'Ship'}],
            'branches': [{'id': 'feature', 'parent_id': 'project', 'title': 'Feature'}],
            'leaves': [
                {
                    'id': 'single-task',
                    'parent_id': 'feature',
                    'title': 'Only One Task',
                    'goal': 'Goal',
                    'handoff': 'Handoff',
                    'dependencies': []
                }
            ]
        }

        self.forest.replace_structure(single_tree)

        results = {}
        def pull_work(agent_name):
            try:
                res = self.forest.claim_next(agent_name)
                results[agent_name] = res
            except Exception as e:
                results[agent_name] = {'status': 'error', 'error': str(e)}

        t1 = threading.Thread(target=pull_work, args=('alpha',))
        t2 = threading.Thread(target=pull_work, args=('beta',))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        statuses = [results['alpha']['status'], results['beta']['status']]
        # 必须恰好有一个 claimed，另一个 no_available_tasks
        self.assertIn('claimed', statuses)
        self.assertIn('no_available_tasks', statuses)
        self.assertEqual(statuses.count('claimed'), 1)

    def test_cli_claim_next_command(self):
        """测试 CLI 子命令 agentbus claim-next 和 claim --next"""
        # 1. 测试 claim-next 子命令
        args1 = argparse.Namespace(
            workspace=str(self.root),
            agent='alpha'
        )
        rc1 = cmd_claim_next(args1)
        self.assertEqual(rc1, 0)
        self.assertEqual(len(self.bus.processing('alpha')), 1)

        # 2. 再次执行 claim-next，由于已有在途任务，应当返回状态码 1（受限）
        rc_repeat = cmd_claim_next(args1)
        self.assertEqual(rc_repeat, 1)

        # 3. 测试 beta 使用 claim --next 成功接走第二个就绪任务
        args2 = argparse.Namespace(
            workspace=str(self.root),
            agent='beta',
            next=True,
            id=None
        )
        rc2 = cmd_claim(args2)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(self.bus.processing('beta')), 1)

    def test_cli_tree_artifacts_and_verify(self):
        """测试 CLI 子命令 tree artifacts 和 tree verify 的产品输出完整性核验"""
        import hashlib
        from agentbus.cli import cmd_tree, cmd_answer

        # 1. 抢单 leaf-1
        res = self.forest.claim_next('alpha')
        self.assertEqual(res['status'], 'claimed')
        msg = res['message']
        ticket = captured_ticket(msg).to_dict()

        # 2. 生产期望产物并交付
        art_path = self.root / "成果" / "core_mod.py"
        art_path.parent.mkdir(parents=True, exist_ok=True)
        art_path.write_text("# Core output\nVALUE = 100\n", encoding="utf-8")
        art_bytes = art_path.stat().st_size
        art_sha = hashlib.sha256(art_path.read_bytes()).hexdigest()

        manifest = [{'path': '成果/core_mod.py', 'bytes': art_bytes, 'sha256': art_sha}]
        m_file = self.root / "manifest.json"
        m_file.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        r_file = self.root / "receipt.md"
        r_file.write_text("完成 core_mod 交付", encoding="utf-8")
        r_sha = hashlib.sha256(r_file.read_bytes()).hexdigest()

        args_ans = argparse.Namespace(
            workspace=str(self.root), agent='alpha', id=msg.id,
            claim_epoch=ticket['claim_epoch'], claim_id=ticket['claim_id'],
            body=None, body_file="receipt.md", expected_body_sha256=r_sha,
            artifacts_file="manifest.json", failed=False, result_to=None
        )
        rc_ans = cmd_answer(args_ans)
        self.assertEqual(rc_ans, 0)

        # 3. 另两项仍在队列中，显式核验当前已交付的文件。
        args_art = argparse.Namespace(workspace=str(self.root), action='artifacts', allow_partial=True)
        rc_art = cmd_tree(args_art)
        self.assertEqual(rc_art, 0)

        # 4. 执行 tree verify -> 应当同样返回 0
        args_ver = argparse.Namespace(workspace=str(self.root), action='verify', allow_partial=True)
        rc_ver = cmd_tree(args_ver)
        self.assertEqual(rc_ver, 0)

        # 5. 篡改产物文件 -> 应当检测到哈希漂移返回 1
        art_path.write_text("# Tampered\nVALUE = 999\n", encoding="utf-8")
        rc_drift = cmd_tree(args_art)
        self.assertEqual(rc_drift, 1)

        # 6. 删除产物文件 -> 应当检测到文件缺失返回 1
        art_path.unlink()
        rc_missing = cmd_tree(args_art)
        self.assertEqual(rc_missing, 1)

    def test_smart_heartbeat_and_smart_answer_with_auto_manifest(self):
        """测试极简心跳与免清单自动回执（Smart Heartbeat & Auto-Manifest）"""
        from agentbus.cli import cmd_heartbeat, cmd_answer

        # 1. 给 leaf-1 加上 expected_artifacts 并更新结构
        struct = copy.deepcopy(self.structure)
        for leaf in struct['leaves']:
            if leaf['id'] == 'leaf-1':
                leaf['expected_artifacts'] = ['成果/leaf1_out.txt']
        self.forest.replace_structure(struct)

        res = self.forest.claim_next('alpha')
        self.assertEqual(res['status'], 'claimed')

        # 2. 可以省略任务ID，但原认领票据必须由本次认领输出保存。
        ticket = captured_ticket(res['message']).to_dict()
        args_hb = argparse.Namespace(
            workspace=str(self.root),
            agent='alpha',
            id=None,
            claim_epoch=ticket['claim_epoch'],
            claim_id=ticket['claim_id'],
            note='Alpha 正在自动执行'
        )
        rc_hb = cmd_heartbeat(args_hb)
        self.assertEqual(rc_hb, 0)

        # 3. 生成产物文件
        out_file = self.root / "成果" / "leaf1_out.txt"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("Auto generated content", encoding="utf-8")

        # 4. 自动清单不代替原票据，不从其他聊天的当前任务反补票据。
        args_ans = argparse.Namespace(
            workspace=str(self.root),
            agent='alpha',
            id=None,
            claim_epoch=ticket['claim_epoch'],
            claim_id=ticket['claim_id'],
            body="极简一键交付完成",
            body_file=None,
            expected_body_sha256=None,
            artifacts_file=None,
            auto_manifest=True,
            artifacts=None,
            failed=False,
            result_to=None
        )
        rc_ans = cmd_answer(args_ans)
        self.assertEqual(rc_ans, 0)

        # 5. 校验该任务已顺利进入 delivered 终态
        snap = self.forest.snapshot()
        l1 = next(l for l in snap['leaves'] if l['id'] == 'leaf-1')
        self.assertEqual(l1['state'], 'delivered')
        self.assertEqual(len(l1['artifacts']), 1)
        self.assertEqual(l1['artifacts'][0]['path'], '成果/leaf1_out.txt')

    def test_tree_artifacts_export_and_summary_report(self):
        """测试全产品一键导出打包与交付白皮书生成 (--export 和 --summary-file)"""
        import zipfile
        from agentbus.cli import cmd_tree

        # 交付一个节点并生成产物
        res = self.forest.claim_next('alpha')
        msg = res['message']
        ticket = captured_ticket(msg).to_dict()
        out_file = self.root / "成果" / "final_prod.bin"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(b"\x00\x01\x02\x03\x04")

        # 交付
        from agentbus.cli import cmd_answer
        args_ans = argparse.Namespace(
            workspace=str(self.root), agent='alpha', id=msg.id,
            claim_epoch=ticket['claim_epoch'], claim_id=ticket['claim_id'],
            body="交付 final_prod", body_file=None, expected_body_sha256=None,
            artifacts_file=None, auto_manifest=False, artifacts="成果/final_prod.bin",
            failed=False, result_to=None
        )
        cmd_answer(args_ans)

        for _ in range(2):
            remaining = self.forest.claim_next('alpha')['message']
            remaining_ticket = captured_ticket(remaining)
            self.bus.heartbeat('alpha', remaining, remaining_ticket, note='verified fixture')
            self.bus.commit_result('alpha', remaining, remaining_ticket,
                                   body='No file artifact required', exit_code=0)

        # 执行 tree artifacts 导出到 zip 和 markdown
        zip_out = self.root / "dist" / "release_bundle.zip"
        md_out = self.root / "成果" / "DELIVERY_REPORT.md"
        args_art = argparse.Namespace(
            workspace=str(self.root), action='artifacts',
            export=str(zip_out), summary_file=str(md_out)
        )
        rc = cmd_tree(args_art)
        self.assertEqual(rc, 0)

        # 验证 zip 包存在且解包后包含产物
        self.assertTrue(zip_out.is_file())
        with zipfile.ZipFile(zip_out, 'r') as zf:
            self.assertIn("成果/final_prod.bin", zf.namelist())

        # 验证白皮书存在且包含章节和产物
        self.assertTrue(md_out.is_file())
        md_content = md_out.read_text(encoding='utf-8')
        self.assertIn("产品全景交付白皮书与产物清单", md_content)
        self.assertIn("成果/final_prod.bin", md_content)

    def test_claim_next_with_wait_polling(self):
        """测试 claim-next --wait 短轮询等待下游依赖动态解锁并成功接单"""
        import threading
        import time
        from agentbus.cli import cmd_claim_next, cmd_answer

        # 1. Alpha 抢单 leaf-1 (前驱任务)
        res1 = self.forest.claim_next('alpha')
        self.assertEqual(res1['status'], 'claimed')
        msg1 = res1['message']

        # 2. 此时 leaf-3 依赖 leaf-1，而 leaf-2 是独立就绪的。先把 leaf-2 消耗掉，使得只剩下被 blocked 的 leaf-3
        res2 = self.forest.claim_next('beta')
        self.assertEqual(res2['status'], 'claimed')
        t_alpha = captured_ticket(msg1)
        t_beta = captured_ticket(res2['message'])

        # 现在全树没有就绪任务（leaf-3 依赖 leaf-1）。若无 wait，抢单返回 no_available_tasks (0)
        args_no_wait = argparse.Namespace(workspace=str(self.root), agent='codex', wait=0.0)
        # Note: codex 未在 enabled_workers，换用已登记但无任务的工位或测试线程
        # 让 beta 交付 leaf-2 先变成空闲
        args_ans_beta = argparse.Namespace(
            workspace=str(self.root), agent='beta', id=None,
            claim_epoch=t_beta.claim_epoch, claim_id=t_beta.claim_id,
            body="交付 beta", body_file=None, expected_body_sha256=None,
            artifacts_file=None, auto_manifest=False, artifacts="成果/beta.bin",
            failed=False, result_to=None
        )
        (self.root / "成果" / "beta.bin").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "成果" / "beta.bin").write_bytes(b"beta")
        cmd_answer(args_ans_beta)

        # 此时 beta 闲置，但仅剩的 leaf-3 受阻于 alpha 的 leaf-1
        # 后台线程在 0.5s 后交付 alpha 的任务
        def deliver_alpha_later():
            time.sleep(0.5)
            (self.root / "成果" / "alpha.bin").write_bytes(b"alpha")
            args_ans_alpha = argparse.Namespace(
                workspace=str(self.root), agent='alpha', id=None,
                claim_epoch=t_alpha.claim_epoch, claim_id=t_alpha.claim_id,
                body="交付 alpha", body_file=None, expected_body_sha256=None,
                artifacts_file=None, auto_manifest=False, artifacts="成果/alpha.bin",
                failed=False, result_to=None
            )
            cmd_answer(args_ans_alpha)

        t = threading.Thread(target=deliver_alpha_later)
        t.start()

        # Beta 执行带 --wait 3.0 的 claim-next，应在约 0.5-1.0s 内自动等待并成功抢到刚刚解锁的 leaf-3
        args_wait = argparse.Namespace(workspace=str(self.root), agent='beta', wait=3.0)
        t_start = time.time()
        rc = cmd_claim_next(args_wait)
        elapsed = time.time() - t_start
        t.join()

        self.assertEqual(rc, 0)
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(len(self.bus.processing('beta')), 1)


if __name__ == '__main__':
    unittest.main()
