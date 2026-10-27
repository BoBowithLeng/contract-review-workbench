from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from clause_discussion import ClauseDiscussionConflict, ClauseDiscussionCorrupt, ClauseDiscussionManager
from opinion_revisions import OpinionRevisionConflict, OpinionRevisionManager
from tests.test_clause_discussion import FakeDelivery, make_loaded, request_body, wait_done
from tests.test_opinion_revisions import answer, body as revision_body, full_loaded, wait_revision


def new_body(state):
    return {"expected_sessions_revision": state["sessions_revision"],
            "expected_delivery_revision": state["context"]["delivery_revision"],
            "expected_opinion_revision": state["opinion_revision"],
            "request_id": str(uuid.uuid4())}


class DiscussionSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.loaded = full_loaded()
        self.delivery = FakeDelivery()
        self.messages = []
        self.discussions = ClauseDiscussionManager(self.root / "discussions", self.delivery,
            completion=lambda _, msgs: self.messages.append(msgs) or "已回答。")

    def test_new_session_preserves_initial_and_sends_no_old_history(self):
        initial = self.discussions.get(self.loaded, "F01")
        self.assertEqual(initial["session_id"], "initial")
        self.assertEqual(initial["sessions_revision"], 0)
        self.assertFalse(self.discussions._sessions_path(self.loaded, "F01").exists())
        old_request = request_body("旧轮问题")
        self.discussions.start(self.loaded, "F01", old_request)
        wait_done(self.discussions, self.loaded)
        first_path = self.discussions._path(self.loaded, "F01")
        old_bytes = first_path.read_bytes()
        state = self.discussions.get(self.loaded, "F01")
        created, did_create = self.discussions.new_session(self.loaded, "F01", new_body(state))
        self.assertTrue(did_create)
        self.assertEqual(created["turns"], [])
        self.assertEqual(created["session_id"], created["active_session_id"])
        self.assertNotEqual(created["session_id"], "initial")
        self.assertEqual(created["sessions_revision"], 1)
        self.assertEqual([x["turn_count"] for x in created["sessions"]], [1, 0])
        self.assertEqual(first_path.read_bytes(), old_bytes)
        old = self.discussions.get(self.loaded, "F01", "initial")
        self.assertEqual(old["turns"][0]["user"], "旧轮问题")
        self.assertEqual(old["active_session_id"], created["session_id"])
        with self.assertRaises(ClauseDiscussionConflict):
            self.discussions.start(self.loaded, "F01", request_body("不指定会话", revision=0))
        with self.assertRaisesRegex(ClauseDiscussionConflict, "request_id 已用于另一轮"):
            self.discussions.start(self.loaded, "F01", {**request_body("新轮误重试", request_id=old_request["request_id"]),
                                                       "session_id": created["session_id"]})
        req = request_body("新轮问题", revision=0)
        req["session_id"] = created["session_id"]
        self.discussions.start(self.loaded, "F01", req)
        wait_done(self.discussions, self.loaded, session_id=created["session_id"])
        self.assertNotIn("旧轮问题", json.dumps(self.messages[-1], ensure_ascii=False))
        self.assertEqual(self.discussions.get(self.loaded, "F01")["session_id"], created["session_id"])

    def test_new_session_idempotency_and_bad_index_not_reset(self):
        state = self.discussions.get(self.loaded, "F01")
        payload = new_body(state)
        first, created = self.discussions.new_session(self.loaded, "F01", payload)
        self.assertTrue(created)
        again, created = self.discussions.new_session(self.loaded, "F01", payload)
        self.assertFalse(created)
        self.assertEqual(again["session_id"], first["session_id"])
        self.assertEqual(len(again["sessions"]), 2)
        with self.assertRaises(ClauseDiscussionConflict):
            self.discussions.new_session(self.loaded, "F01", new_body(state))
        path = self.discussions._sessions_path(self.loaded, "F01")
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ClauseDiscussionCorrupt):
            self.discussions.get(self.loaded, "F01")
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_revision_candidate_scoped_but_acceptance_global(self):
        manager = OpinionRevisionManager(self.root / "revisions", self.discussions,
                                         completion=lambda _, msgs: answer(msgs))
        initial = manager.get(self.loaded, "F01")
        manager.start(self.loaded, "F01", revision_body(initial))
        done = wait_revision(manager, self.loaded)
        first_id = done["candidates"][0]["id"]
        manager.accept(self.loaded, "F01", {"expected_revision": done["revision"], "expected_opinion_revision": 0,
                       "expected_discussion_revision": 0, "expected_delivery_revision": 0, "candidate_id": first_id})
        sidecar = manager._path(self.loaded, "F01")
        old_state = json.loads(sidecar.read_text(encoding="utf-8"))
        old_state["candidates"][0].pop("session_id")  # Pre-session record compatibility.
        sidecar.write_text(json.dumps(old_state, ensure_ascii=False), encoding="utf-8")
        applied = manager.apply(self.loaded)
        fresh, created = self.discussions.new_session(applied, "F01", new_body(self.discussions.get(applied, "F01")))
        self.assertTrue(created)
        sid = fresh["session_id"]
        selected = manager.get(applied, "F01")
        self.assertEqual(selected["session_id"], sid)
        self.assertEqual(selected["candidates"], [])
        self.assertEqual(selected["accepted_candidate_id"], first_id)
        self.assertEqual(selected["accepted_candidate_session_id"], "initial")
        self.assertEqual(selected["current_finding"]["reason"], "讨论后需将验收期延长。")
        with self.assertRaises(OpinionRevisionConflict):
            manager.start(applied, "F01", revision_body(selected))
        new_request = revision_body(selected)
        new_request["session_id"] = sid
        manager.start(applied, "F01", new_request)
        result = wait_revision(manager, applied)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["session_id"], sid)
        self.assertEqual(result["candidates"][0]["base_finding"]["reason"], "讨论后需将验收期延长。")
        old = manager.get(applied, "F01", "initial")
        self.assertEqual(len(old["candidates"]), 1)
        self.assertEqual(old["candidates"][0]["id"], first_id)
        self.assertEqual(manager.apply(self.loaded)["report"]["findings"][0]["opinion_revision"], 1)


if __name__ == "__main__":
    unittest.main()
