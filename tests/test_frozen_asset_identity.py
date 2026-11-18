# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.web_desktop import DesktopStartupError, WEB_ASSETS, asset_directory

class TestFrozenAssetIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentbus-asset-identity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.override = self.workspace / ".agentbus/web_assets"
        self.extraction = self.root / "embedded"
        self.embedded = self.extraction / "agentbus/web_assets"
        for folder, color in ((self.override, "red"), (self.embedded, "blue")):
            folder.mkdir(parents=True)
            for name in WEB_ASSETS:
                (folder / name).write_text(color, encoding="utf-8")

    def test_frozen_uses_embedded_assets_despite_complete_workspace_override(self):
        with patch.object(sys, "_MEIPASS", str(self.extraction), create=True), patch.object(sys, "frozen", True, create=True):
            selected = asset_directory(self.workspace)
        self.assertEqual((selected / "app.css").read_text(encoding="utf-8"), "blue")
        self.assertEqual(selected, self.embedded)

    def test_meipass_alone_prevents_workspace_override(self):
        with patch.object(sys, "_MEIPASS", str(self.extraction), create=True), patch.object(sys, "frozen", False, create=True):
            self.assertEqual(asset_directory(self.workspace), self.embedded)

    def test_missing_embedded_asset_cannot_fall_back_to_workspace(self):
        (self.embedded / "app.js").unlink()
        with patch.object(sys, "_MEIPASS", str(self.extraction), create=True), patch.object(sys, "frozen", True, create=True):
            with self.assertRaises(DesktopStartupError):
                asset_directory(self.workspace)

    def test_source_development_preserves_workspace_override(self):
        with patch.object(sys, "_MEIPASS", None, create=True), patch.object(sys, "frozen", False, create=True):
            self.assertEqual(asset_directory(self.workspace), self.override)

if __name__ == "__main__":
    unittest.main()
