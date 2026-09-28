"""Focused setup and shutdown checks; no Jupyter server or kernel is started."""

import tempfile
import unittest
from unittest.mock import patch

from nb_analysis_bridge import mcp_server, server_extension


class LifecycleTests(unittest.TestCase):
    def test_start_reuses_one_matching_server(self):
        with tempfile.TemporaryDirectory() as root:
            server = {"url": "http://127.0.0.1:8898/", "root_dir": root}
            with patch.object(mcp_server, "_local_servers", return_value=[server]), \
                 patch.object(mcp_server, "_ready_server", return_value={
                     "server_url": server["url"], "root_dir": root, "ready": True}), \
                 patch.object(mcp_server.subprocess, "Popen") as launch:
                result = mcp_server.start_jupyter(root)
            self.assertTrue(result["reused"])
            launch.assert_not_called()

    def test_open_existing_without_frontend_returns_embedded_browser_url(self):
        def response(_base, _token, path, method="GET", data=None):
            if path == "nb-analysis/panels":
                return {"protocol_version": 2, "panels": []}
            if path == "nb-analysis/clients":
                return {"clients": []}
            if path == "nb-analysis/open":
                return {"status": "needs_browser", "path": data["path"]}
            raise AssertionError(path)

        with patch.object(mcp_server, "_connection", return_value=("http://127.0.0.1:8898/", "secret")), \
             patch.object(mcp_server, "_contents_metadata", return_value={"type": "notebook"}), \
             patch.object(mcp_server, "request", side_effect=response):
            result = mcp_server.open_notebook("analysis.ipynb")
        self.assertEqual(result["status"], "needs_browser")
        self.assertEqual(result["browser_url"],
                         "http://127.0.0.1:8898/lab/tree/analysis.ipynb?token=secret")

    def test_new_notebook_uses_server_default_in_another_environment(self):
        calls = []

        def response(_base, _token, path, method="GET", data=None):
            calls.append((path, method, data))
            if path == "nb-analysis/panels":
                return {"protocol_version": 2, "panels": []}
            if path == "nb-analysis/clients":
                return {"clients": [{"client_id": "browser"}]}
            if path == "api/kernelspecs":
                return {"default": "lab-python", "kernelspecs": {"lab-python": {"spec": {
                    "argv": ["/another/project/env/bin/python"],
                    "display_name": "Lab Python", "language": "python"}}}}
            if path == "api/contents/" and method == "POST":
                return {"path": "Untitled.ipynb", "type": "notebook"}
            if path == "api/contents/Untitled.ipynb" and method == "PATCH":
                return {"path": "new.ipynb", "type": "notebook"}
            if path == "api/contents/new.ipynb" and method == "PUT":
                return {"type": "notebook"}
            if path == "nb-analysis/open":
                return {"request_id": "open-1", "status": "queued"}
            raise AssertionError((path, method))

        with patch.object(mcp_server, "_connection", return_value=("http://127.0.0.1:8898/", "secret")), \
             patch.object(mcp_server, "_contents_metadata", return_value=None), \
             patch.object(mcp_server, "request", side_effect=response), \
             patch.dict(mcp_server.os.environ, {"NBIDE_DEFAULT_KERNEL_NAME": ""}), \
             patch.object(mcp_server, "_wait_open", return_value={"status": "done", "result": {"panel_id": "panel"}}):
            result = mcp_server.open_notebook("new.ipynb", create=True)
        saved = next(data for path, method, data in calls if path == "api/contents/new.ipynb" and method == "PUT")
        self.assertEqual(saved["content"]["metadata"]["kernelspec"],
                         {"name": "lab-python", "display_name": "Lab Python", "language": "python"})
        renamed = next(data for path, method, data in calls if method == "PATCH")
        self.assertEqual(renamed["path"], "new.ipynb")
        opened = next(data for path, method, data in calls if path == "nb-analysis/open")
        self.assertEqual(opened["kernel_name"], "lab-python")
        self.assertEqual(result["kernel_name"], "lab-python")
        self.assertTrue(result["created"])

    def test_explicit_kernel_overrides_configured_and_server_defaults(self):
        catalog = {"default": "python", "kernelspecs": {
            "python": {"spec": {"language": "python"}},
            "research-r": {"spec": {"language": "R", "display_name": "Research R"}},
        }}
        with patch.dict(mcp_server.os.environ, {"NBIDE_DEFAULT_KERNEL_NAME": "python"}):
            name, spec = mcp_server._choose_kernelspec(catalog, "research-r")
        self.assertEqual((name, spec["language"]), ("research-r", "R"))

    def test_existing_notebook_rejects_kernel_change(self):
        with patch.object(mcp_server, "_connection") as connect:
            with self.assertRaisesRegex(ValueError, "existing kernels are preserved"):
                mcp_server.open_notebook("existing.ipynb", kernel_name="different")
            connect.assert_not_called()

    def test_shared_kernel_shutdown_is_refused_before_dispatch(self):
        panel = {"panel_id": "p", "path": "analysis.ipynb", "session_id": "s1",
                 "kernel_id": "k", "kernel_epoch": 1}
        with patch.object(mcp_server, "_bound", return_value=("server", "token", panel)), \
             patch.object(mcp_server, "request", return_value=[
                 {"id": "s1", "kernel": {"id": "k"}},
                 {"id": "s2", "kernel": {"id": "k"}}]), \
             patch.object(mcp_server, "_submit") as submit:
            with self.assertRaisesRegex(RuntimeError, "shared"):
                mcp_server.shutdown_kernel("binding")
            submit.assert_not_called()


class OpenBrokerTests(unittest.TestCase):
    def test_no_frontend_does_not_queue_an_open_request(self):
        state = {"panels": {}, "requests": {}, "artifacts": {}, "idempotency": {},
                 "clients": {}, "open_requests": {}}

        class Handler:
            def get_json_body(self):
                return {"path": "analysis.ipynb"}

            def finish(self, result):
                self.result = result

        with patch.object(server_extension, "STATE", state):
            handler = Handler()
            server_extension.OpenNotebook.post.__wrapped__(handler)
        self.assertEqual(handler.result["status"], "needs_browser")
        self.assertEqual(state["open_requests"], {})

    def test_closed_panel_is_retired_immediately(self):
        state = {"panels": {"panel": {"browser_client_id": "browser"}}}

        class Handler:
            def get_json_body(self):
                return {"panel_id": "panel", "browser_client_id": "browser"}

            def finish(self, result):
                self.result = result

        with patch.object(server_extension, "STATE", state):
            handler = Handler()
            server_extension.RetirePanel.post.__wrapped__(handler)
        self.assertTrue(handler.result["ok"])
        self.assertEqual(state["panels"], {})


if __name__ == "__main__":
    unittest.main()
