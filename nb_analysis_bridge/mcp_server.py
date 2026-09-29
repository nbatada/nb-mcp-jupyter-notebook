"""MCP transport for the existing, browser-owned NB Analysis Bridge.

Notebook creation and opening use Jupyter Server and JupyterLab. Analysis
operations still use only a live NotebookPanel and its existing kernel.
"""

import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.parse

from jupyter_server.serverapp import list_running_servers
from mcp.server.mcpserver import MCPServer

from .cli import download_artifact, request, resolve_server, wait_for_result


BINDINGS = {}


def _connection(server_url: str | None):
    return resolve_server(server_url or os.getenv("NBIDE_JUPYTER_URL"), os.getenv("JUPYTER_TOKEN"))


def _panel(base: str, token: str, panel_id: str):
    listing = request(base, token, "nb-analysis/panels")
    if listing.get("protocol_version") != 2:
        raise RuntimeError("The running Jupyter server does not support bridge protocol 2")
    matches = [panel for panel in listing["panels"] if panel["panel_id"] == panel_id]
    if len(matches) != 1:
        raise RuntimeError("Panel is not connected; call list_live_notebooks and bind explicitly")
    return matches[0]


def _bound(binding_id: str):
    bound = BINDINGS.get(binding_id)
    if bound is None:
        raise RuntimeError("Binding expired with the MCP process; call bind_notebook again")
    base, token, original = bound
    current = _panel(base, token, original["panel_id"])
    for field in ("path", "session_id", "kernel_id", "kernel_epoch"):
        if current[field] != original[field]:
            raise RuntimeError(f"BOUND_NOTEBOOK_OR_KERNEL_CHANGED: {field}; bind again after inspection")
    return base, token, current


def _submit(binding_id: str, operation: str, fields: dict,
            idempotency_key: str | None, timeout: float, require_kernel: bool = False):
    if not 0 < timeout <= 300:
        raise ValueError("timeout must be between 0 and 300 seconds")
    base, token, panel = _bound(binding_id)
    panel_id = panel["panel_id"]
    if (operation == "execute" or require_kernel) and (not panel["session_id"] or not panel["kernel_id"]):
        raise RuntimeError("The selected browser notebook has no existing live kernel")
    payload = {
        "panel_id": panel_id,
        "operation": operation,
        "expected_path": panel["path"],
        "expected_session_id": panel["session_id"],
        "expected_kernel_id": panel["kernel_id"],
        "expected_kernel_epoch": panel["kernel_epoch"],
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        **fields,
    }
    dispatched_epoch_ms = time.time() * 1000
    submitted = request(base, token, "nb-analysis/requests", "POST", payload)
    accepted_epoch_ms = time.time() * 1000
    try:
        outcome = wait_for_result(base, token, submitted["request_id"], timeout)
    except TimeoutError as error:
        timeout_error = TimeoutError(f"{error}; idempotency_key={payload['idempotency_key']}")
        timeout_error.request_id = submitted["request_id"]
        timeout_error.idempotency_key = payload["idempotency_key"]
        raise timeout_error from error
    return {
        **outcome,
        "idempotency_key": payload["idempotency_key"],
        "mcp_timing": {
            "bridge_dispatched_epoch_ms": dispatched_epoch_ms,
            "bridge_accepted_epoch_ms": accepted_epoch_ms,
        },
        "bound_panel": {
            "panel_id": panel_id,
            "path": panel["path"],
            "session_id": panel["session_id"],
            "kernel_id": panel["kernel_id"],
            "kernel_epoch": panel["kernel_epoch"],
        },
    }


mcp = MCPServer(
    "nb-mcp-jupyter-notebook",
    instructions=(
        "For setup, start_jupyter and open_notebook can prepare a local JupyterLab panel. "
        "For analysis, first list_notebooks and bind_notebook "
        "to an explicit panel_id. Inspect live cells with list_cells/get_cell. Insert and "
        "execute through this MCP; do not use an owned kernel or edit an open ipynb "
        "on disk. Carry stable cell_id and source_hash. On conflict or unknown status, "
        "inspect state before retrying. Results and figures come from the browser "
        "notebook's existing session and kernel."
    ),
)


