"""§7.39 证据范围契约测试：源码检查 / 候选包 / 正式 EXE 不得互相代称。

来源：§7.39 `PRODUCT-VS-SOURCE-EVIDENCE`（P1）。
验收要点（逐条对应）：
  1. 记录必须指明二进制路径/摘要 **或** 源码快照/摘要；
  2. 必须写明所走入口、环境与未验证项；
  3. 声称检查正式 EXE 而解释器非 frozen → 判错（把源码检查写成正式程序检查）；
  4. 登记摘要与现场摘要不符 → 判错（用错版本下结论）；
  5. 静态检查能抓出"正式程序…源码"这类互相代称措辞。
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from agentbus import evidence_scope as es


class BuildScopeTests(unittest.TestCase):
    def test_formal_exe_scope_requires_binary_path_and_sha(self):
        with self.assertRaises(es.EvidenceScopeError) as ctx:
            es.build_scope(subject=es.SUBJECT_FORMAL_EXE, entry_point='agentbus.exe')
        self.assertEqual(ctx.exception.code, 'MISSING_BINARY')

    def test_formal_exe_scope_rejects_bad_sha(self):
        with self.assertRaises(es.EvidenceScopeError) as ctx:
            es.build_scope(subject=es.SUBJECT_FORMAL_EXE, entry_point='e',
                           binary_path='成果/AgentBus.exe', binary_sha256='nothex',
                           environment={'interpreter': 'frozen'})
        self.assertEqual(ctx.exception.code, 'MISSING_BINARY_SHA')

    def test_source_scope_requires_snapshot_sha(self):
        with self.assertRaises(es.EvidenceScopeError) as ctx:
            es.build_scope(subject=es.SUBJECT_SOURCE, entry_point='pytest',
                           source_path='agentbus')
        self.assertEqual(ctx.exception.code, 'MISSING_SOURCE_SHA')

    def test_source_scope_requires_entry_point(self):
        with self.assertRaises(es.EvidenceScopeError) as ctx:
            es.build_scope(subject=es.SUBJECT_SOURCE, entry_point='',
                           source_path='agentbus',
                           source_snapshot_sha256='a' * 64)
        self.assertEqual(ctx.exception.code, 'MISSING_ENTRY')

    def test_invalid_subject_rejected(self):
        with self.assertRaises(es.EvidenceScopeError) as ctx:
            es.build_scope(subject='whatever', entry_point='x')
        self.assertEqual(ctx.exception.code, 'INVALID_SUBJECT')


class SourceMasqueradeTests(unittest.TestCase):
    """核心反例：声称检查正式 EXE，但解释器不是 frozen。"""

    def test_non_frozen_interpreter_cannot_claim_formal_exe(self):
        with self.assertRaises(es.EvidenceScopeError) as ctx:
            es.build_scope(subject=es.SUBJECT_FORMAL_EXE, entry_point='python -m',
                           binary_path='成果/AgentBus.exe', binary_sha256='b' * 64,
                           environment={'interpreter': 'source'})
        self.assertEqual(ctx.exception.code, 'SOURCE_MASQUERADING_AS_BINARY')

    def test_frozen_interpreter_accepts_formal_exe_scope(self):
        out = es.build_scope(subject=es.SUBJECT_FORMAL_EXE, entry_point='AgentBus.exe',
                             binary_path='成果/AgentBus.exe', binary_sha256='c' * 64,
                             environment={'interpreter': 'frozen'})
        self.assertEqual(out['subject'], es.SUBJECT_FORMAL_EXE)
        self.assertEqual(out['binary']['sha256'], 'c' * 64)
        self.assertEqual(out['kind'], 'evidence_scope')


class LiveHashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_formal_exe_scope_reads_real_bytes(self):
        exe = self.root / 'AgentBus.exe'
        payload = b'fake-binary-bytes'
        exe.write_bytes(payload)
        expected = hashlib.sha256(payload).hexdigest()
        out = es.formal_exe_scope(self.root, exe_relpath='AgentBus.exe',
                                  entry_point='AgentBus.exe')
        self.assertEqual(out['binary']['sha256'], expected)
        self.assertEqual(out['environment'].get('interpreter'), 'frozen')

    def test_registered_sha_mismatch_is_rejected(self):
        exe = self.root / 'AgentBus.exe'
        exe.write_bytes(b'v1')
        with self.assertRaises(es.EvidenceScopeError) as ctx:
            es.formal_exe_scope(self.root, exe_relpath='AgentBus.exe',
                                entry_point='AgentBus.exe',
                                registered_sha256='d' * 64)
        self.assertEqual(ctx.exception.code, 'REGISTERED_SHA_MISMATCH')

    def test_registered_sha_match_passes(self):
        exe = self.root / 'AgentBus.exe'
        payload = b'v2'
        exe.write_bytes(payload)
        good = hashlib.sha256(payload).hexdigest()
        out = es.formal_exe_scope(self.root, exe_relpath='AgentBus.exe',
                                  entry_point='AgentBus.exe', registered_sha256=good)
        self.assertEqual(out['binary']['sha256'], good)

    def test_missing_binary_is_reported(self):
        with self.assertRaises(es.EvidenceScopeError) as ctx:
            es.formal_exe_scope(self.root, exe_relpath='nope.exe', entry_point='x')
        self.assertEqual(ctx.exception.code, 'BINARY_UNREADABLE')

    def test_source_scope_snapshot_is_deterministic_and_content_sensitive(self):
        src = self.root / 'agentbus'
        src.mkdir()
        (src / 'a.py').write_text('x = 1\n', encoding='utf-8')
        first = es.source_scope(self.root, source_relpath='agentbus', entry_point='pytest')
        again = es.source_scope(self.root, source_relpath='agentbus', entry_point='pytest')
        self.assertEqual(first['source']['snapshot_sha256'],
                         again['source']['snapshot_sha256'])
        # 改内容 → 快照必须变，证明它真的绑定了字节。
        (src / 'a.py').write_text('x = 2\n', encoding='utf-8')
        changed = es.source_scope(self.root, source_relpath='agentbus', entry_point='pytest')
        self.assertNotEqual(first['source']['snapshot_sha256'],
                            changed['source']['snapshot_sha256'])

    def test_empty_source_tree_is_reported(self):
        empty = self.root / 'empty'
        empty.mkdir()
        with self.assertRaises(es.EvidenceScopeError) as ctx:
            es.hash_source_tree(empty)
        self.assertEqual(ctx.exception.code, 'EMPTY_TREE')


class ConflationDetectionTests(unittest.TestCase):
    def test_detects_binary_source_conflation(self):
        hits = es.detect_conflation('本结论依据正式程序与源码树得出结论。')
        self.assertTrue(hits, '应识别出"正式程序…源码"代称')
        self.assertIn('正式', hits[0])

    def test_contrast_wording_is_not_flagged(self):
        # 带"对照/对比/≠/还是"等标记的正当区分叙述不应被判代称。
        for text in ('正式程序与源码树对照检查。',
                     '源码检查 ≠ 正式 EXE 检查。',
                     '检查的是源码，还是正式程序？',
                     '三者被互相代称（这是要避免的问题）。'):
            self.assertEqual(es.detect_conflation(text), [], text)

    def test_clean_text_has_no_hits(self):
        hits = es.detect_conflation(
            '检查对象：源码树；入口：pytest；源树快照 sha256=…；未验证项：未启动桌面程序。')
        self.assertEqual(hits, [])

    def test_audit_conclusion_reports_ok(self):
        ok = es.audit_conclusion('检查了正式 EXE（sha256=…），入口 AgentBus.exe。')
        self.assertTrue(ok['ok'])
        bad = es.audit_conclusion('用候选包当正式 EXE 的结论。')
        self.assertFalse(bad['ok'])
        self.assertTrue(bad['conflation_hits'])


class RenderTests(unittest.TestCase):
    def test_markdown_lists_scope_and_unverified(self):
        scope = es.build_scope(subject=es.SUBJECT_SOURCE, entry_point='pytest',
                               source_path='agentbus',
                               source_snapshot_sha256='e' * 64,
                               environment={'interpreter': 'source'},
                               unverified=['未启动桌面程序'])
        text = es.to_markdown(scope)
        self.assertIn('证据范围', text)
        self.assertIn('agentbus', text)
        self.assertIn('未启动桌面程序', text)
        self.assertIn('未验证项', text)

    def test_json_round_trip_is_deterministic(self):
        scope = es.build_scope(subject=es.SUBJECT_CANDIDATE_PACKAGE,
                               entry_point='AgentBus.exe', source_path='pkg',
                               source_snapshot_sha256='f' * 64)
        dumped = json.dumps(scope, ensure_ascii=False, sort_keys=True)
        self.assertEqual(dumped, json.dumps(scope, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    unittest.main()
