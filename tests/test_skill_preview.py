# -*- coding: utf-8 -*-
"""Task 2 修正反例测试：可信摘要、维护者边界与执行固定。

来源：Trae（平级成员），2026-10-05，任务 m-1791170402277-51f640
（job j-cc56fdf40e0c01743b077f4793e34447）。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。

先写反例（红），再最小修复（绿）。只读纯函数：不访问磁盘、不执行脚本、不安装、不读私有配置。
覆盖五项复核缺陷：(1) 严格三字段清单并重算 canonical digest；(2) 去掉旧 SKILL.md-only
skill_sha256 别名；(3) 记录宿主/作用域/路线须一致，丢失 target 不得 recorded；
(4) 当前无固定执行载体，execution_fixed 恒 False；(5) 畸形 active_task_ids 不崩、不误拆。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import copy
import hashlib
import json
import unittest

from agentbus.skill_preview import build_sync_preview

D1 = "a" * 64
D2 = "b" * 64
D3 = "c" * 64


def canonical_digest(files):
    """与 Task1 相同的契约：三字段清单的 canonical JSON 原始字节 SHA256。"""
    payload = {"schema_version": 1, "files": files}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def pkg(file_sha="0" * 64, name="demo", total=10, path="SKILL.md", files=None):
    """Task1 inspect_package 的最小合法输出（摘要由清单重算，不是凭空值）。"""
    entries = files if files is not None else [
        {"path": path, "bytes": total, "sha256": file_sha}]
    summed = 0
    for entry in entries:
        if isinstance(entry, dict) and type(entry.get("bytes")) is int:
            summed += entry["bytes"]
    return {
        "schema_version": 1,
        "source": "skills/" + name,
        "name": name,
        "files": entries,
        "total_bytes": summed,
        "content_digest": canonical_digest(entries),
        "digest_covers": "paths_and_raw_bytes_only",
        "warnings": [],
    }


def record(digest, owner="agentbus", *, host="codex", scope="project",
           route="desktop", schema_version=1, **extra):
    value = {"schema_version": schema_version, "owner": owner,
             "before_content_digest": digest, "host": host,
             "scope": scope, "route": route}
    value.update(extra)
    return value


PA = pkg(D1)
PB = pkg(D2)
PC = pkg(D3)
DA, DB, DC = PA["content_digest"], PB["content_digest"], PC["content_digest"]


def preview(source, target, rec, *, host="codex", scope="project",
            route="desktop", active_task_ids=()):
    return build_sync_preview(source, target, rec, host=host, scope=scope,
                              route=route, active_task_ids=active_task_ids)


class OwnerUnknownTests(unittest.TestCase):
    def test_existing_target_without_record_conflicts(self):
        out = preview(PA, PA, None)
        self.assertEqual(out["action"], "conflict")
        self.assertIn("OWNER_UNKNOWN", out["reason_codes"])
        self.assertEqual(out["owner"], "unknown")
        self.assertIs(out["write_permitted"], False)

    def test_same_content_still_gains_no_right(self):
        out = preview(PA, PA, None)
        self.assertEqual(out["action"], "conflict")
        self.assertIn("OWNER_UNKNOWN", out["reason_codes"])
        self.assertIs(out["write_permitted"], False)
        self.assertIs(out["delete_permitted"], False)


class AgentbusRecordTests(unittest.TestCase):
    def test_update_candidate_when_record_matches_target(self):
        out = preview(PB, PA, record(DA))
        self.assertEqual(out["action"], "update")
        self.assertEqual(out["owner"], "agentbus")
        self.assertIs(out["write_permitted"], False)

    def test_unchanged_when_digests_equal(self):
        out = preview(PA, PA, record(DA))
        self.assertEqual(out["action"], "unchanged")

    def test_target_modified_conflicts(self):
        out = preview(PB, PC, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("TARGET_MODIFIED", out["reason_codes"])

    def test_same_content_does_not_grant_delete_right(self):
        out = preview(PA, PA, record(DA))
        self.assertIs(out["delete_permitted"], False)
        self.assertIn("DELETE_RIGHT_NOT_GRANTED", out["reason_codes"])


class RecordIdentityTests(unittest.TestCase):
    """缺陷 2：旧 SKILL.md-only 摘要不得当完整目录摘要。"""

    def test_skill_sha256_alias_is_not_accepted(self):
        rec = {"schema_version": 1, "owner": "agentbus", "skill_sha256": DA}
        out = preview(PB, PA, rec)
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_RECORD", out["reason_codes"])

    def test_record_without_before_content_digest_is_rejected(self):
        rec = {"schema_version": 1, "owner": "agentbus"}
        out = preview(PB, PA, rec)
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_RECORD", out["reason_codes"])

    def test_ambiguous_content_digest_alias_is_not_accepted(self):
        rec = {"schema_version": 1, "owner": "agentbus", "content_digest": DA}
        out = preview(PB, PA, rec)
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_RECORD", out["reason_codes"])


class PackageIntegrityTests(unittest.TestCase):
    """缺陷 1：严格三字段清单 + 重算 canonical digest，伪造清单不得可信。"""

    def test_valid_package_is_accepted(self):
        out = preview(PA, PA, record(DA))
        self.assertNotIn("INVALID_SOURCE", out["reason_codes"])

    def test_tampered_manifest_entry_sha_is_rejected(self):
        tampered = copy.deepcopy(PA)
        tampered["files"][0]["sha256"] = "f" * 64
        out = preview(tampered, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_forged_content_digest_is_rejected(self):
        forged = copy.deepcopy(PA)
        forged["content_digest"] = "f" * 64
        out = preview(forged, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_extra_field_in_entry_is_rejected(self):
        bad = pkg(files=[{"path": "SKILL.md", "bytes": 1, "sha256": D1,
                          "mode": "755"}])
        out = preview(bad, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_missing_field_in_entry_is_rejected(self):
        bad = pkg(files=[{"path": "SKILL.md", "bytes": 1}])
        out = preview(bad, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_non_integer_bytes_is_rejected(self):
        bad = pkg(files=[{"path": "SKILL.md", "bytes": "1", "sha256": D1}])
        out = preview(bad, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_negative_bytes_is_rejected(self):
        bad = pkg(files=[{"path": "SKILL.md", "bytes": -1, "sha256": D1}])
        out = preview(bad, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_unsafe_entry_path_is_rejected(self):
        for path in ("../SKILL.md", "/abs/SKILL.md", "a\\b", "scripts/../x"):
            with self.subTest(path=path):
                bad = pkg(files=[{"path": path, "bytes": 1, "sha256": D1}])
                out = preview(bad, PA, record(DA))
                self.assertEqual(out["action"], "conflict")
                self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_duplicate_entry_paths_are_rejected(self):
        bad = pkg(files=[{"path": "SKILL.md", "bytes": 1, "sha256": D1},
                         {"path": "skill.md", "bytes": 2, "sha256": D2}])
        out = preview(bad, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_tampered_target_is_rejected(self):
        tampered = copy.deepcopy(PA)
        tampered["files"][0]["sha256"] = "f" * 64
        out = preview(PB, tampered, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_TARGET", out["reason_codes"])


class RecordContextTests(unittest.TestCase):
    """缺陷 3：记录上下文须一致；丢失 target 不得 recorded。"""

    def test_record_context_mismatch_not_recorded(self):
        out = preview(PB, PA, record(DA, host="other"))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("RECORD_CONTEXT_MISMATCH", out["reason_codes"])
        self.assertNotEqual(out["states"]["installed"], "recorded")

    def test_record_scope_mismatch_not_recorded(self):
        out = preview(PB, PA, record(DA, scope="global"))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("RECORD_CONTEXT_MISMATCH", out["reason_codes"])
        self.assertEqual(out["states"]["installed"], "unverified")

    def test_record_route_mismatch_not_recorded(self):
        out = preview(PB, PA, record(DA, route="cli"))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("RECORD_CONTEXT_MISMATCH", out["reason_codes"])
        self.assertEqual(out["states"]["installed"], "unverified")

    def test_matching_context_is_recorded(self):
        out = preview(PB, PA, record(DA))
        self.assertEqual(out["states"]["installed"], "recorded")

    def test_lost_target_is_not_recorded(self):
        out = preview(PB, None, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("TARGET_LOST", out["reason_codes"])
        self.assertEqual(out["states"]["installed"], "unverified")


class ActivePinTests(unittest.TestCase):
    """缺陷 5：畸形 active_task_ids 不引发异常、不被字符串误拆。"""

    def test_valid_pin_list_conflicts(self):
        out = preview(PB, PA, record(DA), active_task_ids=["t-1"])
        self.assertEqual(out["action"], "conflict")
        self.assertIn("ACTIVE_TASK_PIN", out["reason_codes"])

    def test_empty_active_task_ids_is_allowed(self):
        out = preview(PB, PA, record(DA), active_task_ids=())
        self.assertEqual(out["action"], "update")

    def test_string_pin_is_not_mis_split(self):
        out = preview(PB, PA, record(DA), active_task_ids="t-1")
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_ACTIVE_TASK_IDS", out["reason_codes"])
        self.assertNotIn("ACTIVE_TASK_PIN", out["reason_codes"])

    def test_integer_pin_does_not_raise(self):
        out = preview(PB, PA, record(DA), active_task_ids=5)
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_ACTIVE_TASK_IDS", out["reason_codes"])

    def test_dict_pin_does_not_raise(self):
        out = preview(PB, PA, record(DA), active_task_ids={"t-1": True})
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_ACTIVE_TASK_IDS", out["reason_codes"])

    def test_non_string_element_is_rejected(self):
        out = preview(PB, PA, record(DA), active_task_ids=["t-1", 2])
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_ACTIVE_TASK_IDS", out["reason_codes"])

    def test_blank_string_element_is_rejected(self):
        out = preview(PB, PA, record(DA), active_task_ids=["   "])
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_ACTIVE_TASK_IDS", out["reason_codes"])


class ValidationTests(unittest.TestCase):
    def test_invalid_host(self):
        out = preview(PB, PA, record(DA), host="")
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_HOST", out["reason_codes"])

    def test_invalid_scope(self):
        out = preview(PB, PA, record(DA, scope="user"), scope="user")
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SCOPE", out["reason_codes"])

    def test_invalid_route(self):
        out = preview(PB, PA, record(DA, route="gui"), route="gui")
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_ROUTE", out["reason_codes"])

    def test_malformed_record_is_rejected(self):
        out = preview(PB, PA, {"owner": "agentbus"})
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_RECORD", out["reason_codes"])

    def test_record_bad_digest_is_rejected(self):
        out = preview(PB, PA, record("not-a-digest"))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_RECORD", out["reason_codes"])

    def test_bad_source_digest_is_rejected(self):
        out = preview(pkg("zz"), PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_bad_target_digest_is_rejected(self):
        out = preview(PB, pkg("zz"), record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_TARGET", out["reason_codes"])


class CreateTests(unittest.TestCase):
    def test_create_when_target_absent(self):
        out = preview(PB, None, None)
        self.assertEqual(out["action"], "create")
        self.assertIs(out["write_permitted"], False)
        self.assertIn("TARGET_ABSENT", out["reason_codes"])

    def test_create_still_readonly(self):
        out = preview(PB, None, None)
        self.assertIs(out["write_permitted"], False)
        self.assertIs(out["delete_permitted"], False)


class StateTests(unittest.TestCase):
    def test_states_start_unverified(self):
        out = preview(PB, None, None)
        self.assertEqual(out["states"]["installed"], "unverified")
        self.assertEqual(out["states"]["discovered"], "unverified")
        self.assertEqual(out["states"]["called"], "unverified")

    def test_installation_fact_is_separate_from_invocation(self):
        out = preview(PB, PA, record(DA))
        self.assertEqual(out["states"]["installed"], "recorded")
        # 安装事实成立，但发现/调用证据仍必须未验证（互不代证）。
        self.assertEqual(out["states"]["discovered"], "unverified")
        self.assertEqual(out["states"]["called"], "unverified")


class ExecutionFixedTests(unittest.TestCase):
    """缺陷 4：当前无固定执行载体，execution_fixed 恒 False，仅凭 flags 不标绿。"""

    def _rec_with_execution(self, **over):
        execution = {"fixed": True, "resource_read_verified": True,
                     "content_digest": DB, "host": "codex",
                     "scope": "project", "route": "cli"}
        execution.update(over)
        return record(DA, route="cli", execution=execution)

    def test_flags_alone_never_fixed(self):
        out = preview(PB, PA, self._rec_with_execution(), route="cli")
        self.assertIs(out["execution_fixed"], False)
        self.assertIn("EXECUTION_FIX_UNVERIFIED", out["reason_codes"])

    def test_conflict_action_never_fixed(self):
        out = preview(PB, PC, self._rec_with_execution(), route="cli")
        self.assertEqual(out["action"], "conflict")
        self.assertIs(out["execution_fixed"], False)

    def test_missing_target_never_fixed(self):
        out = preview(PB, None, self._rec_with_execution(), route="cli")
        self.assertIs(out["execution_fixed"], False)

    def test_global_desktop_never_fixed(self):
        rec = record(DA, scope="global", route="desktop",
                     execution={"fixed": True, "resource_read_verified": True})
        out = preview(PB, PA, rec, scope="global", route="desktop")
        self.assertIs(out["execution_fixed"], False)

    def test_project_desktop_never_fixed(self):
        out = preview(PB, PA, record(DA, execution={"fixed": True}), route="desktop")
        self.assertIs(out["execution_fixed"], False)

    def test_no_record_never_fixed(self):
        out = preview(PB, PA, None, route="cli")
        self.assertIs(out["execution_fixed"], False)


class OutputShapeTests(unittest.TestCase):
    def test_schema_and_reason_codes_shape(self):
        out = preview(PB, PA, None)
        self.assertEqual(out["schema_version"], 1)
        self.assertEqual(out["reason_codes"], sorted(set(out["reason_codes"])))
        self.assertEqual(out["content"]["digest_covers"], "paths_and_raw_bytes_only")

    def test_write_permitted_false_for_every_action(self):
        cases = [
            (PB, None, None),          # create
            (PA, PA, record(DA)),      # unchanged
            (PB, PA, record(DA)),      # update
            (PB, PA, None),            # conflict
        ]
        for source, target, rec in cases:
            with self.subTest(action=(source, target, rec)):
                out = preview(source, target, rec)
                self.assertIs(out["write_permitted"], False)


class ManifestDirectoryContractTests(unittest.TestCase):
    """OpenCode 互审反例：清单必须符合 Task1 目录契约（私有路径/排序/对账/入口/规模）。"""

    def _sorted(self, *entries):
        return sorted(entries, key=lambda e: e["path"].encode("utf-8"))

    def test_private_control_paths_are_rejected(self):
        for bad in (".workbuddy/x.md", ".git/config",
                    ".agentbus/agents/opencode/ready/m-1.json", ".ssh/id_rsa"):
            with self.subTest(path=bad):
                entries = self._sorted(
                    {"path": "SKILL.md", "bytes": 1, "sha256": D1},
                    {"path": bad, "bytes": 2, "sha256": D2})
                out = preview(pkg(files=entries), PA, record(DA))
                self.assertEqual(out["action"], "conflict")
                self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_unsorted_manifest_is_rejected(self):
        ordered = self._sorted(
            {"path": "SKILL.md", "bytes": 1, "sha256": D1},
            {"path": "a-/y", "bytes": 2, "sha256": D2},
            {"path": "a/x", "bytes": 3, "sha256": D3})
        shuffled = [ordered[0], ordered[2], ordered[1]]
        self.assertNotEqual(canonical_digest(ordered), canonical_digest(shuffled))
        ok = preview(pkg(files=ordered), PA, record(DA))
        self.assertNotIn("INVALID_SOURCE", ok["reason_codes"])
        bad = preview(pkg(files=shuffled), PA, record(DA))
        self.assertEqual(bad["action"], "conflict")
        self.assertIn("INVALID_SOURCE", bad["reason_codes"])

    def test_total_bytes_must_equal_manifest_sum(self):
        forged = pkg(D1)
        forged["total_bytes"] = 123456
        out = preview(forged, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])
        self.assertIsNone(out["source"])

    def test_non_integer_total_bytes_is_rejected(self):
        for value in ("999", 999.0, True):
            with self.subTest(total=value):
                bad = pkg(D1)
                bad["total_bytes"] = value
                out = preview(bad, PA, record(DA))
                self.assertEqual(out["action"], "conflict")
                self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_missing_skill_entry_is_rejected(self):
        bad = pkg(files=[{"path": "scripts/a.py", "bytes": 1, "sha256": D1}])
        out = preview(bad, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_over_file_count_limit_is_rejected(self):
        entries = [{"path": "SKILL.md", "bytes": 1, "sha256": D1}]
        entries += [{"path": f"f{i:05d}.md", "bytes": 1, "sha256": D2}
                    for i in range(5000)]
        out = preview(pkg(files=entries), PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_oversized_single_file_is_rejected(self):
        bad = pkg(files=[{"path": "SKILL.md", "bytes": 8 * 1024 * 1024 + 1,
                          "sha256": D1}])
        out = preview(bad, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_over_total_byte_limit_is_rejected(self):
        chunk = 8 * 1024 * 1024
        entries = [{"path": "SKILL.md", "bytes": chunk, "sha256": D1}]
        entries += [{"path": f"f{i}.md", "bytes": chunk, "sha256": D2}
                    for i in range(4)]
        out = preview(pkg(files=entries), PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])

    def test_forged_name_and_source_are_not_passed_through(self):
        bad = pkg(D1)
        bad["name"] = 12345
        bad["source"] = {"n": 1}
        out = preview(bad, PA, record(DA))
        self.assertEqual(out["action"], "conflict")
        self.assertIn("INVALID_SOURCE", out["reason_codes"])
        self.assertIsNone(out["source"])


if __name__ == "__main__":
    unittest.main()