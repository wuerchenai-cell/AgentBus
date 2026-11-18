"""Message storage identity boundaries; Owner closeout review, 2026-10-14."""
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus, BusError, Message, now_iso


class TestCloseoutBusIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentbus-message-identity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bus = Bus(self.root / ".agentbus")
        self.bus.init()

    def message(self, **fields):
        return Message(**({"id": "m-safe-identity", "ts": now_iso(), "sender": "codex",
                           "to": "codex", "type": "info"} | fields))

    def test_absolute_message_id_cannot_write_outside_bus(self):
        target = self.root / "outside"
        with self.assertRaises(BusError):
            self.bus.post(self.message(id=str(target)))
        self.assertFalse(target.with_suffix(".json").exists())

    def test_invalid_storage_ids_are_rejected_before_post(self):
        for value in ("../escape", "..\\escape", "bad/name", "bad\\name", "C:stream", "m:stream",
                      "trailing.", "CON", "nul.txt", "m\nline", 123):
            with self.subTest(value=value), self.assertRaises(BusError):
                self.message(id=value).validate()

    def test_mailbox_and_reply_reference_boundaries_are_checked(self):
        for field, value in (("to", "../peer"), ("to", 123), ("result_to", "../peer"),
                             ("reply_to", "../task"), ("sender", "../peer"), ("sender", 123)):
            with self.subTest(field=field, value=value), self.assertRaises(BusError):
                self.message(**{field: value}).validate()

    def test_brand_sender_labels_and_short_legacy_ids_remain_supported(self):
        for index, sender in enumerate(("WorkBuddy", "Google Antigravity", "OpenCode", "人工用户")):
            msg = self.message(id=str(index), sender=sender)
            self.bus.post(msg)
            stored = self.bus.read(self.bus.dirs("codex")["ready"] / f"{index}.json")
            self.assertEqual(stored.sender, sender)


if __name__ == "__main__":
    unittest.main()
