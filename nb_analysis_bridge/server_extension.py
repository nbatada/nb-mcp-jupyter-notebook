"""Authenticated request broker inside the existing Jupyter Server.

The browser extension owns notebook edits and execution. This module never
starts a kernel or writes an open notebook file.
"""

import base64
import binascii
import hashlib
from pathlib import PurePosixPath
import time
import uuid

import tornado.web
from jupyter_server.base.handlers import APIHandler
from jupyter_server.utils import url_path_join


PANEL_TTL_SECONDS = 15
DISCONNECTED_REQUEST_GRACE_SECONDS = 60
ARTIFACT_TTL_SECONDS = 3600
MAX_TEXT_CHARS = 32_000
MAX_ARTIFACT_BYTES = 8_000_000
MAX_ARTIFACT_STORE_BYTES = 64_000_000
RESULT_TTL_SECONDS = 3600
CLIENT_TTL_SECONDS = 15
OPERATIONS = {"insert_code", "insert_markdown", "snapshot", "get_cell", "update_cell", "delete_cell", "execute", "push_execute_read", "shutdown_kernel", "close_notebook"}
STATE = {"panels": {}, "requests": {}, "artifacts": {}, "idempotency": {},
         "clients": {}, "open_requests": {}}


def _live_clients():
    now = time.time()
    for client_id, client in list(STATE["clients"].items()):
        if now - client["seen_at"] > CLIENT_TTL_SECONDS:
            del STATE["clients"][client_id]
    return STATE["clients"]


def _notebook_path(path):
    if (not isinstance(path, str) or not path.endswith(".ipynb") or
            not path or "\\" in path or "\x00" in path or
            PurePosixPath(path).is_absolute() or
            any(part in {"", ".", ".."} for part in path.split("/"))):
        raise tornado.web.HTTPError(400, "expected a relative .ipynb path")
    return path


def _live_panels():
    now = time.time()
    for panel_id, panel in list(STATE["panels"].items()):
        if now - panel["seen_at"] > PANEL_TTL_SECONDS:
            del STATE["panels"][panel_id]
    return STATE["panels"]


def _register_panel(panel):
    for old_id, old in list(STATE["panels"].items()):
        if (old_id != panel["panel_id"] and old.get("browser_client_id")
                and old["browser_client_id"] == panel["browser_client_id"]
                and old["path"] == panel["path"]):
            del STATE["panels"][old_id]
            for request in STATE["requests"].values():
                if request["panel_id"] == old_id and request["status"] in {"queued", "running"}:
                    request["status"] = "unknown"
                    request["finished_at"] = time.time()
    STATE["panels"][panel["panel_id"]] = panel


def _expire_requests():
    now = time.time()
    live = _live_panels()
    clients = _live_clients()
    for item in STATE["open_requests"].values():
        if item["status"] in {"queued", "running"} and item["client_id"] not in clients:
            item["status"] = "unknown"
            item["error"] = "JUPYTERLAB_CLIENT_DISCONNECTED"
            item["finished_at"] = now
    for request_id, item in list(STATE["open_requests"].items()):
        if item.get("finished_at") and now - item["finished_at"] > RESULT_TTL_SECONDS:
            del STATE["open_requests"][request_id]
    for request in STATE["requests"].values():
        disconnected = request["panel_id"] not in live
        since = request.get("claimed_at", request["created_at"])
        if request["status"] in {"queued", "running"} and disconnected and now - since > DISCONNECTED_REQUEST_GRACE_SECONDS:
            request["status"] = "unknown"
            request["finished_at"] = now
    for artifact_id, artifact in list(STATE["artifacts"].items()):
        if now - artifact["created_at"] > ARTIFACT_TTL_SECONDS:
            del STATE["artifacts"][artifact_id]
    for request_id, request in list(STATE["requests"].items()):
        finished = request.get("finished_at")
        if finished and now - finished > RESULT_TTL_SECONDS:
            del STATE["requests"][request_id]
    for key, request_id in list(STATE["idempotency"].items()):
        if request_id not in STATE["requests"]:
            del STATE["idempotency"][key]


def _bounded_text(value):
    text = "".join(value) if isinstance(value, list) else str(value or "")
    return {"text": text[:MAX_TEXT_CHARS], "truncated": len(text) > MAX_TEXT_CHARS, "characters": len(text)}


