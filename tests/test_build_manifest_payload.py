"""Manifest must describe resources actually shipped by the onefile recipe."""
from pathlib import Path
import tempfile
import unittest
from tools.generate_build_manifest import build_manifest_data


class PayloadTest(unittest.TestCase):
    def test_preview_backup_and_unused_icon_are_not_claimed_as_shipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'agentbus/web_assets').mkdir(parents=True)
            (root / 'agentbus/assets').mkdir()
            for relative in ('agentbus/__init__.py', 'agentbus/web_assets/app.js',
                             'agentbus/web_assets/app.js.bak_takeover',
                             'agentbus/web_assets/preview.json',
                             'agentbus/assets/agentbus.png', 'agentbus/assets/agentbus.ico'):
                (root / relative).write_bytes(b'fixture')
            data = build_manifest_data(root, root / 'agentbus')['package_data_assets']
            self.assertEqual({'agentbus/web_assets/app.js', 'agentbus/assets/agentbus.ico'}, set(data))


if __name__ == '__main__': unittest.main()
