from __future__ import annotations

import base64
from pathlib import Path
import sys
import unittest
from xml.etree import ElementTree

_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from agentbus.member_identity_icons import (
    identity_icon_data_uri,
    identity_icon_svg,
    known_member_ids,
    resolve_member_identity,
)

_SVG = "{http://www.w3.org/2000/svg}"


class MemberIdentityIconsTests(unittest.TestCase):
    def test_known_member_ids_include_all_core_agents(self):
        self.assertEqual(known_member_ids(), (
            "codex", "antigravity", "gemini", "trae", "workbuddy", "opencode", "zcode",
        ))

    def test_member_aliases_keep_canonical_id_and_display_name(self):
        cases = {
            "openaicodex": ("codex", "Codex"),
            "googleantigravity": ("antigravity", "Antigravity"),
            "googlegemini": ("gemini", "Gemini"),
            "traesolocn": ("trae", "Trae"),
            "tencentworkbuddy": ("workbuddy", "WorkBuddy"),
            "opencode": ("opencode", "OpenCode"),
            "zcode": ("zcode", "ZCode"),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                identity = resolve_member_identity(query)
                self.assertEqual((identity.key, identity.name), expected)
                self.assertEqual(identity.asset_kind, "agentbus-neutral-monogram")
                self.assertFalse(identity.vendor_logo_licensed)

    def test_all_known_members_render_distinct_neutral_monograms(self):
        monograms = set()
        for member in known_member_ids():
            with self.subTest(member=member):
                name = resolve_member_identity(member).name
                svg = identity_icon_svg(member)
                root = ElementTree.fromstring(svg)
                self.assertEqual(root.attrib["viewBox"], "0 0 64 64")
                self.assertEqual(root.find(f"{_SVG}title").text, f"{name} member identifier")
                monograms.add(root.find(f"{_SVG}text").text)
                self.assertIsNone(root.find(f".//{_SVG}image"))
                self.assertIsNone(root.find(f".//{_SVG}defs"))
                self.assertNotIn("base64,", svg)
        self.assertEqual(len(monograms), len(known_member_ids()))

    def test_data_uri_decodes_to_same_svg(self):
        svg = identity_icon_svg("codex", "Review Agent")
        image_type, payload = identity_icon_data_uri("codex", "Review Agent").split(",", 1)
        self.assertEqual(image_type, "data:image/svg+xml;base64")
        self.assertEqual(base64.b64decode(payload).decode("utf-8"), svg)

    def test_custom_agent_fallback_escapes_display_label(self):
        label = "Special & Bot <分析>"
        identity = resolve_member_identity("custom-42", label)
        self.assertEqual(identity.key, "custom")
        self.assertFalse(identity.vendor_logo_licensed)
        root = ElementTree.fromstring(identity_icon_svg("custom-42", label))
        self.assertEqual(root.find(f"{_SVG}title").text, f"{label} member identifier")
        self.assertEqual(root.find(f"{_SVG}text").text, "SB")


if __name__ == "__main__":
    unittest.main()