def _artifact(mime, value):
    try:
        if mime == "image/png":
            encoded = "".join(value) if isinstance(value, list) else value
            encoded = "".join(encoded.split())
            raw = base64.b64decode(encoded, validate=True)
        else:
            raw = ("".join(value) if isinstance(value, list) else value).encode("utf-8")
    except (binascii.Error, ValueError, TypeError):
        return {"mime": mime, "error": "invalid image data"}
    if len(raw) > MAX_ARTIFACT_BYTES:
        return {"mime": mime, "bytes": len(raw), "omitted": True}
    while STATE["artifacts"] and sum(len(item["data"]) for item in STATE["artifacts"].values()) + len(raw) > MAX_ARTIFACT_STORE_BYTES:
        oldest = min(STATE["artifacts"], key=lambda key: STATE["artifacts"][key]["created_at"])
        del STATE["artifacts"][oldest]
    artifact_id = str(uuid.uuid4())
    STATE["artifacts"][artifact_id] = {"mime": mime, "data": raw, "created_at": time.time()}
    return {"artifact_id": artifact_id, "mime": mime, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _summarize_output(output):
    kind = output.get("output_type")
    if kind == "stream":
        return {"output_type": kind, "name": output.get("name", "stdout"), **_bounded_text(output.get("text"))}
    if kind == "error":
        return {"output_type": kind, "ename": output.get("ename"), "evalue": _bounded_text(output.get("evalue")), "traceback": _bounded_text("\n".join(output.get("traceback", [])))}
    if kind == "omitted":
        return {"output_type": kind, "omitted": True, "characters": output.get("characters")}
    data = output.get("data") or {}
    summary = {"output_type": kind, "available_mime": list(data)}
    if "text/plain" in data:
        summary["text_plain"] = _bounded_text(data["text/plain"])
    if "text/html" in data:
        summary["text_html"] = _bounded_text(data["text/html"])
    summary["artifacts"] = [_artifact(mime, data[mime]) for mime in ("image/png", "image/svg+xml") if mime in data]
    return summary


class Panels(APIHandler):
    @tornado.web.authenticated
    def get(self):
        self.finish({"protocol_version": 2, "panels": list(_live_panels().values())})

    @tornado.web.authenticated
    def post(self):
        data = self.get_json_body() or {}
        if not all(isinstance(data.get(key), str) and data[key] for key in ("panel_id", "path")):
            raise tornado.web.HTTPError(400, "panel_id and path are required")
        panel = {
            "panel_id": data["panel_id"],
            "browser_client_id": data.get("browser_client_id"),
            "path": data["path"],
            "session_id": data.get("session_id"),
            "kernel_id": data.get("kernel_id"),
            "kernel_epoch": data.get("kernel_epoch", 0),
            "kernel_status": data.get("kernel_status", "unknown"),
            "active": data.get("active") is True,
            "seen_at": time.time(),
        }
        _register_panel(panel)
        self.finish({"ok": True})


class RetirePanel(APIHandler):
    @tornado.web.authenticated
    def post(self):
        data = self.get_json_body() or {}
        panel = STATE["panels"].get(data.get("panel_id"))
        if panel and panel.get("browser_client_id") != data.get("browser_client_id"):
            raise tornado.web.HTTPError(409, "panel belongs to another JupyterLab client")
        if panel:
            del STATE["panels"][data["panel_id"]]
        self.finish({"ok": True})


class Clients(APIHandler):
    @tornado.web.authenticated
    def get(self):
        self.finish({"clients": list(_live_clients().values())})

    @tornado.web.authenticated
    def post(self):
        client_id = (self.get_json_body() or {}).get("client_id")
        if not isinstance(client_id, str) or not client_id or len(client_id) > 128:
            raise tornado.web.HTTPError(400, "client_id is required")
        STATE["clients"][client_id] = {"client_id": client_id, "seen_at": time.time()}
        self.finish({"ok": True})


class OpenNotebook(APIHandler):
    @tornado.web.authenticated
    def post(self):
        data = self.get_json_body() or {}
        path = _notebook_path(data.get("path"))
        kernel_name = data.get("kernel_name")
        if kernel_name is not None and (not isinstance(kernel_name, str) or not kernel_name):
            raise tornado.web.HTTPError(400, "invalid kernel_name")
        clients = _live_clients()
        requested_client = data.get("client_id")
        if requested_client is not None:
            if requested_client not in clients:
                raise tornado.web.HTTPError(409, "selected JupyterLab client is not connected")
            client_id = requested_client
        elif len(clients) == 1:
            client_id = next(iter(clients))
        elif not clients:
            self.finish({"status": "needs_browser", "path": path})
            return
        else:
            raise tornado.web.HTTPError(409, "multiple JupyterLab clients; select client_id from: " +
                                        ", ".join(sorted(clients)))
        request_id = str(uuid.uuid4())
        STATE["open_requests"][request_id] = {
            "request_id": request_id, "client_id": client_id, "path": path,
            "kernel_name": kernel_name,
            "status": "queued", "created_at": time.time(),
        }
        self.finish({"request_id": request_id, "status": "queued", "client_id": client_id})


class OpenClaim(APIHandler):
    @tornado.web.authenticated
    def post(self):
        client_id = (self.get_json_body() or {}).get("client_id")
        if client_id not in _live_clients():
            raise tornado.web.HTTPError(409, "JupyterLab client is not connected")
        _expire_requests()
        if any(item["client_id"] == client_id and item["status"] == "running"
               for item in STATE["open_requests"].values()):
            self.finish({"requests": []})
            return
        queued = [item for item in STATE["open_requests"].values()
                  if item["client_id"] == client_id and item["status"] == "queued"]
        if not queued:
            self.finish({"requests": []})
            return
        item = min(queued, key=lambda entry: entry["created_at"])
        item["status"] = "running"
        self.finish({"requests": [item]})


class OpenResult(APIHandler):
    @tornado.web.authenticated
    def post(self, request_id):
        item = STATE["open_requests"].get(request_id)
        if item is None:
            raise tornado.web.HTTPError(404, "unknown open request")
        if item["status"] != "running":
            raise tornado.web.HTTPError(409, "open request is not running")
        data = self.get_json_body() or {}
        if data.get("client_id") != item["client_id"] or data.get("path") != item["path"]:
            raise tornado.web.HTTPError(409, "open result does not match request")
        item["status"] = "done" if data.get("ok") is True else "error"
        item["result"] = data
        item["finished_at"] = time.time()
        self.finish({"ok": True})

    @tornado.web.authenticated
    def get(self, request_id):
        _expire_requests()
        item = STATE["open_requests"].get(request_id)
        if item is None:
            raise tornado.web.HTTPError(404, "unknown open request")
        self.finish({"request_id": request_id, "status": item["status"],
                     "path": item["path"], "client_id": item["client_id"],
                     "result": item.get("result"), "error": item.get("error")})


class Requests(APIHandler):
    @tornado.web.authenticated
    def post(self):
        data = self.get_json_body() or {}
        panel_id = data.get("panel_id")
        panel = _live_panels().get(panel_id)
        if panel is None:
            raise tornado.web.HTTPError(409, "panel is not connected")
        if data.get("operation") not in OPERATIONS:
            raise tornado.web.HTTPError(400, "unsupported operation")
        if data["operation"] in {"insert_code", "insert_markdown", "update_cell", "push_execute_read"} and not isinstance(data.get("source"), str):
            raise tornado.web.HTTPError(400, "source is required")
        if isinstance(data.get("source"), str) and len(data["source"]) > 100_000:
            raise tornado.web.HTTPError(413, "cell source is too large")
        if data["operation"] == "execute" and not isinstance(data.get("cell_id"), str):
            raise tornado.web.HTTPError(400, "cell_id is required")
        if data["operation"] in {"execute", "update_cell", "delete_cell"} and not isinstance(data.get("expected_source_hash"), str):
            raise tornado.web.HTTPError(400, "expected_source_hash is required")
        if data["operation"] in {"get_cell", "update_cell", "delete_cell"} and not isinstance(data.get("cell_id"), str):
            raise tornado.web.HTTPError(400, "cell_id is required")
        for requested, actual in (("expected_path", "path"), ("expected_session_id", "session_id"), ("expected_kernel_id", "kernel_id"), ("expected_kernel_epoch", "kernel_epoch")):
            if data.get(requested) != panel[actual]:
                raise tornado.web.HTTPError(409, f"{actual} changed since binding")
        if data["operation"] in {"execute", "push_execute_read"} and not panel["kernel_id"]:
            raise tornado.web.HTTPError(409, "notebook has no live kernel")
        key = data.get("idempotency_key")
        if key is not None and (not isinstance(key, str) or not key or len(key) > 128):
            raise tornado.web.HTTPError(400, "invalid idempotency key")
        if key is not None:
            prior = STATE["idempotency"].get((panel_id, key))
            if prior:
                original = STATE["requests"][prior]
                for field in (
                    "operation", "cell_id", "source", "after_cell_id", "expected_source_hash",
                    "expected_path", "expected_session_id", "expected_kernel_id", "expected_kernel_epoch",
                ):
                    if original.get(field) != data.get(field):
                        raise tornado.web.HTTPError(409, "idempotency key was used for a different operation")
                self.finish({"request_id": prior, "status": STATE["requests"][prior]["status"]})
                return
        request_id = str(uuid.uuid4())
        created_at = time.time()
        if data["operation"] == "push_execute_read":
            panel_busy = panel.get("kernel_status") == "busy"
            panel_has_pending_request = any(
                request.get("panel_id") == panel_id
                and request.get("status") in {"queued", "running"}
                for request in STATE["requests"].values()
            )
            if panel_busy or panel_has_pending_request:
                STATE["requests"][request_id] = {
                    **data,
                    "request_id": request_id,
                    "status": "done",
                    "created_at": created_at,
                    "finished_at": created_at,
                    "broker_timing": {"queued_epoch_ms": created_at * 1000},
                    "result": {
                        "status": "busy",
                        "ok": False,
                        "error": "KERNEL_BUSY",
                        "session_id": panel["session_id"],
                        "kernel_id": panel["kernel_id"],
                        "kernel_epoch": panel["kernel_epoch"],
                    },
                }
                if key is not None:
                    STATE["idempotency"][(panel_id, key)] = request_id
                self.finish({"request_id": request_id, "status": "done"})
                return
        STATE["requests"][request_id] = {
            **data,
            "request_id": request_id,
            "status": "queued",
            "created_at": created_at,
            "broker_timing": {"queued_epoch_ms": created_at * 1000},
        }
        if key is not None:
            STATE["idempotency"][(panel_id, key)] = request_id
        self.finish({"request_id": request_id, "status": "queued"})


class Claim(APIHandler):
    @tornado.web.authenticated
    def post(self):
        panel_id = (self.get_json_body() or {}).get("panel_id")
        if panel_id not in _live_panels():
            raise tornado.web.HTTPError(409, "panel is not connected")
        _expire_requests()
        if any(q["panel_id"] == panel_id and q["status"] == "running" for q in STATE["requests"].values()):
            self.finish({"requests": []})
            return
        queued = [q for q in STATE["requests"].values() if q["panel_id"] == panel_id and q["status"] == "queued"]
        if not queued:
            self.finish({"requests": []})
            return
        request = min(queued, key=lambda q: q["created_at"])
        request["status"] = "running"
        request["claimed_at"] = time.time()
        request["broker_timing"]["claimed_epoch_ms"] = request["claimed_at"] * 1000
        self.finish({"requests": [request]})


class Result(APIHandler):
    @tornado.web.authenticated
    def post(self, request_id):
        request = STATE["requests"].get(request_id)
        if request is None:
            raise tornado.web.HTTPError(404, "unknown request")
        if request["status"] != "running":
            raise tornado.web.HTTPError(409, "request is not running")
        result = self.get_json_body() or {}
        if result.get("panel_id") != request["panel_id"]:
            raise tornado.web.HTTPError(409, "result panel does not match request")
        request["broker_timing"]["result_received_epoch_ms"] = time.time() * 1000
        if isinstance(result.get("outputs"), list):
            result["outputs"] = [_summarize_output(output) for output in result["outputs"]]
        request["broker_timing"]["result_processed_epoch_ms"] = time.time() * 1000
        request["result"] = result
        request["status"] = "done"
        request["finished_at"] = time.time()
        self.finish({"ok": True})

    @tornado.web.authenticated
    def get(self, request_id):
        _expire_requests()
        request = STATE["requests"].get(request_id)
        if request is None:
            raise tornado.web.HTTPError(404, "unknown request")
        self.finish({
            "request_id": request_id,
            "status": request["status"],
            "result": request.get("result"),
            "broker_timing": request.get("broker_timing"),
        })


class Artifact(APIHandler):
    @tornado.web.authenticated
    def get(self, artifact_id):
        _expire_requests()
        artifact = STATE["artifacts"].get(artifact_id)
        if artifact is None:
            raise tornado.web.HTTPError(404, "artifact not found or expired")
        self.set_header("Content-Disposition", "attachment")
        self.set_header("X-Content-Type-Options", "nosniff")
        self.finish(artifact["data"], set_content_type=artifact["mime"])


def _load_jupyter_server_extension(app):
    base = app.web_app.settings["base_url"]
    app.web_app.add_handlers(".*$", [
        (url_path_join(base, "nb-analysis", "panels"), Panels),
        (url_path_join(base, "nb-analysis", "panels", "retire"), RetirePanel),
        (url_path_join(base, "nb-analysis", "clients"), Clients),
        (url_path_join(base, "nb-analysis", "open"), OpenNotebook),
        (url_path_join(base, "nb-analysis", "open", "claim"), OpenClaim),
        (url_path_join(base, "nb-analysis", "open", "result", r"([^/]+)"), OpenResult),
        (url_path_join(base, "nb-analysis", "requests"), Requests),
        (url_path_join(base, "nb-analysis", "requests", "claim"), Claim),
        (url_path_join(base, "nb-analysis", "result", r"([^/]+)"), Result),
        (url_path_join(base, "nb-analysis", "artifact", r"([^/]+)"), Artifact),
    ])
    app.log.info("NB Analysis Bridge broker loaded")
