# -*- coding: utf-8 -*-
import unittest
from agentbus.bus import BusError, Message

class TestMessageIdPathBoundary(unittest.TestCase):
    def test_reject_slash_and_relative_path(self):
        for identity in ('../escape', '..\\escape', '/absolute', 'C:\\absolute', 'm/child', 'm\\child'):
            with self.subTest(identity=identity), self.assertRaises(BusError):
                Message(id=identity, ts='2026-10-14T00:00:00+08:00', sender='codex', to='codex', type='info').validate()

if __name__ == "__main__":
    unittest.main()
