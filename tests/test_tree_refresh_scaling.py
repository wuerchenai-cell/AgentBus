"""§7.31 任务树刷新性能：等价性 + 索引不跨快照复用 的回归守卫。

本文件**不测速度**（墙钟/CPU 在同机多成员的协作机器上噪声太大，不适合做断言）。
它测的是这次优化**必须保住的两条性质**：

  1. **信箱索引与旧实现逐例等价**：成员遍历顺序、位置优先级、
     "同一 identity 只取首个存在文件"、"内容非法仍判 unreadable 不后退"全部一致；
  2. **索引不跨快照复用**（因此不存在陈旧视图）：新增消息下次快照必须可见。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.task_tree import TaskForest


class _Forest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load(Path(self.tmp.name), create=True)
        self.cfg.data["workers"].update({
            "alpha": {"enabled": True, "executor_enabled": True, "can_spawn": False, "kind": "gui"},
            "beta": {"enabled": True, "executor_enabled": True, "can_spawn": False, "kind": "gui"},
        })
        self.cfg = save(self.cfg)
        self.forest = TaskForest(self.cfg)
        self.bus = self.forest.bus
        self.bus.init()

    def tree(self, leaves):
        self.forest.replace_structure({
            "schema_version": 1,
            "roots": [{"id": "project", "parent_id": None, "title": "Project", "goal": "Ship"}],
            "branches": [{"id": "feature", "parent_id": "project", "title": "Feature"}],
            "leaves": [{"id": n, "parent_id": "feature", "title": t, "goal": "g"}
                       for n, t in leaves],
        })

    def legacy_parallel_claim(self, leaf_id, agent):
        """Inject historical multi-claim data to test read-only projection parity.

        This is not the current production claim policy, which R02 tests enforce.
        """
        with patch.object(self.bus, 'active_work', return_value=[]):
            return self.forest.claim_leaf(leaf_id, agent)


class IndexEquivalenceTests(_Forest):
    """索引取到的消息必须与逐叶扫描的结果一致。"""

    def test_index_and_legacy_scan_agree_on_every_leaf(self):
        self.tree([("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")])
        msgs = {name: self.legacy_parallel_claim(name, "beta")
                for name in ("a", "b", "c", "d")}
        self.forest.snapshot()                      # 让 a/b/c 落到 done 之前先制造差异

        index = self.forest._message_index()

        # 逐叶扫描的"旧实现"：按成员 iterdir 顺序 + 4 个位置顺序找首个存在文件
        def legacy(identity):
            if not identity:
                return None, "unassigned"
            self.forest._path_check(self.bus.agents_dir)
            for base in (list(self.bus.agents_dir.iterdir())
                         if self.bus.agents_dir.exists() else []):
                if base.name.casefold() in {".workbuddy", "do not access"}:
                    continue
                self.forest._path_check(base)
                for location in ("done", "processing", "blocked", "ready"):
                    path = base / location / (identity + ".json")
                    self.forest._path_check(path)
                    if path.is_file():
                        return (path, location, base.name)
            return None

        for identity in ("a", "b", "c", "d"):
            expected = legacy(identity)
            actual = index.get(identity)
            self.assertEqual(expected, actual,
                             "%s: 索引结果必须与逐叶扫描一致" % identity)

    def test_index_marks_unreadable_instead_of_falling_through(self):
        """内容非法的文件：判 unreadable，**不**退回后面的位置/成员。"""
        self.tree([("a", "A")])
        message = self.forest.claim_leaf("a", "beta")
        path = self.bus.agents_dir / "beta" / "processing" / (message.id + ".json")
        path.write_text("{not json", encoding="utf-8")   # 让 read 失败
        found, location = self.forest._message(message.id, self.forest._message_index())
        self.assertIsNone(found)
        self.assertEqual("unreadable", location)

    def test_index_ignores_non_json_and_directories(self):
        self.tree([("a", "A")])
        folder = self.bus.agents_dir / "beta" / "processing"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "note.txt").write_text("x", encoding="utf-8")
        self.assertEqual([], [k for k in self.forest._message_index()
                              if k.startswith("note")])

    def test_duplicate_identity_prefers_the_same_file_as_legacy_scan(self):
        """同一 identity 出现在多个位置时，索引必须与逐叶扫描选**同一个**文件。

        这条用例专门守"首个存在优先"的优先级：若索引改成"后者覆盖前者"，
        信箱位置优先级会被悄悄改变（例如 done 被 processing 顶掉），
        从而让交付状态错判。
        """
        self.tree([("a", "A")])
        message = self.forest.claim_leaf("a", "beta")
        origin = self.bus.agents_dir / "beta" / "processing" / (message.id + ".json")
        self.assertTrue(origin.is_file())
        # 在"优先级更高"的 done 里放一份同名文件
        done = self.bus.agents_dir / "beta" / "done"
        done.mkdir(parents=True, exist_ok=True)
        done.joinpath(message.id + ".json").write_text(origin.read_text(encoding="utf-8"),
                                                       encoding="utf-8")
        index = self.forest._message_index()
        hit = index[message.id]
        self.assertEqual("done", hit[1], "done 优先于 processing，索引必须取 done")
        self.assertIn(str(self.bus.agents_dir / "beta" / "done"), str(hit[0]))

    def test_private_member_directories_are_skipped(self):
        self.tree([("a", "A")])
        message = self.forest.claim_leaf("a", "beta")
        private = self.bus.agents_dir / ".workbuddy" / "ready"
        private.mkdir(parents=True, exist_ok=True)
        (private / (message.id + ".json")).write_text("{}", encoding="utf-8")
        index = self.forest._message_index()
        # 同一 id 也会在正式成员目录里存在；守卫要求的是"索引**不得**取到 .workbuddy 下的那份"
        hit = index[message.id]
        self.assertNotIn(".workbuddy", str(hit[0]),
                         "不得索引 .workbuddy 目录里的同名文件")
        self.assertEqual("beta", hit[2])


class IndexFreshnessTests(_Forest):
    """索引每次快照新建，不得跨快照复用。"""

    def test_new_message_is_visible_on_the_next_snapshot(self):
        self.tree([("a", "A"), ("b", "B")])
        first = self.forest.claim_leaf("a", "beta")
        self.forest.snapshot()
        # 同一棵树上新认领 b：下一次快照必须看得到（索引不是旧缓存）
        second = self.legacy_parallel_claim("b", "beta")
        view = self.forest.snapshot()
        states = {leaf["id"]: leaf["state"] for leaf in view["leaves"]}
        self.assertNotEqual("queued", states.get("b"), "新认领的叶子必须出现在新快照里")
        self.assertTrue(all(leaf["conversation_id"] for leaf in view["leaves"]
                            if leaf["id"] in ("a", "b")))

    def test_index_is_rebuilt_each_snapshot(self):
        self.tree([("a", "A")])
        message = self.forest.claim_leaf("a", "beta")
        before = self.forest._message_index()
        self.assertEqual("processing", before[message.id][1])
        # §3.10 交付门禁要求「排他锁 + 合法会话心跳溯源」齐备方可提交成果；
        # 直接调用 commit_result 会因缺心跳被熔断（PIPELINE_HEARTBEAT_MISSING），
        # 故先按生产路径上报一次显式心跳。
        ticket = captured_ticket(message)
        self.bus.heartbeat(message.to, message, ticket, note="index freshness fixture")
        self.bus.commit_result(message.to, message, ticket,
                               body="done", artifacts=[], exit_code=0)
        after = self.forest._message_index()
        # 同一对象绝不共享：commit 后位置从 processing 变成 done
        self.assertEqual("done", after[message.id][1])

    def test_snapshot_is_stable_across_repeated_calls(self):
        self.tree([("a", "A"), ("b", "B")])
        for name in ("a", "b"):
            self.legacy_parallel_claim(name, "beta")
        first = json.dumps(self.forest.snapshot(), sort_keys=True, ensure_ascii=False)
        second = json.dumps(self.forest.snapshot(), sort_keys=True, ensure_ascii=False)
        self.assertEqual(first, second, "同一状态的两次快照必须一致（索引不影响结果）")


class WorkspaceHoistTests(_Forest):
    """工作区根改为构造期解析后，安全守卫必须原样保留。"""

    def test_path_check_still_rejects_outside_paths(self):
        outside = Path(self.tmp.name).parent / "somewhere-else"
        with self.assertRaises(Exception):
            self.forest._path_check(outside)

    def test_path_check_still_rejects_private_dirs(self):
        private = self.forest.root / ".workbuddy"
        with self.assertRaises(Exception):
            self.forest._path_check(private)

    def test_in_project_paths_pass(self):
        self.forest._path_check(self.forest.root / "jobs")

    def test_workspace_root_is_cached_and_matches_config(self):
        self.assertEqual(self.cfg.workspace.resolve(), self.forest._workspace_root)
        self.assertEqual(self.forest._workspace_root / ".agentbus", self.forest.root)


class SnapshotParityTests(_Forest):
    """优化前后快照字段必须一致（防止索引化顺手改坏业务）。"""

    def test_leaf_fields_are_unchanged(self):
        self.tree([("a", "A"), ("b", "B")])
        for name in ("a", "b"):
            self.legacy_parallel_claim(name, "beta")
        view = self.forest.snapshot()
        for leaf in view["leaves"]:
            for key in ("state", "owner", "conversation_id", "location", "task_id",
                        "claimable", "eligible_agents", "handoff_history"):
                self.assertIn(key, leaf, "缺少字段 %s" % key)
        self.assertEqual("claimed", view["leaves"][0]["state"])


if __name__ == "__main__":
    unittest.main()
