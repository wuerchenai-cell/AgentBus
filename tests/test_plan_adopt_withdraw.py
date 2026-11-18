# -*- coding: utf-8 -*-
"""PLAN-ADOPT-WITHDRAW 专项回归测试。
验证：
1. 未评估提案必须拒绝采用；
2. 已撤回提案必须拒绝采用；
3. 受 CAS 校验和审计保护的采用入口（CLI 与 WebBridge）；
4. 采用后成功绑定 revision_id 与 content_sha256，且可幂等重复调用；
5. CAS mismatch 保护：当计划被并发更新或传入旧 revision 时拒绝覆盖；
6. 受理由保护与审计记录的撤回入口（CLI 与 WebBridge）；
7. 撤回幂等性；撤回后无法无序覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib
import io
import json
import tempfile
import unittest

from receipt_helpers import receipt_ticket
from agentbus.bus import Bus
from agentbus.cli import main
from agentbus.config import load
from agentbus.change_control import classify_change, ChangeControlError
from agentbus.project_plan import ProjectPlanError, adopt_change_record, withdraw_change_record
from agentbus.web_bridge import WebBridge


class PlanAdoptWithdrawTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.cfg = load(self.ws, create=True)
        self.bus = Bus(self.ws / '.agentbus')
        self.bus.init()

    def cli(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = main([*args, '--workspace', str(self.ws)])
        return code, stream.getvalue()

    def _setup_adopted_plan(self):
        # 创建并正式采用一个基础计划
        (self.ws / 'initial_plan.md').write_text('# 初始计划\n目标：建立第一阶段交付基线。', encoding='utf-8')
        rc, out = self.cli('plan', 'propose', '--caller', 'codex', '--file', 'initial_plan.md',
                           '--reason', '初始化计划', '--reviewers', 'workbuddy', '--request-id', 'req-1')
        self.assertEqual(rc, 0, out)
        prop = json.loads(out)
        prop_id = prop['id']

        # 构造评审结果
        task = self.bus.make('codex', 'workbuddy', 'review',
                             body=f"计划评估：SHA256: {prop['content_sha256']}",
                             result_to='codex')
        self.bus.post(task)
        self.bus.finish('workbuddy', task, 'done', ticket=receipt_ticket(task))
        res = self.bus.make('workbuddy', 'codex', 'result', body='Verdict: accept\n认可此计划。',
                            reply_to=task.id)
        self.bus.post(res)

        rc, out = self.cli('plan', 'review', '--caller', 'codex', '--proposal-id', prop_id,
                           '--task-id', task.id, '--verdict', 'accept')
        self.assertEqual(rc, 0, out)

        rc, out = self.cli('plan', 'adopt', '--caller', 'codex', '--proposal-id', prop_id,
                           '--expected-current-sha256', 'MISSING',
                           '--dispositions-json', json.dumps({'workbuddy': '已采纳'}))
        self.assertEqual(rc, 0, out)
        adopted_plan = json.loads(out)
        return adopted_plan

    def test_unassessed_proposal_cannot_be_adopted(self):
        plan = self._setup_adopted_plan()
        unassessed = {'change_id': 'CH-fake123', 'status': 'proposed'}
        (self.ws / 'unassessed.json').write_text(json.dumps(unassessed), encoding='utf-8')

        rc, out = self.cli('plan', 'adopt-change', '--caller', 'codex', '--file', 'unassessed.json')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('未评估', out)

    def test_withdrawn_proposal_cannot_be_adopted(self):
        plan = self._setup_adopted_plan()
        snapshot = {
            'adopted_revision': {'revision_id': plan['revision_id'], 'content_sha256': plan['content_sha256']},
            'taskbook': {'work_items': [], 'acceptance': []},
            'changes': {}
        }
        change = classify_change('新要求：必须支持撤回', snapshot=snapshot, now_ts=100.0)
        change['status'] = 'withdrawn'
        (self.ws / 'withdrawn.json').write_text(json.dumps(change), encoding='utf-8')

        rc, out = self.cli('plan', 'adopt-change', '--caller', 'codex', '--file', 'withdrawn.json')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('已撤回', out)

    def test_cli_adopt_and_withdraw_change_flow(self):
        plan = self._setup_adopted_plan()
        rev_id = plan['revision_id']
        rev_sha = plan['content_sha256']

        # 1. 评估新要求
        (self.ws / 'snapshot.json').write_text(json.dumps({
            'adopted_revision': {'revision_id': rev_id, 'content_sha256': rev_sha},
            'current_revision_sha256': rev_sha,
            'taskbook': {'work_items': [], 'acceptance': []},
            'changes': {}
        }), encoding='utf-8')
        rc, out = self.cli('plan', 'assess', '--file', 'snapshot.json', '--caller', 'codex',
                           '--reason', '增加用户身份验证与令牌轮换')
        self.assertEqual(rc, 0, out)
        change_rec = json.loads(out)
        cid = change_rec['change_id']
        self.assertTrue(cid.startswith('CH-'))
        (self.ws / 'change.json').write_text(json.dumps(change_rec), encoding='utf-8')

        # 2. CAS 保护测试：传递错误的 expected_revision_sha256 必须失败
        rc, out = self.cli('plan', 'adopt-change', '--caller', 'codex', '--file', 'change.json',
                           '--expected-current-sha256', '0' * 64)
        self.assertNotEqual(rc, 0, out)
        self.assertIn('CAS 不匹配', out)

        # 3. 正常采用
        rc, out = self.cli('plan', 'adopt-change', '--caller', 'codex', '--file', 'change.json',
                           '--plan-id', rev_id, '--expected-current-sha256', rev_sha)
        self.assertEqual(rc, 0, out)
        adopted_rec = json.loads(out)
        self.assertEqual(adopted_rec['status'], 'adopted')
        self.assertTrue(adopted_rec['adopted'])
        self.assertTrue(adopted_rec['may_dispatch'])
        self.assertEqual(adopted_rec['adopted_revision'], rev_id)
        self.assertEqual(adopted_rec['adopted_revision_sha256'], rev_sha)

        # 4. 幂等采用测试
        rc, out = self.cli('plan', 'adopt-change', '--caller', 'codex', '--file', 'change.json',
                           '--plan-id', rev_id, '--expected-current-sha256', rev_sha)
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)['change_id'], cid)

        # 5. 撤回测试：缺少 reason 失败
        rc, out = self.cli('plan', 'withdraw-change', '--caller', 'codex', '--change-id', cid)
        self.assertNotEqual(rc, 0, out)

        # 正常撤回
        rc, out = self.cli('plan', 'withdraw-change', '--caller', 'codex', '--change-id', cid,
                           '--reason', '业务方案已变动，此需求不再需要')
        self.assertEqual(rc, 0, out)
        withdrawn_rec = json.loads(out)
        self.assertEqual(withdrawn_rec['status'], 'withdrawn')
        self.assertFalse(withdrawn_rec['adopted'])
        self.assertFalse(withdrawn_rec['may_dispatch'])
        self.assertEqual(withdrawn_rec['withdrawal_reason'], '业务方案已变动，此需求不再需要')

        # 撤回后不可再被 adopt-change 覆盖
        rc, out = self.cli('plan', 'adopt-change', '--caller', 'codex', '--file', 'change.json')
        self.assertNotEqual(rc, 0, out)

    def test_web_bridge_adopt_and_withdraw_change(self):
        plan = self._setup_adopted_plan()
        rev_id = plan['revision_id']
        rev_sha = plan['content_sha256']

        bridge = WebBridge(self.ws)
        snapshot = {
            'adopted_revision': {'revision_id': rev_id, 'content_sha256': rev_sha},
            'current_revision_sha256': rev_sha,
            'taskbook': {'work_items': [], 'acceptance': []},
            'changes': {}
        }
        change = classify_change('集成监控指标与追踪链路', snapshot=snapshot, now_ts=200.0)
        cid = change['change_id']

        # CAS 不匹配
        res = bridge.call('adopt_change', {
            'caller': 'codex',
            'change_record': change,
            'expected_revision_id': 'r-wrong'
        })
        self.assertFalse(res['ok'])
        self.assertIn('CAS 不匹配', res['error'])

        # 成功采用
        res = bridge.call('adopt_change', {
            'caller': 'codex',
            'change_record': change,
            'expected_revision_id': rev_id,
            'expected_revision_sha256': rev_sha
        })
        self.assertTrue(res['ok'], res.get('error'))
        self.assertEqual(res['data']['status'], 'adopted')

        # 撤回必须给 reason
        res = bridge.call('withdraw_change', {
            'caller': 'codex',
            'change_id': cid,
            'reason': ''
        })
        self.assertFalse(res['ok'])

        # 成功撤回
        res = bridge.call('withdraw_change', {
            'caller': 'codex',
            'change_id': cid,
            'reason': '改为独立链路组件'
        })
        self.assertTrue(res['ok'], res.get('error'))
        self.assertEqual(res['data']['status'], 'withdrawn')
        self.assertEqual(res['data']['withdrawal_reason'], '改为独立链路组件')


if __name__ == '__main__':
    unittest.main()
