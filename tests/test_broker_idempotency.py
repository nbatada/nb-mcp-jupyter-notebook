"""Focused broker replay check; no Jupyter server or kernel is started."""

import time
import unittest
from unittest.mock import patch

from tornado.web import HTTPError

from nb_analysis_bridge import server_extension


class _Handler:
    def __init__(self, body):
        self.body = body
        self.response = None

    def get_json_body(self):
        return self.body

    def finish(self, response):
        self.response = response


class BrokerIdempotencyTests(unittest.TestCase):
    def test_atomic_busy_result_is_idempotent_and_does_not_queue(self):
        panel = {
            "panel_id": "panel", "path": "analysis.ipynb", "session_id": "session",
            "kernel_id": "kernel", "kernel_epoch": 1, "kernel_status": "busy",
            "seen_at": time.time(),
        }
        body = {
            "panel_id": "panel", "operation": "push_execute_read", "source": "print(1)",
            "idempotency_key": "busy-step", "expected_path": "analysis.ipynb",
            "expected_session_id": "session", "expected_kernel_id": "kernel",
            "expected_kernel_epoch": 1,
        }
        state = {"panels": {"panel": panel}, "requests": {}, "artifacts": {}, "idempotency": {}}
        with patch.object(server_extension, "STATE", state):
            first = _Handler(body)
            server_extension.Requests.post.__wrapped__(first)
            request_id = first.response["request_id"]
            request = state["requests"][request_id]
            expected_result = {
                "status": "busy", "ok": False, "error": "KERNEL_BUSY",
                "session_id": "session", "kernel_id": "kernel", "kernel_epoch": 1,
            }
            self.assertEqual(first.response["status"], "done")
            self.assertEqual(request["status"], "done")
            self.assertEqual(request["result"], expected_result)
            self.assertNotIn("cell_id", request["result"])
            self.assertFalse(any(q["status"] == "queued" for q in state["requests"].values()))

            replay = _Handler(body.copy())
            server_extension.Requests.post.__wrapped__(replay)
            self.assertEqual(replay.response, {"request_id": request_id, "status": "done"})
            self.assertEqual(state["requests"][request_id]["result"], expected_result)
            self.assertEqual(len(state["requests"]), 1)

            # A pending request also forces BUSY_RETURN when the panel heartbeat is stale.
            panel["kernel_status"] = "idle"
            state["requests"]["in-flight"] = {"panel_id": "panel", "status": "running"}
            second_busy = _Handler({**body, "idempotency_key": "busy-step-2"})
            server_extension.Requests.post.__wrapped__(second_busy)
            second_request = state["requests"][second_busy.response["request_id"]]
            self.assertEqual(second_request["result"], expected_result)
            self.assertEqual(second_request["status"], "done")

            # Once the in-flight operation is gone and the panel is idle, normal enqueue resumes.
            del state["requests"]["in-flight"]
            later = _Handler({**body, "idempotency_key": "later-step"})
            server_extension.Requests.post.__wrapped__(later)
            self.assertEqual(later.response["status"], "queued")
            self.assertEqual(state["requests"][later.response["request_id"]]["status"], "queued")

    def test_atomic_replay_returns_same_request_and_rejects_changed_source(self):
        panel = {
            "panel_id": "panel", "path": "analysis.ipynb", "session_id": "session",
            "kernel_id": "kernel", "kernel_epoch": 1, "seen_at": time.time(),
        }
        body = {
            "panel_id": "panel", "operation": "push_execute_read", "source": "print(1)",
            "idempotency_key": "step", "expected_path": "analysis.ipynb",
            "expected_session_id": "session", "expected_kernel_id": "kernel",
            "expected_kernel_epoch": 1,
        }
        state = {"panels": {"panel": panel}, "requests": {}, "artifacts": {}, "idempotency": {}}
        with patch.object(server_extension, "STATE", state):
            first = _Handler(body)
            server_extension.Requests.post.__wrapped__(first)
            replay = _Handler(body.copy())
            server_extension.Requests.post.__wrapped__(replay)
            self.assertEqual(first.response["request_id"], replay.response["request_id"])
            self.assertEqual(len(state["requests"]), 1)

            changed_source = _Handler({**body, "source": "print(2)"})
            with self.assertRaises(HTTPError) as raised:
                server_extension.Requests.post.__wrapped__(changed_source)
            self.assertEqual(raised.exception.status_code, 409)

    def test_insert_replay_requires_same_anchor(self):
        panel = {
            "panel_id": "panel", "path": "analysis.ipynb", "session_id": "session",
            "kernel_id": "kernel", "kernel_epoch": 1, "seen_at": time.time(),
        }
        body = {
            "panel_id": "panel", "operation": "insert_code", "source": "print(1)",
            "after_cell_id": "first", "idempotency_key": "step",
            "expected_path": "analysis.ipynb", "expected_session_id": "session",
            "expected_kernel_id": "kernel", "expected_kernel_epoch": 1,
        }
        state = {"panels": {"panel": panel}, "requests": {}, "artifacts": {}, "idempotency": {}}
        with patch.object(server_extension, "STATE", state):
            first = _Handler(body)
            server_extension.Requests.post.__wrapped__(first)
            replay = _Handler(body.copy())
            server_extension.Requests.post.__wrapped__(replay)
            self.assertEqual(first.response["request_id"], replay.response["request_id"])

            changed_anchor = _Handler({**body, "after_cell_id": "second"})
            with self.assertRaises(HTTPError) as raised:
                server_extension.Requests.post.__wrapped__(changed_anchor)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(len(state["requests"]), 1)


if __name__ == "__main__":
    unittest.main()
