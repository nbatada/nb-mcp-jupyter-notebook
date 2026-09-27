"""Focused adapter tests; no Jupyter server or kernel is started."""

import unittest
from unittest.mock import patch

from nb_analysis_bridge import mcp_server


BOUND = {"panel_id": "panel", "path": "analysis.ipynb", "session_id": "session",
         "kernel_id": "kernel", "kernel_epoch": 3}


class PushExecuteReadTests(unittest.TestCase):
    @patch.object(mcp_server, "_submit")
    def test_one_submission_returns_outputs_identity_and_timing(self, submit):
        submit.return_value = {
            "request_id": "atomic-id", "status": "done", "bound_panel": BOUND,
            "broker_timing": {"queued_epoch_ms": 1},
            "mcp_timing": {"bridge_dispatched_epoch_ms": 2},
            "result": {
                "ok": True, "status": "ok", "cell_id": "cell", "source_hash": "hash",
                "execution_count": 7, "session_id": "session", "kernel_id": "kernel",
                "kernel_epoch": 3, "timing": {"t3_cell_inserted_epoch_ms": 3,
                                               "t6_execution_resolved_epoch_ms": 4},
                "outputs": [
                    {"output_type": "stream", "name": "stdout", "text": "answer\n"},
                    {"output_type": "display_data", "text_plain": {"text": "plot"},
                     "artifacts": [{"artifact_id": "image", "mime": "image/png"}]},
                ]},
        }

        result = mcp_server.push_execute_read("binding", "print('answer')", idempotency_key="step")

        submit.assert_called_once_with("binding", "push_execute_read",
                                       {"source": "print('answer')"}, "step", 60,
                                       require_kernel=True)
        self.assertEqual(result["operation_id"], "atomic-id")
        self.assertEqual((result["cell_id"], result["source_hash"]), ("cell", "hash"))
        self.assertEqual(result["stdout"], "answer\n")
        self.assertEqual(result["text_plain"], "plot")
        self.assertEqual(result["artifacts"][0]["artifact_id"], "image")
        self.assertEqual((result["session_id"], result["kernel_id"], result["restart_epoch"]),
                         ("session", "kernel", 3))
        self.assertEqual(result["execution_status"], "ok")
        self.assertEqual(result["timing"]["frontend"]["t3_cell_inserted_epoch_ms"], 3)
        self.assertLessEqual(result["t0_mcp_received_epoch_ms"],
                             result["t8_mcp_response_prepared_epoch_ms"])

    @patch.object(mcp_server, "_submit")
    def test_uncertain_timeout_reports_one_recovery_id_without_retry(self, submit):
        uncertain = TimeoutError("execution still running")
        uncertain.request_id = "atomic-id"
        submit.side_effect = uncertain

        result = mcp_server.push_execute_read("binding", "print(1)", idempotency_key="step")

        self.assertEqual(submit.call_count, 1)
        self.assertEqual(result["execution_status"], "unknown")
        self.assertEqual(result["operation_id"], "atomic-id")
        self.assertEqual(result["idempotency_key"], "step")

    @patch.object(mcp_server, "_submit")
    def test_error_after_insert_keeps_cell_and_traceback(self, submit):
        submit.return_value = {
            "request_id": "atomic-id", "status": "done", "bound_panel": BOUND,
            "result": {"ok": False, "status": "error", "cell_id": "cell",
                       "source_hash": "hash", "execution_count": 8,
                       "error": "ValueError", "outputs": [
                           {"output_type": "error", "ename": "ValueError",
                            "traceback": {"text": "ValueError: failed"}}]},
        }

        result = mcp_server.push_execute_read("binding", "raise ValueError()", idempotency_key="step")

        self.assertEqual(submit.call_count, 1)
        self.assertEqual(result["execution_status"], "error")
        self.assertFalse(result["ok"])
        self.assertEqual((result["cell_id"], result["source_hash"]), ("cell", "hash"))
        self.assertEqual(result["traceback"], "ValueError: failed")

    @patch.object(mcp_server, "_bound", return_value=("server", "token", {
        **BOUND, "kernel_id": None}))
    @patch.object(mcp_server, "request")
    def test_kernel_preflight_prevents_insert(self, request, bound):
        with self.assertRaisesRegex(RuntimeError, "no existing live kernel"):
            mcp_server._submit("binding", "push_execute_read", {"source": "print(1)"},
                               "key", 1, require_kernel=True)

        bound.assert_called_once_with("binding")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
