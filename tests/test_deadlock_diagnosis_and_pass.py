import os
import sys
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

# 添加 agentbus 到路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentbus.config import load
from agentbus.bus import Bus
from agentbus.task_tree import TaskForest
from agentbus.delivery_verify import verify_artifacts
from agentbus.web_bridge import WebBridge
from agentbus.mcp_server import AgentBusMcp


class TestDeadlockDiagnosisAndPass(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="agentbus-deadlock-test-")
        self.ws = Path(self.temp_dir)
        # 初始化控制目录与最小配置
        ctrl = self.ws / ".agentbus"
        ctrl.mkdir(parents=True, exist_ok=True)
        config_data = {
            "version": 1,
            "project_name": "test-deadlock",
            "workers": {
                "codex": {"enabled": True},
                "alpha": {"enabled": True},
                "beta": {"enabled": True}
            },
            "director": "codex",
            "communication_mode": "file",
            "assignment_mode": "autonomous"
        }
        (ctrl / "config.json").write_text(json.dumps(config_data), encoding="utf-8")
        self.cfg = load(self.ws)
        self.forest = TaskForest(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deadlock_diagnosis_and_admin_pass(self):
        # 1. 构建一个具备单点卡阻 + 级联下游的任务树结构
        tree_struct = {
            "schema_version": 1,
            "roots": [{"id": "ROOT", "parent_id": None, "title": "根目标", "goal": "根"}],
            "branches": [{"id": "BR1", "parent_id": "ROOT", "title": "分支1"}],
            "leaves": [
                {
                    "id": "TASK-L1",
                    "parent_id": "BR1",
                    "title": "阶段一核心底座",
                    "goal": "完成底座",
                    "dependencies": [],
                    "expected_artifacts": ["src/base.py"]
                },
                {
                    "id": "TASK-L2",
                    "parent_id": "BR1",
                    "title": "阶段二业务功能",
                    "goal": "完成功能",
                    "dependencies": ["TASK-L1"],
                    "expected_artifacts": ["src/feature.py"]
                },
                {
                    "id": "TASK-L3",
                    "parent_id": "BR1",
                    "title": "阶段三集成发布",
                    "goal": "完成集成",
                    "dependencies": ["TASK-L2"],
                    "expected_artifacts": ["dist/release.zip"]
                }
            ]
        }
        self.forest.replace_structure(tree_struct)

        res = self.forest.claim_next("alpha")
        self.assertEqual(res["status"], "claimed")
        self.assertEqual(res["leaf"]["id"], "TASK-L1")

        res_beta = self.forest.claim_next("beta")
        self.assertEqual(res_beta["status"], "no_available_tasks")
        self.assertFalse(res_beta.get("diagnostics", {}).get("is_deadlocked", False))

        bus = Bus(self.ws / ".agentbus")
        for p in bus.dirs("alpha")["processing"].glob("*.json"):
            p.unlink()

        diag = self.forest.diagnose_deadlock()
        self.assertFalse(diag["is_deadlocked"])

        passed_leaf = self.forest.pass_leaf("TASK-L1", "人类管理员确认已验收底座")
        self.assertIn("admin_override", passed_leaf)
        self.assertEqual(passed_leaf["admin_override"]["reason"], "人类管理员确认已验收底座")

        snap_after = self.forest.snapshot()
        l1_view = next(l for l in snap_after["leaves"] if l["id"] == "TASK-L1")
        self.assertEqual(l1_view["state"], "admin_passed")
        self.assertIn("【管理员放行】", l1_view["results"])

        l2_view = next(l for l in snap_after["leaves"] if l["id"] == "TASK-L2")
        self.assertTrue(l2_view["claimable"])

        res_beta_claim = self.forest.claim_next("beta")
        self.assertEqual(res_beta_claim["status"], "claimed")
        self.assertEqual(res_beta_claim["leaf"]["id"], "TASK-L2")

    def test_concrete_artifact_verification_feedback(self):
        test_file = self.ws / "sample.txt"
        test_file.write_text("hello world\n", encoding="utf-8")
        
        fake_entries = [
            {
                "path": "sample.txt",
                "bytes": 9999,
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
            }
        ]
        ok, problems = verify_artifacts(self.ws, fake_entries)
        self.assertFalse(ok)
        self.assertGreater(len(problems), 0)
        prob = problems[0]
        self.assertEqual(prob["code"], "SHA_MISMATCH")
        self.assertIn("期望SHA: 00000000...", prob["detail"])
        self.assertIn("实际SHA:", prob["detail"])
        self.assertEqual(prob["expected_sha256"], "0000000000000000000000000000000000000000000000000000000000000000")

    def test_deadlock_detection_with_quarantined_root(self):
        tree_struct = {
            "schema_version": 1,
            "roots": [{"id": "ROOT", "parent_id": None, "title": "根", "goal": "根"}],
            "branches": [{"id": "BR1", "parent_id": "ROOT", "title": "分支1"}],
            "leaves": [
                {
                    "id": "TASK-BLOCKED-ROOT",
                    "parent_id": "BR1",
                    "title": "卡死根任务",
                    "goal": "卡死目标",
                    "dependencies": [],
                    "expected_artifacts": ["src/missing.py"]
                },
                {
                    "id": "TASK-DOWNSTREAM-1",
                    "parent_id": "BR1",
                    "title": "下游任务1",
                    "goal": "目标1",
                    "dependencies": ["TASK-BLOCKED-ROOT"],
                    "expected_artifacts": ["src/out1.py"]
                },
                {
                    "id": "TASK-DOWNSTREAM-2",
                    "parent_id": "BR1",
                    "title": "下游任务2",
                    "goal": "目标2",
                    "dependencies": ["TASK-DOWNSTREAM-1"],
                    "expected_artifacts": ["src/out2.py"]
                }
            ]
        }
        self.forest.replace_structure(tree_struct)

        snap = self.forest.snapshot()
        for leaf in snap["leaves"]:
            if leaf["id"] == "TASK-BLOCKED-ROOT":
                leaf["state"] = "awaiting_receipt"
                leaf["claimable"] = False
                leaf["evidence_error"] = "任务与原始作业意图不一致；请核对原记录。"
            else:
                leaf["state"] = "blocked"
                leaf["claimable"] = False

        diag = self.forest.diagnose_deadlock(snap)
        self.assertTrue(diag["is_deadlocked"])
        self.assertEqual(len(diag["root_blockers"]), 1)
        rb = diag["root_blockers"][0]
        self.assertEqual(rb["id"], "TASK-BLOCKED-ROOT")
        self.assertEqual(rb["evidence_error"], "任务与原始作业意图不一致；请核对原记录。")
        self.assertEqual(rb["blocked_downstream_count"], 2)

    def test_branch_inherited_dependency_deadlock_and_reset(self):
        """验证跨分支继承依赖 (effective_dependencies) 的死锁下游计数，以及 reset_leaf 重置超时节点。"""
        tree_struct = {
            "schema_version": 1,
            "roots": [{"id": "ROOT", "parent_id": None, "title": "根", "goal": "根"}],
            "branches": [
                {"id": "BR1", "parent_id": "ROOT", "title": "阶段一分支", "dependencies": []},
                {"id": "BR2", "parent_id": "ROOT", "title": "阶段二分支", "dependencies": ["L-BR1"]},
            ],
            "leaves": [
                {
                    "id": "L-BR1",
                    "parent_id": "BR1",
                    "section_no": "1.1",
                    "title": "阶段一底座",
                    "goal": "底座",
                    "dependencies": [],
                    "expected_artifacts": [],
                },
                {
                    "id": "L-BR2-A",
                    "parent_id": "BR2",
                    "section_no": "2.1",
                    "title": "阶段二模块A",
                    "goal": "模块A",
                    "dependencies": [],
                    "expected_artifacts": [],
                },
                {
                    "id": "L-BR2-B",
                    "parent_id": "BR2",
                    "section_no": "2.2",
                    "title": "阶段二模块B",
                    "goal": "模块B",
                    "dependencies": ["L-BR2-A"],
                    "expected_artifacts": [],
                },
            ],
        }
        self.forest.replace_structure(tree_struct)

        # 认领 L-BR1 并将其 deadline_ts 设为已过期，然后释放 claim，模拟超时受阻
        claimed = self.forest.claim_next("alpha")
        self.assertEqual(claimed["status"], "claimed")
        msg_id = claimed["message"].id
        bus = Bus(self.ws / ".agentbus")
        proc_file = bus.dirs("alpha")["processing"] / f"{msg_id}.json"
        m_data = json.loads(proc_file.read_text(encoding="utf-8"))
        m_data["deadline_ts"] = time.time() - 100
        proc_file.write_text(json.dumps(m_data, ensure_ascii=False), encoding="utf-8")

        # 死锁诊断：BR2 下面的两个叶子继承了对 BR1 的依赖，因此 L-BR1 阻断下游数应为 2
        snap = self.forest.snapshot()
        for l in snap["leaves"]:
            if l["id"] == "L-BR1":
                l["state"] = "blocked"
                l["claimable"] = False
                l["evidence_error"] = "该任务已超过自己的时间预算"
        diag = self.forest.diagnose_deadlock(snap)
        self.assertTrue(diag["is_deadlocked"])
        self.assertEqual(len(diag["root_blockers"]), 1)
        self.assertEqual(diag["root_blockers"][0]["id"], "L-BR1")
        self.assertEqual(diag["root_blockers"][0]["blocked_downstream_count"], 2)

        # 通过 WebBridge 和 MCPServer 测试 diagnose_deadlock / reset_leaf / pass_leaf
        bridge = WebBridge(self.ws)
        diag_res = bridge.call("diagnose_deadlock", {})
        self.assertTrue(diag_res["ok"])
        self.assertTrue(diag_res["data"]["is_deadlocked"])

        before_task = proc_file.read_bytes()
        before_tree = (self.ws / ".agentbus/task-tree.json").read_bytes()
        reset_res = bridge.call("reset_leaf", {"leaf_id": "1.1", "reason": "重置超时任务",
                                                "owner_confirmed_inactive": True})
        # An expired/changed original must never be bypassed by writing blocked directly.
        self.assertFalse(reset_res["ok"], reset_res)
        self.assertEqual(proc_file.read_bytes(), before_task)
        self.assertEqual((self.ws / ".agentbus/task-tree.json").read_bytes(), before_tree)

        # 测试 WebBridge pass_leaf 与 MCP Server
        pass_res = bridge.call("pass_leaf", {"leaf_id": "1.1", "reason": "管理员直接放行"})
        self.assertTrue(pass_res["ok"])
        self.assertTrue(pass_res["data"]["ok"])
        snap_passed = self.forest.snapshot()
        l_br1_passed = next(l for l in snap_passed["leaves"] if l["id"] == "L-BR1")
        self.assertEqual(l_br1_passed["state"], "admin_passed")

        mcp = AgentBusMcp(self.ws)
        mcp_claim = mcp.claim_next(agent="beta")
        self.assertIn("2.1", mcp_claim)
        mcp_diag = mcp.diagnose_deadlock()
        self.assertIn("is_deadlocked", mcp_diag)
        captured = json.loads(mcp_claim)
        active = self.forest.bus.find_message(captured["task_id"])
        mcp_reset = mcp.reset_leaf(leaf_id="2.1", reason="MCP重置测试", owner_confirmed_inactive=True,
                                   expected_task_id=active.id, expected_owner="beta", expected_claim_epoch=active.claim_epoch)
        self.assertIn("L-BR2-A", mcp_reset)
        mcp_pass = mcp.pass_leaf(leaf_id="2.1", reason="MCP放行测试")
        self.assertIn("L-BR2-A", mcp_pass)

    def test_usability_enhancements_draft_zero_sha_gc_and_status(self):
        """验证 6 项易用性改进：认领自动生成 .reply.md 草稿、answer 免算 SHA256 一键交单、gc --include-deprecated 与 WebBridge gc_plan/gc_apply、status 合一看板。"""
        import argparse
        import io
        from contextlib import redirect_stdout
        from agentbus.cli import cmd_claim_next, cmd_answer, cmd_status

        tree_struct = {
            "schema_version": 1,
            "roots": [{"id": "ROOT", "parent_id": None, "title": "根", "goal": "根"}],
            "branches": [{"id": "BR1", "parent_id": "ROOT", "title": "分支1"}],
            "leaves": [
                {
                    "id": "TASK-EASY-1",
                    "parent_id": "BR1",
                    "section_no": "1.1",
                    "title": "极简交单测试任务",
                    "goal": "测试免算哈希与自动草稿",
                    "dependencies": [],
                    "expected_artifacts": ["src/easy1.txt"],
                },
            ],
        }
        self.forest.replace_structure(tree_struct)

        # 1. 调用 cmd_claim_next 认领任务，验证自动生成 .reply.md 草稿
        ns_claim = argparse.Namespace(
            workspace=str(self.ws),
            agent="alpha",
            wait=0,
            poll_interval=1.0,
            json=False,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_claim_next(ns_claim)
        self.assertEqual(rc, 0)
        claim_out = buf.getvalue()
        self.assertIn(".reply.md", claim_out)

        receipts_dir = self.ws / "成果" / "任务书维护" / "receipts"
        draft_files = list(receipts_dir.glob("*.reply.md"))
        self.assertEqual(len(draft_files), 1)
        draft_file = draft_files[0]
        draft_content = draft_file.read_text(encoding="utf-8")
        self.assertIn("极简交单测试任务", draft_content)

        # 准备产物文件并编辑草稿文件
        art_path = self.ws / "src" / "easy1.txt"
        art_path.parent.mkdir(parents=True, exist_ok=True)
        art_path.write_text("easy artifact content\n", encoding="utf-8")
        draft_content = '\n'.join(line for line in draft_content.splitlines() if 'AGENTBUS_DRAFT_UNFINISHED' not in line)
        draft_content = draft_content.replace('（填写实际完成内容；未完成或受阻请如实说明。）','已完成 easy1.txt 并在本用例复核物理字节。')
        draft_file.write_text(draft_content + "\n已完成 easy1.txt 编写与自测。\n", encoding="utf-8")

        # 2. 显式提供已填写、带原认领票据的草稿；不反补其他窗口的票据。
        ns_answer = argparse.Namespace(
            workspace=str(self.ws),
            caller="alpha",
            agent=None,
            id=None,
            to=None,
            title=None,
            body=None,
            body_file=draft_file.relative_to(self.ws).as_posix(),
            body_stdin=False,
            expected_body_sha256=None,
            allow_replacement_char=False,
            artifacts=None,
            auto_manifest=True,
            artifacts_manifest=None,
            tokens_in=120,
            tokens_out=340,
            cost=0.015,
            duration_ms=1500,
        )
        buf_ans = io.StringIO()
        with redirect_stdout(buf_ans):
            rc_ans = cmd_answer(ns_answer)
        self.assertEqual(rc_ans, 0)
        snap_after = self.forest.snapshot()
        leaf_after = next(l for l in snap_after["leaves"] if l["id"] == "TASK-EASY-1")
        self.assertEqual(leaf_after["state"], "delivered")

        # 3. 验证 cmd_status 输出合一看板且兼容 GBK 编码
        ns_status = argparse.Namespace(workspace=str(self.ws), all=False)
        buf_status = io.StringIO()
        with redirect_stdout(buf_status):
            rc_st = cmd_status(ns_status)
        self.assertEqual(rc_st, 0)
        st_out = buf_status.getvalue()
        self.assertIn("[任务树与全局调度看板]", st_out)
        self.assertIn("共 1 项 | 已交付 1", st_out)
        # 确保 GBK strict 编码不报错
        st_out.encode("gbk")

        # 4. 验证 WebBridge 的 gc_plan 与 gc_apply 支持清理过期 .deprecated_old_version 文件
        old_dep1 = self.ws / "成果" / "OldAgentBus1.exe.deprecated_old_version"
        old_dep2 = self.ws / "成果" / "OldAgentBus2.exe.deprecated_old_version"
        old_dep1.parent.mkdir(parents=True, exist_ok=True)
        old_dep1.write_bytes(b"OLD_BINARY_DATA_1" * 100)
        old_dep2.write_bytes(b"OLD_BINARY_DATA_2" * 100)
        old_ts = time.time() - 10 * 86400
        os.utime(old_dep1, (old_ts, old_ts))

        bridge = WebBridge(self.ws)
        plan_res = bridge.call("gc_plan", {"keep": 1, "include_deprecated": True, "include_packages": False})
        self.assertTrue(plan_res["ok"], f"gc_plan failed: {plan_res}")
        self.assertEqual(len(plan_res["data"]["delete"]), 1)

        apply_res = bridge.call("gc_apply", {"keep": 1, "include_deprecated": True, "include_packages": False, "confirmed": True})
        self.assertTrue(apply_res["ok"], f"gc_apply failed: {apply_res}")
        self.assertEqual(apply_res["data"]["removed"], 1)
        self.assertFalse(old_dep1.exists())
        self.assertTrue(old_dep2.exists())

    def test_five_open_source_patterns_integration(self):
        """全面验证 5 项成熟开源方案融合：Git Worktree 沙箱、Aider AST RepoMap、Pre-Answer 语法门禁、Beads Discover 衍生任务、Lessons Learned 经验库。"""
        import argparse
        import io
        from contextlib import redirect_stdout
        from agentbus.cli import cmd_claim_next, cmd_answer, cmd_discover, cmd_lessons, cmd_repomap, cmd_worktree
        from agentbus.open_patterns import record_lesson

        # 预先写入一个关联的 Python 文件与一条历史避坑经验，测试 RepoMap + Lessons 自动注入
        src_dir = self.ws / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "engine.py").write_text(
            "class CoreEngine:\n    def execute_pipeline(self, task_id: str, retries: int = 3) -> bool:\n        return True\n",
            encoding="utf-8",
        )
        record_lesson(
            self.ws,
            agent="codex",
            lesson="修改 src/ 目录时注意保持 UTF-8 无 BOM 编码",
            task_id="INIT-0",
            scope_paths=["src/engine.py"],
        )

        tree_struct = {
            "schema_version": 1,
            "roots": [{"id": "ROOT", "parent_id": None, "title": "根", "goal": "根"}],
            "branches": [{"id": "BR1", "parent_id": "ROOT", "title": "核心分支"}],
            "leaves": [
                {
                    "id": "TASK-OPEN-1",
                    "parent_id": "BR1",
                    "section_no": "1.1",
                    "title": "扩展 CoreEngine 模块",
                    "goal": "实现 engine 扩展",
                    "dependencies": [],
                    "expected_artifacts": ["src/engine.py"],
                },
            ],
        }
        self.forest.replace_structure(tree_struct)

        # 1. 带 --worktree 认领任务，验证同时创建沙箱并向 .reply.md 注入 RepoMap 与 Lessons Learned
        ns_claim = argparse.Namespace(
            workspace=str(self.ws),
            agent="alpha",
            wait=0,
            tier=None,
            worktree=True,
        )
        buf_claim = io.StringIO()
        with redirect_stdout(buf_claim):
            rc = cmd_claim_next(ns_claim)
        self.assertEqual(rc, 0)

        receipts_dir = self.ws / "成果" / "任务书维护" / "receipts"
        draft_file = next(receipts_dir.glob("*.reply.md"))
        draft_txt = draft_file.read_text(encoding="utf-8")
        # 验证 RepoMap 注入了 CoreEngine 和 execute_pipeline
        self.assertIn("class CoreEngine", draft_txt)
        self.assertIn("def execute_pipeline", draft_txt)
        # 验证 Lessons Learned 注入了历史避坑经验
        self.assertIn("修改 src/ 目录时注意保持 UTF-8 无 BOM 编码", draft_txt)
        # 验证 Worktree 沙箱已创建并在草稿中登记
        self.assertIn("隔离沙箱 (Worktree)", draft_txt)

        bridge = WebBridge(self.ws)
        wt_list = bridge.call("worktree_list", {})
        self.assertTrue(wt_list["ok"])
        self.assertEqual(wt_list["data"]["count"], 1)
        wt_path = Path(wt_list["data"]["worktrees"][0]["worktree_path"])
        self.assertTrue(wt_path.is_dir())

        # 2. 验证 Beads 风格任务衍生 (discover_leaf / cmd_discover)
        disc_res = bridge.call("discover_leaf", {
            "agent": "alpha",
            "from_task_id": "TASK-OPEN-1",
            "title": "修复 Engine 边界重试异常",
            "goal": "补充重试上限校验",
            "expected_artifacts": "src/retry_fix.py",
        })
        self.assertTrue(disc_res["ok"], f"discover_leaf failed: {disc_res}")
        self.assertEqual(disc_res["data"]["discovered"]["section_no"], "1.2")
        self.assertEqual(disc_res["data"]["discovered"]["discovered_from"], "TASK-OPEN-1")

        # 3. 验证交单前 Pre-Answer 静态语法门禁：先在 Worktree 沙箱写入一段带语法错误的 Python 代码
        wt_engine = wt_path / "src" / "engine.py"
        wt_engine.parent.mkdir(parents=True, exist_ok=True)
        wt_engine.write_text("def broken_syntax(:\n    pass\n", encoding="utf-8")
        draft_txt = '\n'.join(line for line in draft_txt.splitlines() if 'AGENTBUS_DRAFT_UNFINISHED' not in line)
        draft_txt = draft_txt.replace('（填写实际完成内容；未完成或受阻请如实说明。）','已在隔离沙箱实现 engine.py；语法结果由交单前门禁核验。')
        draft_file.write_text(draft_txt + "\n初次尝试提交。\n", encoding="utf-8")

        ns_answer = argparse.Namespace(
            workspace=str(self.ws),
            caller="alpha",
            agent="alpha",
            id=None,
            body=None,
            body_file=draft_file.relative_to(self.ws).as_posix(),
            expected_body_sha256=None,
            artifacts=None,
            auto_manifest=True,
            artifacts_file=None,
            failed=False,
            result_to=None,
            tokens=200,
            cost=0.02,
            lesson="交付前务必通过 AST 语法门禁检查",
            verify_cmd=None,
            skip_verify=False,
        )
        with self.assertRaises(ValueError) as ctx:
            cmd_answer(ns_answer)
        self.assertIn("Pre-Answer Gate", str(ctx.exception))
        self.assertIn("PYTHON_SYNTAX_ERROR", str(ctx.exception))

        # 4. 修复语法错误后再次交单（写入主工作区 src/engine.py），验证通过门禁并自动沉淀新 lesson
        ns_answer.body = None
        ns_answer.id = None
        wt_engine.write_text(
            "class CoreEngine:\n    def execute_pipeline(self, task_id: str, retries: int = 3) -> bool:\n        return bool(task_id)\n",
            encoding="utf-8",
        )
        buf_ans = io.StringIO()
        with redirect_stdout(buf_ans):
            rc_ans = cmd_answer(ns_answer)
        self.assertEqual(rc_ans, 0)
        self.assertIn("[经验沉淀]", buf_ans.getvalue())

        lessons_res = bridge.call("lessons_list", {"target_paths": ["src/engine.py"]})
        self.assertTrue(lessons_res["ok"])
        self.assertGreaterEqual(lessons_res["data"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