def _local_servers(project_root: Path) -> list[dict]:
    return [server for server in list_running_servers()
            if urllib.parse.urlsplit(server["url"]).hostname in {"127.0.0.1", "localhost", "::1"}
            and server.get("root_dir")
            and Path(server["root_dir"]).resolve() == project_root]


def _ready_server(server: dict) -> dict:
    base, token = _connection(server["url"])
    listing = request(base, token, "nb-analysis/panels")
    if listing.get("protocol_version") != 2:
        raise RuntimeError("The Jupyter server does not have bridge protocol 2")
    request(base, token, "nb-analysis/clients")
    return {"server_url": base, "root_dir": server.get("root_dir"), "ready": True}


@mcp.tool()
def start_jupyter(project_root: str, python_executable: str | None = None,
                  timeout: float = 30, idle_kernel_timeout_hours: int = 24,
                  cull_connected: bool = False) -> dict:
    """Reuse or start local JupyterLab; new servers cull idle kernels after 24 hours by default."""
    root = Path(project_root).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("project_root must be an existing absolute directory")
    root = root.resolve()
    if not 0 < timeout <= 120:
        raise ValueError("timeout must be between 0 and 120 seconds")
    if (isinstance(idle_kernel_timeout_hours, bool) or
            not isinstance(idle_kernel_timeout_hours, int) or
            not 0 <= idle_kernel_timeout_hours <= 168):
        raise ValueError("idle_kernel_timeout_hours must be 0 (off) or 1 to 168 hours")
    if not isinstance(cull_connected, bool):
        raise ValueError("cull_connected must be true or false")
    existing = _local_servers(root)
    if len(existing) > 1:
        raise RuntimeError("Multiple Jupyter servers serve this project; select one explicitly")
    if existing:
        return {**_ready_server(existing[0]), "reused": True,
                "cull_policy_applied": False,
                "cull_policy_note": "Existing server settings were not changed"}
    python = Path(python_executable or sys.executable).expanduser()
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("python_executable must be an executable absolute path")
    with tempfile.NamedTemporaryFile(prefix="nbide-jupyter-", suffix=".log", delete=False) as log:
        log_path = log.name
        process = subprocess.Popen(
            [str(python), "-m", "jupyterlab", "--no-browser",
             "--ServerApp.ip=127.0.0.1", "--ServerApp.port=8888",
             "--ServerApp.port_retries=100",
             f"--ServerApp.root_dir={root}",
             f"--MappingKernelManager.cull_idle_timeout={idle_kernel_timeout_hours * 3600}",
             "--MappingKernelManager.cull_interval=300",
             f"--MappingKernelManager.cull_connected={cull_connected}",
             "--MappingKernelManager.cull_busy=False"],
            cwd=root, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            matches = [server for server in _local_servers(root)
                       if server.get("pid") == process.pid]
            if matches:
                try:
                    return {**_ready_server(matches[0]), "reused": False,
                            "cull_policy_applied": True,
                            "idle_kernel_timeout_hours": idle_kernel_timeout_hours,
                            "cull_connected": cull_connected,
                            "cull_busy": False}
                except (RuntimeError, OSError):
                    pass
            if process.poll() is not None:
                break
            time.sleep(0.2)
        raise RuntimeError(f"JupyterLab did not start with the bridge; log: {log_path}")
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        raise


@mcp.tool()
def stop_jupyter(server_url: str) -> dict:
    """Stop a specified local Jupyter server only after its panels and sessions are closed."""
    if urllib.parse.urlsplit(server_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only a specified loopback Jupyter server can be stopped")
    matching = [server for server in list_running_servers()
                if server["url"].rstrip("/") == server_url.rstrip("/")]
    if len(matching) != 1:
        raise RuntimeError("The specified local Jupyter server is not uniquely running")
    base, token = _connection(server_url)
    if request(base, token, "nb-analysis/panels").get("panels"):
        raise RuntimeError("Live notebook panels remain; close them before stopping Jupyter")
    if request(base, token, "api/sessions"):
        raise RuntimeError("Jupyter sessions remain; shut down their kernels before stopping Jupyter")
    if request(base, token, "api/terminals"):
        raise RuntimeError("Jupyter terminals remain; stop them before stopping Jupyter")
    request(base, token, "api/shutdown", "POST", {})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not any(server["url"].rstrip("/") == server_url.rstrip("/")
                   for server in list_running_servers()):
            return {"server_url": base, "stopped": True}
        time.sleep(0.2)
    return {"server_url": base, "stopped": False, "status": "shutdown_requested"}


def _notebook_path(path: str) -> str:
    if (not isinstance(path, str) or not path.endswith(".ipynb") or
            not path or "\\" in path or "\x00" in path or
            PurePosixPath(path).is_absolute() or
            any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("path must be a relative .ipynb path within the Jupyter server root")
    return path


def _contents_metadata(base: str, token: str, path: str) -> dict | None:
    try:
        return request(base, token, "api/contents/" + urllib.parse.quote(path, safe="/") + "?content=0")
    except RuntimeError as error:
        if "HTTP 404:" in str(error):
            return None
        raise


def _browser_url(base: str, token: str, path: str) -> str:
    return (base.rstrip("/") + "/lab/tree/" + urllib.parse.quote(path, safe="/") +
            "?token=" + urllib.parse.quote(token, safe=""))


def _wait_open(base: str, token: str, request_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        result = request(base, token, "nb-analysis/open/result/" + request_id)
        if result["status"] in {"done", "error", "unknown"}:
            return result
        if time.monotonic() >= deadline:
            return {"request_id": request_id, "status": "unknown",
                    "error": "Open request is pending; inspect before retrying"}
        time.sleep(0.25)


def _choose_kernelspec(catalog: dict, requested: str | None) -> tuple[str, dict]:
    specs = catalog.get("kernelspecs") or {}
    if not isinstance(specs, dict) or not specs:
        raise RuntimeError("Jupyter reports no available kernelspecs")
    if requested is not None and (not isinstance(requested, str) or not requested):
        raise ValueError("kernel_name must be a nonempty kernelspec name")
    name = requested or os.getenv("NBIDE_DEFAULT_KERNEL_NAME") or catalog.get("default")
    if not name and len(specs) == 1:
        name = next(iter(specs))
    if not isinstance(name, str) or not name:
        raise RuntimeError("No default kernel; choose kernel_name from: " + ", ".join(sorted(specs)))
    entry = specs.get(name) or {}
    spec = entry.get("spec") if isinstance(entry, dict) else None
    if not isinstance(spec, dict) or not isinstance(spec.get("language"), str) or not spec["language"]:
        raise RuntimeError(f"Kernelspec {name!r} is unavailable or invalid; available: " +
                           ", ".join(sorted(specs)))
    return name, spec


@mcp.tool()
def open_notebook(path: str, server_url: str | None = None, create: bool = False,
                  browser_client_id: str | None = None, timeout: float = 30,
                  kernel_name: str | None = None) -> dict:
    """Open an existing notebook, or create one with a selected Jupyter kernelspec."""
    path = _notebook_path(path)
    if not 0 < timeout <= 120:
        raise ValueError("timeout must be between 0 and 120 seconds")
    if kernel_name is not None and not create:
        raise ValueError("kernel_name applies only when create=true; existing kernels are preserved")
    base, token = _connection(server_url)
    listing = request(base, token, "nb-analysis/panels")
    if listing.get("protocol_version") != 2:
        raise RuntimeError("The running Jupyter server does not support bridge protocol 2")
    request(base, token, "nb-analysis/clients")
    metadata = _contents_metadata(base, token, path)
    kernel_name = None
    if create:
        if metadata is not None:
            raise RuntimeError("Notebook already exists; create never overwrites")
        selected_kernel, spec = _choose_kernelspec(request(base, token, "api/kernelspecs"), kernel_name)
        content = {"cells": [], "metadata": {"kernelspec": {
            "name": selected_kernel,
            "display_name": spec.get("display_name") or selected_kernel,
            "language": spec["language"]}}, "nbformat": 4, "nbformat_minor": 5}
        parent = path.rpartition("/")[0]
        created = request(base, token, "api/contents/" + urllib.parse.quote(parent, safe="/"),
                          "POST", {"type": "notebook"})
        temporary_path = created["path"]
        try:
            request(base, token, "api/contents/" + urllib.parse.quote(temporary_path, safe="/"),
                    "PATCH", {"path": path})
        except Exception:
            try:
                request(base, token, "api/contents/" + urllib.parse.quote(temporary_path, safe="/"),
                        "DELETE")
            except Exception:
                pass
            raise
        request(base, token, "api/contents/" + urllib.parse.quote(path, safe="/"),
                "PUT", {"type": "notebook", "format": "json", "content": content})
        kernel_name = selected_kernel
    elif metadata is None or metadata.get("type") != "notebook":
        raise RuntimeError("Notebook does not exist at this Jupyter server root")
    listing = request(base, token, "nb-analysis/panels")
    matches = [panel for panel in listing["panels"] if panel["path"] == path]
    if len(matches) > 1:
        raise RuntimeError("Multiple live panels have this path; bind an explicit panel ID")
    if matches:
        return {"status": "already_open", "server_url": base, "path": path,
                "panel": matches[0], "created": create,
                **({"kernel_name": kernel_name} if create else {})}
    opened = request(base, token, "nb-analysis/open", "POST",
                     {"path": path, "client_id": browser_client_id,
                      "kernel_name": kernel_name})
    if opened["status"] == "needs_browser":
        return {"status": "needs_browser", "server_url": base, "path": path,
                "created": create, "browser_url": _browser_url(base, token, path),
                **({"kernel_name": kernel_name} if create else {})}
    result = _wait_open(base, token, opened["request_id"], timeout)
    response = {**result, "server_url": base, "created": create}
    if create:
        response["kernel_name"] = kernel_name
    if result["status"] == "unknown":
        response["browser_url"] = _browser_url(base, token, path)
    return response


@mcp.tool()
def list_notebooks(server_url: str | None = None) -> dict:
    """Discover browser-open notebook panels across local Jupyter servers; no kernel is started."""
    if server_url:
        candidates = [server_url]
    else:
        candidates = [server["url"] for server in list_running_servers()
                      if urllib.parse.urlsplit(server["url"]).hostname in {"127.0.0.1", "localhost", "::1"}]
    listings = []
    for url in dict.fromkeys(candidates):
        try:
            base, token = _connection(url)
            listing = request(base, token, "nb-analysis/panels")
            if listing.get("protocol_version") == 2:
                listings.append({"server_url": base, "panels": listing["panels"]})
        except (RuntimeError, OSError):
            if server_url:
                raise
    return {"servers": listings}


@mcp.tool()
def bind_notebook(panel_id: str, server_url: str | None = None) -> dict:
    """Bind explicitly to a live browser panel, session, kernel and restart epoch."""
    base, token = _connection(server_url)
    panel = _panel(base, token, panel_id)
    binding_id = str(uuid.uuid4())
    BINDINGS[binding_id] = (base, token, panel)
    return {"binding_id": binding_id, "server_url": base, "panel": panel}


@mcp.tool()
def list_cells(binding_id: str, start: int = 0, limit: int = 50,
               timeout: float = 30) -> dict:
    """Read stable IDs, sources and hashes from the live browser model, including unsaved edits."""
    return _submit(binding_id, "snapshot", {"start": start, "limit": limit}, None, timeout)


@mcp.tool()
def get_cell(binding_id: str, cell_id: str, timeout: float = 30) -> dict:
    """Read one cell by stable ID from the unsaved live model, with source hash."""
    return _submit(binding_id, "get_cell", {"cell_id": cell_id}, None, timeout)


@mcp.tool()
def insert_cell(binding_id: str, source: str, cell_type: str = "code",
                after_cell_id: str | None = None,
                idempotency_key: str | None = None, timeout: float = 30) -> dict:
    """Insert a code or Markdown cell visibly into the live browser notebook."""
    if cell_type not in {"code", "markdown"}:
        raise ValueError("cell_type must be code or markdown")
    fields = {"source": source}
    if after_cell_id:
        fields["after_cell_id"] = after_cell_id
    return _submit(binding_id, "insert_" + cell_type, fields, idempotency_key, timeout)


@mcp.tool()
def insert_code_cell(binding_id: str, source: str, after_cell_id: str | None = None,
                     idempotency_key: str | None = None, timeout: float = 30) -> dict:
    """Insert a visible code cell in the bound live browser notebook."""
    return insert_cell(binding_id, source, "code", after_cell_id, idempotency_key, timeout)


@mcp.tool()
def insert_markdown_cell(binding_id: str, source: str, after_cell_id: str | None = None,
                         idempotency_key: str | None = None, timeout: float = 30) -> dict:
    """Insert a visible Markdown cell in the bound live browser notebook."""
    return insert_cell(binding_id, source, "markdown", after_cell_id, idempotency_key, timeout)


@mcp.tool()
def execute_cell(binding_id: str, cell_id: str, expected_source_hash: str,
                 idempotency_key: str | None = None,
                 timeout: float = 60) -> dict:
    """Execute a browser-visible code cell through that panel's existing kernel."""
    return _submit(binding_id, "execute", {"cell_id": cell_id, "expected_source_hash": expected_source_hash},
                   idempotency_key, timeout)


@mcp.tool()
def shutdown_kernel(binding_id: str, idempotency_key: str | None = None,
                    timeout: float = 30) -> dict:
    """Shut down the bound idle kernel, refusing kernels shared by another Jupyter session."""
    base, token, panel = _bound(binding_id)
    if not panel.get("kernel_id"):
        raise RuntimeError("The bound notebook has no running kernel")
    sessions = request(base, token, "api/sessions")
    shared = [session for session in sessions
              if (session.get("kernel") or {}).get("id") == panel["kernel_id"]
              and session.get("id") != panel["session_id"]]
    if shared:
        raise RuntimeError("Kernel is shared by another Jupyter session; shutdown refused")
    return _submit(binding_id, "shutdown_kernel", {}, idempotency_key, timeout,
                   require_kernel=True)


@mcp.tool()
def close_notebook(binding_id: str, idempotency_key: str | None = None,
                   timeout: float = 30) -> dict:
    """Save unsaved live edits and close the bound JupyterLab notebook panel."""
    return _submit(binding_id, "close_notebook", {}, idempotency_key, timeout)


def _text_from_outputs(outputs: list, field: str, name: str | None = None) -> str:
    """Join already bounded bridge summaries without expanding their total text limit."""
    pieces = []
    for output in outputs:
        if name is not None and (output.get("output_type") != "stream" or output.get("name") != name):
            continue
        value = output.get(field)
        if isinstance(value, dict):
            value = value.get("text")
        if isinstance(value, str):
            pieces.append(value)
    return "".join(pieces)[:32_000]


@mcp.tool()
def push_execute_read(binding_id: str, source: str,
                      idempotency_key: str | None = None, timeout: float = 60) -> dict:
    """Insert a visible code cell, execute it in the bound kernel, and return its output."""
    if not 0 < timeout <= 300:
        raise ValueError("timeout must be between 0 and 300 seconds")
    t0_epoch_ms = time.time() * 1000
    started = time.monotonic()
    def finish(response: dict) -> dict:
        return {**response, "t0_mcp_received_epoch_ms": t0_epoch_ms,
                "t8_mcp_response_prepared_epoch_ms": time.time() * 1000}

    key = idempotency_key or str(uuid.uuid4())
    try:
        completed = _submit(binding_id, "push_execute_read", {"source": source}, key, timeout,
                            require_kernel=True)
    except Exception as error:
        return finish({"execution_status": "unknown",
                "operation_id": getattr(error, "request_id", None),
                "idempotency_key": key,
                "error": str(error),
                "timing_ms": {"total": round((time.monotonic() - started) * 1000, 3)}})
    execution = completed.get("result") or {}
    outputs = execution.get("outputs") or []
    elapsed_ms = (time.monotonic() - started) * 1000
    return finish({"operation_id": completed.get("request_id"),
            "idempotency_key": key, "cell_id": execution.get("cell_id"),
            "source_hash": execution.get("source_hash"),
            "bound_panel": completed.get("bound_panel"),
            "execution_status": execution.get("status", completed.get("status")),
            "execution_count": execution.get("execution_count"),
            "ok": execution.get("ok") is True,
            "error": execution.get("error"),
            "stdout": _text_from_outputs(outputs, "text", "stdout"),
            "stderr": _text_from_outputs(outputs, "text", "stderr"),
            "traceback": _text_from_outputs(outputs, "traceback"),
            "text_plain": _text_from_outputs(outputs, "text_plain"),
            "text_html": _text_from_outputs(outputs, "text_html"),
            "artifacts": [artifact for output in outputs for artifact in output.get("artifacts", [])],
            "outputs": outputs, "session_id": execution.get("session_id"),
            "kernel_id": execution.get("kernel_id"),
            "restart_epoch": execution.get("kernel_epoch"),
            "timing": {"broker": completed.get("broker_timing"),
                       "frontend": execution.get("timing"),
                       "mcp": completed.get("mcp_timing")},
            "timing_ms": {"total": round(elapsed_ms, 3)}})


@mcp.tool()
def update_cell(binding_id: str, cell_id: str, expected_source_hash: str, source: str,
                idempotency_key: str | None = None,
                timeout: float = 30) -> dict:
    """Replace a live cell's source only if its current source hash still matches."""
    return _submit(binding_id, "update_cell", {"cell_id": cell_id, "expected_source_hash": expected_source_hash,
                                             "source": source}, idempotency_key, timeout)


@mcp.tool()
def delete_cell(binding_id: str, cell_id: str, expected_source_hash: str,
                idempotency_key: str | None = None, timeout: float = 30) -> dict:
    """Delete a live cell by stable ID only if its source hash still matches."""
    return _submit(binding_id, "delete_cell", {"cell_id": cell_id, "expected_source_hash": expected_source_hash},
                   idempotency_key, timeout)


@mcp.tool()
def get_status(binding_id: str) -> dict:
    """Read the bound panel's current connection, session, kernel and kernel status."""
    base, _, panel = _bound(binding_id)
    return {"server_url": base, "panel": panel, "connected": True}


@mcp.tool()
def get_execution_result(request_id: str, server_url: str | None = None) -> dict:
    """Recover a submitted bridge request after timeout or Codex interruption."""
    base, token = _connection(server_url)
    return request(base, token, "nb-analysis/result/" + request_id)


@mcp.tool()
def inspect_output_artifact(artifact_id: str, server_url: str | None = None) -> dict:
    """Download a PNG or SVG output artifact to a bounded local inspection file."""
    base, token = _connection(server_url)
    return download_artifact(base, token, artifact_id)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
