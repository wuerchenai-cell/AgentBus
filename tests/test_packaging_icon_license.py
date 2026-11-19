# -*- coding: utf-8 -*-
"""Check public icon provenance separately from frozen dependency notices."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import struct
import sys
import unittest

_INTEGRATION_DIR = Path(__file__).resolve().parents[1]
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from tools.export_source import APP_ICON_FILE, _validate_ico

_NOTICE = _INTEGRATION_DIR / "share_assets" / "THIRD_PARTY_NOTICES.md"
_MEMBER_SRC = _INTEGRATION_DIR / "agentbus" / "member_identity_icons.py"
_OLD_BITMAP_MODULE = _INTEGRATION_DIR / "agentbus" / "builtin_icon_data.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class PublicIconPackagingTests(unittest.TestCase):
    def test_public_sources_exclude_legacy_vendor_bitmap_module(self):
        self.assertFalse(_OLD_BITMAP_MODULE.exists())
        self.assertNotIn("builtin_icon_data", _read(_MEMBER_SRC))

    def test_notice_identifies_neutral_members_and_preserves_their_names(self):
        notice = _read(_NOTICE)
        self.assertIn("member_identity_icons.py", notice)
        self.assertIn("中性机器人头像", notice)
        self.assertIn("显示名称", notice)

    def test_application_icon_is_multiresolution_and_matches_its_provenance(self):
        relative = APP_ICON_FILE
        blob = (_INTEGRATION_DIR / relative).read_bytes()
        notice = _read(_NOTICE)
        self.assertTrue(_validate_ico(blob))
        count = struct.unpack_from("<H", blob, 4)[0]
        widths = {blob[6 + 16 * entry] or 256 for entry in range(count)}
        self.assertTrue({16, 32, 48, 256}.issubset(widths))
        prefix = f"`{relative}` 的 SHA-256 为 "
        provenance = re.search(re.escape(prefix) + r"`([0-9a-f]{64})`", notice)
        self.assertIsNotNone(provenance)
        self.assertEqual(provenance.group(1), hashlib.sha256(blob).hexdigest())


class PinnedSoftwareLicenseEvidenceTests(unittest.TestCase):
    def test_native_component_licenses_use_verified_immutable_commits(self):
        import json
        payload=json.loads((_INTEGRATION_DIR/'share_assets/licenses/provenance.json').read_text(encoding='utf-8'))
        names={'codex-LICENSE.txt','codex-NOTICE.txt','ratatui-LICENSE.txt',
               'ripgrep-LICENSE-MIT.txt','ripgrep-UNLICENSE.txt','proxy-tools-LICENSE.txt'}
        found={}
        for item in payload['files']:
            name=Path(item['path']).name
            if name in names:
                self.assertRegex(item['source'],r'https://raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/')
                self.assertNotIn('snapshot',item['version'])
                found[name]=item
        self.assertEqual(set(found),names)

    def test_proxy_license_is_bound_to_the_exact_upstream_source_commit(self):
        import json
        root = _NOTICE.parent
        data = json.loads(_read(root / 'licenses/provenance.json'))
        proxy = next(item for item in data['files'] if item['component'] == 'proxy-tools')
        commit = '77369e4d321ed7b10d7b6e4885d7fe7a7ec2dfb7'
        self.assertIn(f'/{commit}/LICENSE.txt', proxy['source'])
        self.assertEqual(hashlib.sha256((root / proxy['path']).read_bytes()).hexdigest(), proxy['sha256'])
        for token in (commit, 'proxy_tools/__init__.py', 'BSD', 'MIT'):
            self.assertIn(token, _read(_NOTICE))

    def test_observed_frozen_build_packages_have_license_materials(self):
        import json
        payload=json.loads((_INTEGRATION_DIR/'share_assets/licenses/provenance.json').read_text(encoding='utf-8'))
        components={item['component'] for item in payload['files']}
        self.assertTrue({'altgraph','pefile','pywin32-ctypes','setuptools'}<=components)
        names={item['path'] for item in payload['files']}
        self.assertIn('licenses/setuptools-validate-pyproject-NOTICE.txt',names)
        self.assertIn('licenses/setuptools-config-NOTICE.txt',names)


if __name__ == "__main__":
    unittest.main()
