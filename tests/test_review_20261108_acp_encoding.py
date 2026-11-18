"""JSON escapes preserve Unicode even with a legacy Windows stdin codepage."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from agentbus.acp import ACPAdapter
import json

class ACPEncodingRegression(unittest.TestCase):
    def test_protocol_write_is_ascii_json_with_lossless_unicode_roundtrip(self):
        adapter=ACPAdapter(['fixture-not-executed'])
        pipe=io.StringIO()
        adapter.process=Mock(stdin=pipe,poll=lambda:None)
        packet={'jsonrpc':'2.0','method':'session/new','params':{'cwd':'I:/中文 空格/目录','text':'读取 α😀 路径'}}
        adapter._write(packet)
        wire=pipe.getvalue()
        self.assertTrue(wire.isascii())
        self.assertEqual(json.loads(wire.encode('ascii').decode('cp936')),packet)
        adapter.process=None
