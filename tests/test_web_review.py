"""Tests for the chat-centric review UI (renders from the persisted store)."""

import os
import tempfile
import unittest

from insta_reactor.web import app as webapp
from insta_reactor.models import Decision, Action, Flag, FlagKind, RunSummary
from insta_reactor.review_store import ReviewStore


class WebReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        webapp._REVIEW_PATH = os.path.join(self.tmp, "review.json")
        webapp._THUMBS_DIR = os.path.join(self.tmp, "thumbs")
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def _seed(self):
        summ = RunSummary()
        summ.add(Decision(action=Action.AUTO_REPLY, reply_text="💀",
                          chat_name="Alice", reel_id="r1",
                          position_from_bottom=1))
        summ.add(Decision(action=Action.FLAG,
                          flag=Flag(FlagKind.NO_CONSENSUS, "crowd split"),
                          chat_name="Alice", reel_id="r2",
                          position_from_bottom=3))
        ReviewStore(webapp._REVIEW_PATH).record_run(summ, ["Alice", "Bob"])

    def test_empty_state_when_nothing_run(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"No chats gone over yet", r.data)

    def test_shows_chats_gone_over_and_badge(self):
        self._seed()
        r = self.client.get("/")
        body = r.data.decode()
        self.assertIn("Alice", body)
        self.assertIn("Bob", body)            # chat with nothing still listed
        self.assertIn("1 need you", body)     # Alice has one flagged reel
        self.assertIn("all clear", body)      # Bob is clear

    def test_shows_position_from_bottom(self):
        self._seed()
        body = self.client.get("/").data.decode()
        self.assertIn("3rd reel from the bottom", body)

    def test_position_label_for_most_recent(self):
        summ = RunSummary()
        summ.add(Decision(action=Action.FLAG,
                          flag=Flag(FlagKind.LOW_CONFIDENCE, "meh"),
                          chat_name="Alice", reel_id="r1",
                          position_from_bottom=1))
        ReviewStore(webapp._REVIEW_PATH).record_run(summ, ["Alice"])
        body = self.client.get("/").data.decode()
        self.assertIn("most recent reel", body)

    def test_resolve_clears_the_flag(self):
        self._seed()
        store = ReviewStore(webapp._REVIEW_PATH)
        key = store.chats["Alice"].pending()[0].key
        r = self.client.post("/resolve",
                             data={"chat": "Alice", "key": key},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        # Reload the page — Alice should now be all clear.
        body = self.client.get("/").data.decode()
        self.assertIn("all clear", body)
        self.assertEqual(ReviewStore(webapp._REVIEW_PATH).total_pending(), 0)

    def test_ordinal_helper(self):
        self.assertEqual(webapp._ordinal(1), "1st")
        self.assertEqual(webapp._ordinal(2), "2nd")
        self.assertEqual(webapp._ordinal(3), "3rd")
        self.assertEqual(webapp._ordinal(4), "4th")
        self.assertEqual(webapp._ordinal(11), "11th")
        self.assertEqual(webapp._ordinal(21), "21st")

    def test_incoming_text_shown_as_quote(self):
        summ = RunSummary()
        summ.add(Decision(action=Action.FLAG,
                          flag=Flag(FlagKind.INCOMING_TEXT, "you up?"),
                          chat_name="Alice", reel_id="text"))
        ReviewStore(webapp._REVIEW_PATH).record_run(summ, ["Alice"])
        body = self.client.get("/").data.decode()
        self.assertIn("you up?", body)


if __name__ == "__main__":
    unittest.main()
