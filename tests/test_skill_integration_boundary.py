"""Source: Codex independent integration review, 2026-10-05."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agentbus.skill_evidence import evaluate_evidence
from agentbus.skill_packages import inspect_package, SkillPackageError
from agentbus.skill_preview import build_sync_preview
from agentbus.skill_presentation import present_preview


class SkillIntegrationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pkg = self.root / 'pkg'
        self.pkg.mkdir()
        (self.pkg / 'SKILL.md').write_bytes(b'# example\n')

    def package(self):
        return inspect_package(self.root, 'pkg')

    def test_full_posix_path_utf8_sort_contract(self):
        for d in ('a', 'a-'):
            (self.pkg / d).mkdir()
            (self.pkg / d / 'x').write_bytes(b'data')
        report = self.package()
        paths = [x['path'] for x in report['files']]
        self.assertEqual(paths, sorted(paths, key=lambda x: x.encode('utf8')))
        self.assertEqual(report['content_digest'], hashlib.sha256(json.dumps(
            {'schema_version': 1, 'files': report['files']}, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode('utf8')).hexdigest())

    def test_directory_enumeration_has_a_real_resource_bound(self):
        for i in range(12):
            (self.pkg / ('empty-%02d' % i)).mkdir()
        with self.assertRaises(SkillPackageError):
            inspect_package(self.root, 'pkg', max_dir_bytes=8)

    def test_claimed_package_digest_cannot_hide_tampered_manifest(self):
        pkg = self.package()
        pkg['files'][0]['sha256'] = 'f' * 64
        out = build_sync_preview(pkg, None, None,
                                host='codex', scope='project', route='desktop')
        self.assertEqual(out['action'], 'conflict')
        self.assertIn('INVALID_SOURCE', out['reason_codes'])

    def test_legacy_single_entry_digest_is_not_full_package_identity(self):
        pkg = self.package()
        record = {'schema_version': 1, 'owner': 'agentbus',
                  'skill_sha256': pkg['content_digest']}
        out = build_sync_preview(pkg, pkg, record,
                                host='codex', scope='project', route='desktop')
        self.assertEqual(out['action'], 'conflict')

    def test_uncertified_fixed_flags_never_become_verified_execution(self):
        pkg = self.package()
        record = {'schema_version': 1, 'owner': 'agentbus',
                  'before_content_digest': pkg['content_digest'],
                  'host': 'codex', 'scope': 'project', 'route': 'cli',
                  'execution': {'fixed': True, 'resource_read_verified': True,
                                'content_digest': pkg['content_digest'],
                                'host': 'codex', 'scope': 'project', 'route': 'cli'}}
        out = build_sync_preview(pkg, pkg, record,
                                host='codex', scope='project', route='cli')
        self.assertIs(out['execution_fixed'], False)
        self.assertIn('未验证', present_preview(out)['execution_version_label'])

    def test_expiry_cannot_be_relaxed_past_thirty_days(self):
        ctx = {'kind': 'invocation', 'host': 'codex', 'host_version': '1',
               'machine_scope': 'one', 'project_scope': 'one', 'route': 'cli',
               'content_digest': 'a' * 64}
        ev = {**ctx, 'source': 'public-probe.json', 'outcome': 'passed',
              'observed_at_ts': 10_000_000 - 60 * 86400}
        try:
            out = evaluate_evidence(ev, ctx, now_ts=10_000_000, max_age_s=90*86400)
        except ValueError:
            pass
        else:
            self.assertEqual(out['state'], 'unverified')
        self.assertEqual(ev['outcome'], 'passed')

    def test_empty_identity_cannot_verify(self):
        ctx = {k: '' for k in ('kind', 'host', 'host_version', 'machine_scope',
                              'project_scope', 'route', 'content_digest')}
        out = evaluate_evidence({**ctx, 'outcome': 'passed', 'observed_at_ts': 1},
                                ctx, now_ts=2)
        self.assertEqual(out['state'], 'unverified')

    def test_failed_source_does_not_get_content_verified_note(self):
        view = present_preview({'action': 'conflict', 'reason_codes': ['INVALID_SOURCE'],
                                'scope': 'project', 'route': 'cli'})
        self.assertFalse(any('内容已校验' in s for s in view['notes']))
        self.assertIs(view['sync_clickable'], False)


if __name__ == '__main__':
    unittest.main()
