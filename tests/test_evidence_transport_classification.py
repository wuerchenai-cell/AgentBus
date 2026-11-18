"""§7.1 AUDIT-EVIDENCE-TRANSPORT 回归测试。

背景（两个叠加缺陷，缺一不可）
------------------------------

缺陷 A —— 证据分级读了不存在的字段：
    ``classify_evidence()`` 用 ``transport in NATIVE_TRANSPORTS`` 判原生通道，
    但 ``Message``（bus.py）35 个字段里**没有** ``transport``，``answer`` 也从不写它。
    实测全账本 184 条 result 消息 **0 条含该键**（既非 "native" 也非 None，是根本没这个键），
    于是判据恒假 → 每条真实交付都被降级为 ``transcript``。

缺陷 B —— ID 正则拒绝全部任务树任务（更致命，且在 A 之前生效）：
    ``TASK_ID_RE`` 原为 ``^(?:m|rx)-[0-9]{10,20}-[0-9a-f]{6,32}$``，要求中段是**纯数字**。
    但任务树发放的 ID 形如 ``m-tree-<24 hex>``（task_tree.py:771 ``_allocation_id``），
    中段是字面量 ``tree``。实测 38 条 tree 任务被正则拒绝。
    ``verify_task`` 在第一步 ``task_id_format`` 检查即返回，**根本走不到**证据分级，
    于是无论 A 修不修，tree 任务恒 ``evidence=none`` / ``verdict=fail``。

修法
----
* A：``classify_evidence`` 新增 ``fenced`` 参数，用「回执带非空 ``source_claim_id``」
  （即由 claim 闸门签发，程序路径产出）作为原生通道的**可实测**证据；
  ``transport`` 保留为兼容入口。弱判据仍落 ``transcript``，不放松纪律。
* B：ID 正则中段由 ``[0-9]{10,20}`` 放宽为 ``(?:[0-9]{10,20}|tree)``，
  锚定与长度约束保留。

覆盖率要求：本测试必须能区分「修前 / 修后」，因此既断言正向（合法 ID 通过、
原生证据成立），也断言反向（畸形 ID 仍被拒、无栅栏回执仍降级）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentbus.delivery_verify import (EVIDENCE_NATIVE, EVIDENCE_TRANSCRIPT,
                                      RESULT_ID_RE, TASK_ID_RE,
                                      classify_evidence)


class TreeTaskIdRegexTests(unittest.TestCase):
    """缺陷 B：任务树 ID 必须被 TASK_ID_RE / RESULT_ID_RE 接受。"""

    TREE_TASK = "m-tree-2e3537a4088ad346813b6619"
    NORMAL_TASK = "m-1791457584499-8893c2"
    RX_TASK = "rx-1791457584502-3c65ce"
    NORMAL_RESULT = "m-1791720518627-536841"

    def test_tree_task_id_is_accepted(self):
        self.assertIsNotNone(TASK_ID_RE.match(self.TREE_TASK),
                             "任务树 ID 被 TASK_ID_RE 拒绝 —— tree 任务在被体检前就被丢弃")

    def test_tree_result_id_is_accepted(self):
        # 同一正则族；tree 任务的回执也走这里。
        self.assertIsNotNone(RESULT_ID_RE.match(self.TREE_TASK))

    def test_name_task_id_still_accepted(self):
        self.assertIsNotNone(TASK_ID_RE.match(self.NORMAL_TASK))
        self.assertIsNotNone(RESULT_ID_RE.match(self.NORMAL_RESULT))

    def test_rx_prefix_still_accepted(self):
        self.assertIsNotNone(TASK_ID_RE.match(self.RX_TASK))

    def test_malformed_ids_still_rejected(self):
        """放宽中段不得变成「任意字符都收」。"""
        for bad in ("m--x", "m-1791-ab", "m-tree-zz", "m-tree-", "tree-x",
                    "m-树-abc123", "m-tree2-abc123def456"):
            with self.subTest(bad=bad):
                self.assertIsNone(TASK_ID_RE.match(bad),
                                  "%r 不应被判为合法任务 ID" % bad)


class EvidenceClassificationTests(unittest.TestCase):
    """缺陷 A：原生通道判据必须由可实测信号驱动，而非不存在的 transport 字段。"""

    def test_fenced_receipt_is_native(self):
        level, _ = classify_evidence(receipt_found=True, artifact_verified=True,
                                     fenced=True)
        self.assertEqual(level, EVIDENCE_NATIVE)

    def test_transport_still_honored_for_compat(self):
        level, _ = classify_evidence(receipt_found=True, artifact_verified=True,
                                     transport="native")
        self.assertEqual(level, EVIDENCE_NATIVE)

    def test_unfenced_receipt_without_transport_stays_transcript(self):
        """纪律不能松：既无栅栏又无 transport 的回执仍是 transcript。"""
        level, reason = classify_evidence(receipt_found=True, artifact_verified=True)
        self.assertEqual(level, EVIDENCE_TRANSCRIPT)
        self.assertIn("转录", reason)

    def test_fenced_but_artifacts_unverified_is_transcript(self):
        """有栅栏但产物未核验，仍不得升为 native。"""
        level, _ = classify_evidence(receipt_found=True, artifact_verified=False,
                                     fenced=True)
        self.assertEqual(level, EVIDENCE_TRANSCRIPT)

    def test_no_receipt_still_self_report_ceiling(self):
        """不把自报升级为实测 —— 无回执时最高 self_report。"""
        from agentbus.delivery_verify import EVIDENCE_SELF_REPORT
        level, _ = classify_evidence(receipt_found=False, self_reported=True)
        self.assertEqual(level, EVIDENCE_SELF_REPORT)

    def test_failed_receipt_wins_over_fencing(self):
        from agentbus.delivery_verify import EVIDENCE_FAILED
        level, _ = classify_evidence(receipt_found=True, receipt_failed=True,
                                     artifact_verified=True, fenced=True)
        self.assertEqual(level, EVIDENCE_FAILED)


if __name__ == "__main__":
    unittest.main()
