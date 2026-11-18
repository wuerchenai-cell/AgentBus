from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DesktopAppIconTests(unittest.TestCase):
    def module(self):
        from agentbus import desktop_app_icon
        return desktop_app_icon

    def test_rejects_relative_paths_without_shell_access(self):
        mod = self.module()
        with patch.object(mod, "_is_windows", return_value=True), \
             patch.object(mod, "_extract_shell_icon_png") as extract:
            result = mod.get_desktop_app_icon("apps\\agent.exe")
        self.assertEqual(result.status, "placeholder")
        self.assertIsNotNone(result.data_uri)
        extract.assert_not_called()

    def test_rejects_unselected_file_types_and_missing_paths(self):
        mod = self.module()
        with tempfile.TemporaryDirectory() as temp:
            wrong_path = Path(temp) / "agent.json"
            wrong_path.touch()
            missing = Path(temp) / "missing.exe"
            with patch.object(mod, "_is_windows", return_value=True), \
                 patch.object(mod, "_extract_shell_icon_png") as extract:
                wrong_type = mod.get_desktop_app_icon(wrong_path)
                missing_result = mod.get_desktop_app_icon(missing)
        self.assertEqual(wrong_type.status, "placeholder")
        self.assertEqual(wrong_type.reason, "unsupported-file-type")
        self.assertEqual(missing_result.status, "placeholder")
        self.assertEqual(missing_result.reason, "file-not-found")
        extract.assert_not_called()

    def test_non_windows_returns_neutral_placeholder(self):
        mod = self.module()
        with tempfile.TemporaryDirectory() as temp:
            selected = Path(temp) / "agent.exe"
            selected.touch()
            with patch.object(mod, "_is_windows", return_value=False), \
                 patch.object(mod, "_extract_shell_icon_png") as extract:
                result = mod.get_desktop_app_icon(selected)
        self.assertEqual(result.status, "placeholder")
        self.assertIn("image/svg+xml", result.data_uri)
        extract.assert_not_called()

    def test_selected_exe_returns_png_data_uri_and_shortcut_is_rejected(self):
        mod = self.module()
        png = mod._encode_rgba_png(1, 1, bytes((10, 20, 30, 255)))
        with tempfile.TemporaryDirectory() as temp:
            selected = Path(temp) / "selected.exe"
            selected.touch()
            with patch.object(mod, "_is_windows", return_value=True), \
                 patch.object(mod, "_extract_shell_icon_png", return_value=png):
                result = mod.get_desktop_app_icon(selected)
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data_uri, "data:image/png;base64," +
                             base64.b64encode(png).decode("ascii"))
            shortcut = Path(temp) / "selected.lnk"
            shortcut.touch()
            with patch.object(mod, "_is_windows", return_value=True), \
                 patch.object(mod, "_extract_shell_icon_png") as extract:
                unsupported = mod.get_desktop_app_icon(shortcut)
            self.assertEqual(unsupported.status, "placeholder")
            self.assertEqual(unsupported.reason, "unsupported-file-type")
            extract.assert_not_called()

    def test_failed_or_invalid_shell_result_falls_back_without_leaking_path(self):
        mod = self.module()
        with tempfile.TemporaryDirectory() as temp:
            selected = Path(temp) / "custom-agent.exe"
            selected.touch()
            with patch.object(mod, "_is_windows", return_value=True), \
                 patch.object(mod, "_extract_shell_icon_png", side_effect=OSError("shell error")):
                result = mod.get_desktop_app_icon(selected)
        self.assertEqual(result.status, "placeholder")
        self.assertNotIn(str(selected), result.reason)
        self.assertTrue(result.data_uri.startswith("data:image/svg+xml;base64,"))

    def test_invalid_shell_bytes_fall_back_to_neutral_icon(self):
        mod = self.module()
        with tempfile.TemporaryDirectory() as temp:
            selected = Path(temp) / "custom-agent.exe"
            selected.touch()
            with patch.object(mod, "_is_windows", return_value=True), \
                 patch.object(mod, "_extract_shell_icon_png", return_value=b"not-png"):
                result = mod.get_desktop_app_icon(selected)
        self.assertEqual(result.status, "placeholder")
        self.assertEqual(result.reason, "icon-unavailable")

    def test_generated_png_has_valid_signature_and_dimensions(self):
        mod = self.module()
        png = mod._encode_rgba_png(1, 1, bytes((10, 20, 30, 255)))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(png[16:20], "big"), 1)
        self.assertEqual(int.from_bytes(png[20:24], "big"), 1)
        offset = 8
        names = []
        while offset < len(png):
            size = int.from_bytes(png[offset:offset + 4], "big")
            name = png[offset + 4:offset + 8]
            body = png[offset + 8:offset + 8 + size]
            crc = int.from_bytes(png[offset + 8 + size:offset + 12 + size], "big")
            self.assertEqual(mod.zlib.crc32(name + body) & 0xFFFFFFFF, crc)
            names.append(name)
            offset += 12 + size
        self.assertEqual(names, [b"IHDR", b"IDAT", b"IEND"])


if __name__ == "__main__":
    unittest.main()
