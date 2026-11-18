"""Real child output must keep Chinese evidence regardless of parent locale."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools.delivery_self_proof import run_single_test_module


class EvidenceEncodingTest(unittest.TestCase):
    def test_child_chinese_output_survives_non_utf8_parent_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'test_unicode.py'
            path.write_text("import unittest\nclass RoundTrip(unittest.TestCase):\n"
                            " def test_unicode(self):\n  print('中文证据不截断')\n",
                            encoding='utf-8')
            with patch.dict('os.environ', {'PYTHONIOENCODING': 'cp936', 'PYTHONUTF8': '0'}):
                result = run_single_test_module(path, cwd=root)
            self.assertTrue(result['passed'], result)
            self.assertEqual(result['ran_count'], 1)
            self.assertIn('中文证据不截断', result['raw_output'])
            self.assertNotIn('\ufffd', result['raw_output'])


if __name__ == '__main__': unittest.main()
