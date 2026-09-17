"""Tests for the chat-centric ReviewStore — the state behind the panel."""

import os
import tempfile
import unittest

from insta_reactor.models import (
    Decision, Action, Flag, FlagKind, RunSummary,
)
from insta_reactor.review_store import ReviewStore, ChatReport


def _flag(chat, reel_id, kind, reason, pos=0, thumb=None):
    return Decision(
        action=Action.FLAG, flag=Flag(kind, reason),
        chat_name=chat, reel_id=reel_id,
        position_from_bottom=pos, thumbnail_path=thumb,
    )


def _react(chat, reel_id, reply, pos=0, thumb=None):
    return Decision(
        action=Action.AUTO_REPLY, reply_text=reply,
        chat_name=chat, reel_id=reel_id,
        position_from_bottom=pos, thumbnail_path=thumb,
    )


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "review.json")

    def _store(self):
        return ReviewStore(self.path)

    def test_records_flagged_and_reacted_per_chat(self):
        summ = RunSummary()
        summ.add(_react("Alice", "r1", "💀", pos=1, thumb="/x/a.png"))
        summ.add(_flag("Alice", "r2", FlagKind.NO_CONSENSUS, "split", pos=3,
                       thumb="/x/b.png"))
        self._store().record_run(summ, ["Alice"])

        rep = self._store().chats["Alice"]
        self.assertEqual(len(rep.reacted), 1)
        self.assertEqual(len(rep.pending()), 1)
        flagged = rep.pending()[0]
        self.assertEqual(flagged.position_from_bottom, 3)
        self.assertEqual(flagged.thumbnail, "b.png")   # basename only
        self.assertEqual(flagged.reason, "split")

    def test_empty_chat_still_shows_as_gone_over(self):
        self._store().record_run(RunSummary(), ["Quiet"])
        rep = self._store().chats.get("Quiet")
        self.assertIsNotNone(rep)
        self.assertEqual(rep.pending(), [])
        self.assertTrue(rep.last_run_at > 0)

    def test_flag_persists_across_a_rerun_that_skips_it(self):
        # Run 1 flags r2. Run 2 (runner de-dup) doesn't re-yield r2 => summary
        # has no r2 => it must STILL be in the queue, not vanish.
        s1 = RunSummary()
        s1.add(_flag("Alice", "r2", FlagKind.LOW_CONFIDENCE, "meh", pos=2))
        self._store().record_run(s1, ["Alice"])

        self._store().record_run(RunSummary(), ["Alice"])   # nothing new
        rep = self._store().chats["Alice"]
        self.assertEqual(len(rep.pending()), 1)
        self.assertEqual(rep.pending()[0].reel_id, "r2")

    def test_reacted_snapshot_is_replaced_each_run(self):
        s1 = RunSummary()
        s1.add(_react("Alice", "r1", "💀"))
        self._store().record_run(s1, ["Alice"])
        self.assertEqual(len(self._store().chats["Alice"].reacted), 1)

        self._store().record_run(RunSummary(), ["Alice"])   # nothing new
        self.assertEqual(len(self._store().chats["Alice"].reacted), 0)

    def test_no_duplicate_flag_on_repeat(self):
        for _ in range(3):
            s = RunSummary()
            s.add(_flag("Alice", "r9", FlagKind.UNABLE_TO_READ, "blocked"))
            self._store().record_run(s, ["Alice"])
        self.assertEqual(len(self._store().chats["Alice"].pending()), 1)

    def test_distinct_incoming_texts_stay_separate(self):
        s = RunSummary()
        s.add(_flag("Alice", "text", FlagKind.INCOMING_TEXT, "you up?"))
        s.add(_flag("Alice", "text", FlagKind.INCOMING_TEXT, "call me"))
        self._store().record_run(s, ["Alice"])
        self.assertEqual(len(self._store().chats["Alice"].pending()), 2)

    def test_resolve_removes_from_pending_and_persists(self):
        s = RunSummary()
        s.add(_flag("Alice", "r2", FlagKind.NO_CONSENSUS, "split"))
        self._store().record_run(s, ["Alice"])
        key = self._store().chats["Alice"].pending()[0].key

        self.assertTrue(self._store().resolve("Alice", key))
        # Reload from disk — resolution must be durable.
        self.assertEqual(self._store().chats["Alice"].pending(), [])
        self.assertEqual(self._store().total_pending(), 0)

    def test_resolved_flag_not_resurrected_by_later_run(self):
        s = RunSummary()
        s.add(_flag("Alice", "r2", FlagKind.NO_CONSENSUS, "split"))
        self._store().record_run(s, ["Alice"])
        key = self._store().chats["Alice"].pending()[0].key
        self._store().resolve("Alice", key)

        # Same reel flagged again in a later run — must stay handled.
        s2 = RunSummary()
        s2.add(_flag("Alice", "r2", FlagKind.NO_CONSENSUS, "split"))
        self._store().record_run(s2, ["Alice"])
        self.assertEqual(self._store().chats["Alice"].pending(), [])

    def test_ordered_chats_puts_most_pending_first(self):
        s = RunSummary()
        s.add(_flag("Busy", "a", FlagKind.NO_CONSENSUS, "x"))
        s.add(_flag("Busy", "b", FlagKind.NO_CONSENSUS, "y"))
        s.add(_flag("Light", "c", FlagKind.NO_CONSENSUS, "z"))
        self._store().record_run(s, ["Busy", "Light", "Clear"])
        order = [r.chat_name for r in self._store().ordered_chats()]
        self.assertEqual(order[0], "Busy")
        self.assertEqual(order[-1], "Clear")

    def test_total_pending(self):
        s = RunSummary()
        s.add(_flag("A", "1", FlagKind.NO_CONSENSUS, "x"))
        s.add(_flag("B", "2", FlagKind.NO_CONSENSUS, "y"))
        self._store().record_run(s, ["A", "B"])
        self.assertEqual(self._store().total_pending(), 2)


if __name__ == "__main__":
    unittest.main()
