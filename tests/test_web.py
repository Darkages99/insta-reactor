"""Smoke tests for the Flask control-panel (no browser launched)."""

import unittest

from insta_reactor.web import app as webapp


class WebSmokeTests(unittest.TestCase):
    def setUp(self):
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def test_home_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Reactor", r.data)

    def test_status_json(self):
        r = self.client.get("/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("status", r.get_json())

    def test_thumb_404_for_missing(self):
        r = self.client.get("/thumb/does-not-exist.png")
        self.assertEqual(r.status_code, 404)

    def test_run_without_chats_errors_gracefully(self):
        r = self.client.post("/run", data={}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)   # redirects home, shows error state


if __name__ == "__main__":
    unittest.main()
