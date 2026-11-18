# -*- coding: utf-8 -*-
"""MEMBER-ENTRY-CONSISTENCY (4.5) 专项回归测试。
验证：
1. team join 与 member add 入口行为与校验规则共享一致的 agent ID 规则；
2. 缺失参数时明确友好报错；
3. 未初始化/未配置工作空间中调用提供能执行的下一步（指引 init / add）；
4. 未加入项目的成员调用 team join 给出可执行的下一步（指引 member add）；
5. 未准备工作流 (workflow unready) 调用 team join 给出可执行的下一步（指引 workflow prepare）；
6. 正常注册 (member add) 与确认接入 (team join) 闭环流程一致。
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

from agentbus.bus import Bus
from agentbus.cli import main
from agentbus.config import load


class MemberEntryConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    def cli(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = main([*args, '--workspace', str(self.ws)])
        return code, stream.getvalue()

    def test_uninitialized_workspace_guidance(self):
        # 在完全未初始化的工作区执行 member add 与 team join
        rc1, out1 = self.cli('member', 'add', '--id', 'workbuddy')
        self.assertNotEqual(rc1, 0)
        self.assertIn('init', out1)

        rc2, out2 = self.cli('team', 'join', '--caller', 'workbuddy')
        self.assertNotEqual(rc2, 0)
        self.assertIn('init', out2)

    def test_missing_identity_arguments(self):
        # 初始化工作空间
        self.cli('init', '--minimal')

        # member add 缺 --id
        rc1, out1 = self.cli('member', 'add')
        self.assertNotEqual(rc1, 0)
        self.assertIn('--id', out1)

        # team join 缺 --caller
        rc2, out2 = self.cli('team', 'join')
        self.assertNotEqual(rc2, 0)
        self.assertIn('--caller', out2)

    def test_invalid_identity_validation_shared(self):
        self.cli('init', '--minimal')
        bad_ids = ['123invalid', 'con', 'AUX', 'has_underscore', '']
        for bad in bad_ids:
            if bad:
                rc1, out1 = self.cli('member', 'add', '--id', bad)
                self.assertNotEqual(rc1, 0)
                self.assertTrue('成员标识' in out1 or '字母' in out1)

                rc2, out2 = self.cli('team', 'join', '--caller', bad)
                self.assertNotEqual(rc2, 0)
                self.assertTrue('成员标识' in out2 or '字母' in out2)

    def test_unselected_member_join_guidance(self):
        # 仅启用协调者
        self.cli('init', '--minimal')
        # 未加入成员执行 team join，应提示其尚未加入并引导 member add
        rc, out = self.cli('team', 'join', '--caller', 'workbuddy')
        self.assertNotEqual(rc, 0)
        self.assertIn('尚未加入当前项目', out)
        self.assertIn('member add', out)

    def test_workflow_unready_join_guidance(self):
        self.cli('init', '--minimal')
        # 添加成员成功
        rc, out = self.cli('member', 'add', '--id', 'workbuddy')
        self.assertEqual(rc, 0, out)

        # 此时删除 workflow.json 模拟未保存/冲突状态
        wf_file = self.ws / '.agentbus' / 'workflow.json'
        if wf_file.exists():
            wf_file.unlink()

        rc, out = self.cli('team', 'join', '--caller', 'workbuddy')
        self.assertNotEqual(rc, 0)
        self.assertIn('workflow prepare', out)

    def test_successful_add_and_join_flow(self):
        self.cli('init', '--minimal')
        # 1. 添加成员
        rc, out = self.cli('member', 'add', '--id', 'trae')
        self.assertEqual(rc, 0, out)
        add_res = json.loads(out)
        self.assertEqual(add_res['id'], 'trae')
        self.assertTrue(add_res['saved'])
        self.assertTrue(add_res['workflow_ready'])

        # 2. 成员确认加入
        rc, out = self.cli('team', 'join', '--caller', 'trae')
        self.assertEqual(rc, 0, out)
        join_res = json.loads(out)
        self.assertEqual(join_res['member'], 'trae')
        self.assertEqual(join_res['to'], 'codex')
        self.assertIn('文件接入自报', join_res['scope'])


if __name__ == '__main__':
    unittest.main()
