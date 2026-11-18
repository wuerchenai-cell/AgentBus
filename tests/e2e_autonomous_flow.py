#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end full lifecycle verification for Autonomous Pull Scheduling.

Simulates the entire journey:
1. Initialize project with assignment_mode='autonomous'
2. Create and import product task tree (Task A -> Task B -> Task C)
3. Agent Alpha claims Task A via claim-next (Autonomous Pull)
4. Verify in-flight limit blocks duplicate pull
5. Alpha sends heartbeat
6. Alpha creates artifact, artifact manifest, receipt, and submits answer
7. Task A becomes delivered; Task B is automatically unblocked into ready pool
8. Agent Beta claims Task B via claim-next
9. Beta delivers Task B; Task C is unblocked
10. Alpha claims and delivers Task C
11. Full product tree 100% delivered!
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import argparse
import hashlib
import json
import os
import tempfile
import time

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.cli import cmd_claim, cmd_claim_next, cmd_heartbeat, cmd_answer
from agentbus.config import load, save
from agentbus.task_tree import TaskForest


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

def main():

    print("======================================================================")
    print("🚀 开始自主抢单自由调度（Autonomous Pull）端到端全流程走查")
    print("======================================================================\n")

    with tempfile.TemporaryDirectory() as temp_dir:
        ws = Path(temp_dir)
        print(f"步骤 1: 初始化工作区 -> {ws}")
        cfg = load(ws, create=True)
        data = dict(cfg.data)
        data['assignment_mode'] = 'autonomous'
        workers = data.setdefault('workers', {})
        for name in ('alpha', 'beta'):
            workers[name] = {
                'enabled': True,
                'executor_enabled': True,
                'can_spawn': False,
                'kind': 'gui',
                'transport': 'file',
                'communication_mode': 'file',
                'host_version': '1',
                'model_id': 'model-1'
            }
        cfg = save(cfg, data)
        print("  [OK] assignment_mode 成功配置为 'autonomous'\n")

        print("步骤 2: 构建产品任务树 (Task A -> Task B -> Task C)")
        forest = TaskForest(cfg)
        tree = {
            'schema_version': 1,
            'roots': [{'id': 'ROOT-PROD', 'parent_id': None, 'title': 'Product Milestone', 'goal': 'Deliver product'}],
            'branches': [{'id': 'BR-CORE', 'parent_id': 'ROOT-PROD', 'title': 'Core Features'}],
            'leaves': [
                {
                    'id': 'LEAF-A',
                    'parent_id': 'BR-CORE',
                    'title': '阶段一：核心算法底座',
                    'goal': '实现核心算法并输出 base.py',
                    'handoff': '交付 base.py',
                    'section_no': '1.1',
                    'dependencies': [],
                    'expected_artifacts': ['src/base.py']
                },
                {
                    'id': 'LEAF-B',
                    'parent_id': 'BR-CORE',
                    'title': '阶段二：业务流水线集成',
                    'goal': '基于 base.py 构建 pipeline.py',
                    'handoff': '交付 pipeline.py',
                    'section_no': '1.2',
                    'dependencies': ['LEAF-A'],
                    'expected_artifacts': ['src/pipeline.py']
                },
                {
                    'id': 'LEAF-C',
                    'parent_id': 'BR-CORE',
                    'title': '阶段三：端到端发布包交付',
                    'goal': '汇编发布成果 release.json',
                    'handoff': '交付 release.json',
                    'section_no': '1.3',
                    'dependencies': ['LEAF-B'],
                    'expected_artifacts': ['dist/release.json']
                }
            ]
        }
        forest.replace_structure(tree)
        print("  [OK] 任务树成功导入，初始依赖拓扑建立完成\n")

        print("步骤 3: Agent Alpha 自主抢单 (claim-next)")
        args_alpha = argparse.Namespace(workspace=str(ws), agent='alpha')
        rc = cmd_claim_next(args_alpha)
        assert rc == 0, f"claim-next failed with rc={rc}"
        bus = Bus(ws / ".agentbus")
        alpha_proc = bus.processing('alpha')
        assert len(alpha_proc) == 1, "Alpha should have exactly 1 processing task"
        msg_a = bus.find_message(alpha_proc[0].stem)
        print(f"  [OK] Alpha 成功抢单 {msg_a.id} [{msg_a.title}]\n")

        print("步骤 4: 验证单工位在途上限 (In-flight limit = 1)")
        rc_dup = cmd_claim_next(args_alpha)
        assert rc_dup == 1, "Duplicate claim should be blocked"
        print("  [OK] Alpha 再次抢单被成功阻断，防止超额囤单\n")

        print("步骤 5: Alpha 维持极简心跳 (smart heartbeat - 省略 id 与票据参数)")
        args_hb = argparse.Namespace(
            workspace=str(ws), agent='alpha', id=None,
            claim_epoch=None, claim_id=None,
            note='Alpha 正在编写 base.py'
        )
        rc_hb = cmd_heartbeat(args_hb)
        assert rc_hb == 0, "Heartbeat failed"
        print("  [OK] Alpha 活跃心跳维持正常（自动绑定在途任务与有效票据）\n")

        print("步骤 6: Alpha 生产产物并使用一键回执交付 (answer --auto-manifest)")
        src_dir = ws / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        base_file = src_dir / "base.py"
        base_file.write_text("# Core Algorithm Implementation\ndef compute(): return 42\n", encoding="utf-8")
        docs_dir = ws / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)

        args_ans_a = argparse.Namespace(
            workspace=str(ws), agent='alpha', id=None,
            claim_epoch=None, claim_id=None,
            body="已完成 base.py 核心算法实现与自测", body_file=None, expected_body_sha256=None,
            artifacts_file=None, auto_manifest=True, artifacts=None, failed=False, result_to=None
        )
        rc_ans_a = cmd_answer(args_ans_a)
        assert rc_ans_a == 0, f"Alpha answer failed: rc={rc_ans_a}"
        print("  [OK] 阶段一回执免清单极简交付完成（产物与哈希由系统自动生成签署）\n")

        print("步骤 7: 检查下游状态 —— 阶段二自动解除阻塞进入就绪池")
        snap = forest.snapshot()
        leaf_b = next(l for l in snap['leaves'] if l['id'] == 'LEAF-B')
        assert leaf_b['claimable'] is True, "LEAF-B should be claimable now"
        print(f"  [OK] 阶段二状态: claimable={leaf_b['claimable']}, prerequisites_satisfied={leaf_b['prerequisites_satisfied']}\n")

        print("步骤 8: Agent Beta 自主抢单阶段二 (claim-next)")
        args_beta = argparse.Namespace(workspace=str(ws), agent='beta')
        rc_b = cmd_claim_next(args_beta)
        assert rc_b == 0, "Beta claim-next failed"
        beta_proc = bus.processing('beta')
        msg_b = bus.find_message(beta_proc[0].stem)
        print(f"  [OK] Beta 成功接单 {msg_b.id} [{msg_b.title}]\n")

        print("步骤 9: Beta 生产并交付阶段二产物")
        pipe_file = src_dir / "pipeline.py"
        pipe_file.write_text("import base\nprint(base.compute())\n", encoding="utf-8")
        pipe_bytes = pipe_file.stat().st_size
        pipe_sha = hashlib.sha256(pipe_file.read_bytes()).hexdigest()

        manifest_b = [{'path': 'src/pipeline.py', 'bytes': pipe_bytes, 'sha256': pipe_sha}]
        manifest_b_path = docs_dir / "manifest_b.json"
        manifest_b_path.write_text(json.dumps(manifest_b, indent=2, ensure_ascii=False), encoding="utf-8")

        receipt_b = docs_dir / "receipt_b.md"
        receipt_b.write_text("# 阶段二交付回执\n已完成 pipeline.py 集成。\n", encoding="utf-8")
        receipt_b_sha = hashlib.sha256(receipt_b.read_bytes()).hexdigest()

        ticket_b = captured_ticket(msg_b).to_dict()
        args_ans_b = argparse.Namespace(
            workspace=str(ws), agent='beta', id=msg_b.id,
            claim_epoch=ticket_b['claim_epoch'], claim_id=ticket_b['claim_id'],
            body=None, body_file="docs/receipt_b.md", expected_body_sha256=receipt_b_sha,
            artifacts_file="docs/manifest_b.json", failed=False, result_to=None
        )
        rc_ans_b = cmd_answer(args_ans_b)
        assert rc_ans_b == 0, f"Beta answer failed: rc={rc_ans_b}"
        print("  [OK] 阶段二回执正式交付\n")

        print("步骤 10: Alpha 再次自主抢单阶段三 (claim-next) 并交付")
        rc_c = cmd_claim_next(args_alpha)
        assert rc_c == 0, "Alpha claim-next for task C failed"
        msg_c = bus.find_message(bus.processing('alpha')[0].stem)
        print(f"  [OK] Alpha 成功抢单 {msg_c.id} [{msg_c.title}]")

        dist_dir = ws / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        rel_file = dist_dir / "release.json"
        rel_file.write_text(json.dumps({'version': '1.0.0', 'status': 'ready'}), encoding='utf-8')
        rel_bytes = rel_file.stat().st_size
        rel_sha = hashlib.sha256(rel_file.read_bytes()).hexdigest()

        manifest_c = [{'path': 'dist/release.json', 'bytes': rel_bytes, 'sha256': rel_sha}]
        manifest_c_path = docs_dir / "manifest_c.json"
        manifest_c_path.write_text(json.dumps(manifest_c, indent=2, ensure_ascii=False), encoding="utf-8")

        receipt_c = docs_dir / "receipt_c.md"
        receipt_c.write_text("# 阶段三交付回执\n发布包已生成。\n", encoding="utf-8")
        receipt_c_sha = hashlib.sha256(receipt_c.read_bytes()).hexdigest()

        ticket_c = captured_ticket(msg_c).to_dict()
        args_ans_c = argparse.Namespace(
            workspace=str(ws), agent='alpha', id=msg_c.id,
            claim_epoch=ticket_c['claim_epoch'], claim_id=ticket_c['claim_id'],
            body=None, body_file="docs/receipt_c.md", expected_body_sha256=receipt_c_sha,
            artifacts_file="docs/manifest_c.json", failed=False, result_to=None
        )
        rc_ans_c = cmd_answer(args_ans_c)
        assert rc_ans_c == 0, f"Alpha answer C failed: rc={rc_ans_c}"
        print("  [OK] 阶段三最终发布回执交付完成\n")

        print("步骤 11: 验证全产品任务树状态")
        final_snap = forest.snapshot()
        all_delivered = all(l['state'] == 'delivered' for l in final_snap['leaves'])
        assert all_delivered, "All leaves must be delivered"
        print("  [OK] 初始全树 3/3 项任务全部达成 DELIVERED 终态！\n")

        print("步骤 12: 动态追加需求叶子任务 (tree add)")
        from agentbus.cli import cmd_tree
        args_add = argparse.Namespace(
            workspace=str(ws), action='add',
            file=None, title='阶段四：用户手册与部署清单',
            goal='编写手册并输出 docs/USER_GUIDE.md',
            dependencies='1.3',
            artifacts='docs/USER_GUIDE.md',
            branch=None
        )
        rc_add = cmd_tree(args_add)
        assert rc_add == 0, f"tree add failed: rc={rc_add}"
        print("  [OK] 新任务动态追加成功，挂载到主干分支\n")

        print("步骤 13: Beta 自主抢单并交付新追加的任务")
        rc_d = cmd_claim_next(args_beta)
        assert rc_d == 0, "Beta claim-next for task D failed"
        msg_d = bus.find_message(bus.processing('beta')[0].stem)
        print(f"  [OK] Beta 成功抢单新任务 {msg_d.id} [{msg_d.title}]")

        guide_file = ws / "docs" / "USER_GUIDE.md"
        guide_file.write_text("# User Guide\nHow to deploy the release.\n", encoding="utf-8")
        guide_bytes = guide_file.stat().st_size
        guide_sha = hashlib.sha256(guide_file.read_bytes()).hexdigest()

        manifest_d = [{'path': 'docs/USER_GUIDE.md', 'bytes': guide_bytes, 'sha256': guide_sha}]
        manifest_d_path = docs_dir / "manifest_d.json"
        manifest_d_path.write_text(json.dumps(manifest_d, indent=2, ensure_ascii=False), encoding="utf-8")

        receipt_d = docs_dir / "receipt_d.md"
        receipt_d.write_text("# 阶段四交付回执\n手册已交付。\n", encoding="utf-8")
        receipt_d_sha = hashlib.sha256(receipt_d.read_bytes()).hexdigest()

        ticket_d = captured_ticket(msg_d).to_dict()
        args_ans_d = argparse.Namespace(
            workspace=str(ws), agent='beta', id=msg_d.id,
            claim_epoch=ticket_d['claim_epoch'], claim_id=ticket_d['claim_id'],
            body=None, body_file="docs/receipt_d.md", expected_body_sha256=receipt_d_sha,
            artifacts_file="docs/manifest_d.json", failed=False, result_to=None
        )
        rc_ans_d = cmd_answer(args_ans_d)
        assert rc_ans_d == 0, f"Beta answer D failed: rc={rc_ans_d}"
        print("  [OK] 阶段四手册回执交付完成\n")

        print("步骤 14: 全产品输出完整性核验、一键打包归档与白皮书生成 (tree artifacts)")
        pkg_zip = ws / "dist" / "product_bundle.zip"
        white_paper = ws / "成果" / "DELIVERY_WHITEPAPER.md"
        args_art = argparse.Namespace(
            workspace=str(ws), action='artifacts',
            export=str(pkg_zip), summary_file=str(white_paper)
        )
        rc_art = cmd_tree(args_art)
        assert rc_art == 0, f"tree artifacts verification failed: rc={rc_art}"
        assert pkg_zip.is_file(), "Product bundle zip must be generated"
        assert white_paper.is_file(), "Delivery whitepaper markdown must be generated"
        print("  [OK] 全树产出 4/4 项交付物 100% 吻合，并已成功导出 Zip 归档包与交付白皮书！\n")

        print("步骤 15: 产物防篡改与漂移拦截测试")
        guide_file.write_text("# Corrupted User Guide\n", encoding="utf-8")
        rc_drift = cmd_tree(args_art)
        assert rc_drift == 1, "tree artifacts should detect hash drift"
        # 恢复
        guide_file.write_text("# User Guide\nHow to deploy the release.\n", encoding="utf-8")
        rc_recover = cmd_tree(args_art)
        assert rc_recover == 0, "Recovery verification should pass"
        print("  [OK] 产物防篡改与哈希校验拦截验证通过\n")

    print("\n======================================================================")
    print("全生命周期闭环验证通过：从任务创建、自由抢单、执行交付到全产品核验顺畅通关！")
    print("======================================================================")

if __name__ == '__main__':
    main()
