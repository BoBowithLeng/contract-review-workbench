"""Session HTTP regression tests with explicit model stubs, never paid inference."""
from __future__ import annotations

import copy
import json
import unittest
import uuid

from tests import test_opinion_revision_routes as version_fixtures


class DiscussionSessionRouteTests(unittest.TestCase):
    def setUp(self):
        self.base = version_fixtures.OpinionRevisionRouteTests("test_new_discussion_makes_unadopted_candidate_stale")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.api, self.path = self.base.api, self.base.path
        self.server = self.base.fixture.server
        self.discussion_calls = []

        def respond(config, messages):
            self.discussion_calls.append(copy.deepcopy(messages))
            return "这是会话隔离的测试回答，不是法律建议。"

        self.server.discussions.completion = respond

    def get(self, session=None, versions=False):
        suffix = "/revisions" if versions else ""
        if session:
            suffix += "?session_id=" + session
        status, _, result = self.api("GET", self.path + suffix)
        self.assertEqual(status, 200, result)
        return result

    def send(self, session, message):
        state = self.get(session)
        status, _, result = self.api("POST", self.path, {
            "session_id": session, "expected_revision": state["revision"],
            "expected_opinion_revision": state["opinion_revision"],
            "expected_delivery_revision": state["context"]["delivery_revision"],
            "request_id": str(uuid.uuid4()), "message": message,
            "config": self.base.config, "consent": True,
        })
        self.assertIn(status, (200, 202), result)
        return self.base.wait("?session_id=" + session, "pending_turn_id")

    def create(self):
        current = self.get()
        body = {"expected_sessions_revision": current["sessions_revision"],
                "expected_delivery_revision": current["context"]["delivery_revision"],
                "expected_opinion_revision": current["opinion_revision"], "request_id": str(uuid.uuid4())}
        status, _, result = self.api("POST", self.path + "/sessions", body)
        self.assertEqual(status, 201, result)
        return result, body

    def test_new_session_is_clean_old_history_and_adopted_version_survive(self):
        first = self.get()["session_id"]
        self.send(first, "OLD_ONLY_MARKER_20260924")
        candidate_state, _ = self.base.generate()
        accepted_id = candidate_state["candidates"][-1]["id"]
        status, _, result = self.api("POST", self.path + "/revisions/accept", self.base.accept_body(candidate_state))
        self.assertEqual(status, 200, result)
        original_files = {p: p.read_bytes() for p in self.server.discussions.root.glob("*.json")}
        calls_before = len(self.discussion_calls)

        second, creation = self.create()
        sid = second["session_id"]
        self.assertNotEqual(sid, first)
        self.assertEqual(second["turns"], [])
        self.assertEqual(second["opinion_revision"], 1)
        self.assertEqual(len(self.discussion_calls), calls_before)
        self.assertTrue(all(p.read_bytes() == data for p, data in original_files.items()))
        status, _, repeat = self.api("POST", self.path + "/sessions", creation)
        self.assertEqual(status, 200, repeat)
        self.assertEqual(repeat["session_id"], sid)
        self.assertEqual(len(repeat["sessions"]), 2)

        self.send(sid, "NEW_ONLY_MARKER_20260924")
        sent = json.dumps(self.discussion_calls[-1], ensure_ascii=False)
        self.assertIn("NEW_ONLY_MARKER_20260924", sent)
        self.assertNotIn("OLD_ONLY_MARKER_20260924", sent)
        self.assertIn("讨论后：分别约定工资支付与交接责任", sent)
        old = self.get(first)
        self.assertEqual(len(old["turns"]), 1)
        self.assertEqual(old["turns"][0]["user"], "OLD_ONLY_MARKER_20260924")
        self.assertEqual(self.get()["session_id"], sid)  # Viewing history does not change the default.
        old_versions, new_versions = self.get(first, True), self.get(sid, True)
        self.assertEqual(len(old_versions["candidates"]), 1)
        self.assertEqual(new_versions["candidates"], [])
        self.assertEqual(new_versions["accepted_candidate_id"], accepted_id)
        self.assertEqual(new_versions["accepted_candidate_session_id"], first)

        thread = self.get(sid)
        body = {"session_id": sid, "expected_revision": new_versions["revision"],
                "expected_opinion_revision": new_versions["opinion_revision"],
                "expected_discussion_revision": thread["revision"],
                "expected_delivery_revision": thread["context"]["delivery_revision"],
                "request_id": str(uuid.uuid4()), "config": self.base.config, "consent": True}
        status, _, result = self.api("POST", self.path + "/revisions", body)
        self.assertIn(status, (200, 202), result)
        versions = self.base.wait("/revisions?session_id=" + sid, "pending_candidate_id")
        self.assertEqual(versions["candidates"][0]["status"], "completed", versions)
        self.assertEqual(versions["candidates"][0]["session_id"], sid)
        sent = json.dumps(self.base.calls[-1], ensure_ascii=False)
        self.assertNotIn("OLD_ONLY_MARKER_20260924", sent)
        self.assertIn("NEW_ONLY_MARKER_20260924", sent)
        self.assertEqual(len(self.get(first, True)["candidates"]), 1)

        wrong = self.base.accept_body(versions)
        wrong.update(session_id=sid, candidate_id=accepted_id)
        status, _, _ = self.api("POST", self.path + "/revisions/accept", wrong)
        self.assertEqual(status, 409)
        report = self.api("GET", "/api/reports/detailed")[2]["report"]
        self.assertEqual(next(f for f in report["findings"] if f["id"] == "F01")["opinion_revision"], 1)
        self.assertEqual(self.base.raw_path.read_bytes(), self.base.raw_before)

    def test_session_query_and_ambiguous_legacy_send_are_rejected(self):
        for query in ("?session_id=initial&session_id=initial", "?session_id=", "?session_id=..%2Fother", "?other=1"):
            self.assertEqual(self.api("GET", self.path + query)[0], 400)
            self.assertEqual(self.api("GET", self.path + "/revisions" + query)[0], 400)
        self.assertEqual(self.api("POST", self.path + "/sessions", {}, origin=False)[0], 403)
        state, _ = self.create()
        status, _, _ = self.api("POST", self.path, {
            "expected_revision": state["revision"], "expected_opinion_revision": state["opinion_revision"],
            "expected_delivery_revision": state["context"]["delivery_revision"],
            "request_id": str(uuid.uuid4()), "message": "不能误发进新会话", "config": self.base.config, "consent": True,
        })
        self.assertEqual(status, 409)
        self.assertFalse(self.discussion_calls)


if __name__ == "__main__":
    unittest.main()
