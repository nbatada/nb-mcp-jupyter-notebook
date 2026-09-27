"""Small Codex-facing CLI for the live JupyterLab bridge."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

from jupyter_server.serverapp import list_running_servers


def request(base, token, path, method="GET", data=None):
    body = None if data is None else json.dumps(data).encode()
    url = base.rstrip("/") + "/" + path.lstrip("/")
    call = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "token " + token,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(call, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Jupyter bridge HTTP {error.code}: {detail}") from error


def wait_for_result(base, token, request_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        response = request(base, token, "nb-analysis/result/" + request_id)
        if response["status"] in {"done", "unknown"}:
            return response
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Request {request_id} is still {response['status']}; inspect it before retrying")
        time.sleep(0.25)


def download_artifact(base, token, artifact_id):
    url = base.rstrip("/") + "/nb-analysis/artifact/" + artifact_id
    call = urllib.request.Request(url, headers={"Authorization": "token " + token})
    with urllib.request.urlopen(call, timeout=30) as response:
        mime = response.headers.get("Content-Type", "application/octet-stream").split(";")[0]
        data = response.read(8_000_001)
    if len(data) > 8_000_000:
        raise RuntimeError("artifact exceeds the 8 MB inspection limit")
    suffix = ".png" if mime == "image/png" else ".svg" if mime == "image/svg+xml" else ".bin"
    with tempfile.NamedTemporaryFile(prefix="nbide-artifact-", suffix=suffix, delete=False) as output:
        output.write(data)
        path = output.name
    return {"path": path, "mime": mime, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def resolve_server(url, token):
    if url and token:
        return url, token
    local = [server for server in list_running_servers() if urllib.parse.urlsplit(server["url"]).hostname in {"127.0.0.1", "localhost", "::1"}]
    if url:
        local = [server for server in local if server["url"].rstrip("/") == url.rstrip("/")]
    if not url and len(local) > 1:
        current = os.path.realpath(os.getcwd())
        matching = [server for server in local if os.path.realpath(server.get("root_dir", "")) == current]
        if len(matching) == 1:
            local = matching
    if len(local) != 1:
        choices = ", ".join(f"{server['url']} (root {server.get('root_dir')})" for server in local)
        raise RuntimeError(f"Could not choose one local Jupyter server. {choices or 'None found.'} Set NBIDE_JUPYTER_URL and JUPYTER_TOKEN if needed")
    server = local[0]
    resolved_token = token or server.get("token")
    if not resolved_token:
        raise RuntimeError("The selected Jupyter server has no token available in its runtime record")
    return url or server["url"], resolved_token


def main():
    parser = argparse.ArgumentParser(prog="nbide")
    parser.add_argument("--url", default=os.getenv("NBIDE_JUPYTER_URL"))
    parser.add_argument("--token", default=os.getenv("JUPYTER_TOKEN"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("panels")
    send = sub.add_parser("request")
    send.add_argument("panel")
    send.add_argument("operation", choices=["insert_code", "insert_markdown", "snapshot", "update_cell", "execute"])
    send.add_argument("--json", default="{}")
    send.add_argument("--idempotency-key")
    send.add_argument("--wait", action="store_true")
    send.add_argument("--timeout", type=float, default=60)
    result = sub.add_parser("result")
    result.add_argument("id")
    result.add_argument("--wait", action="store_true")
    result.add_argument("--timeout", type=float, default=60)
    artifact = sub.add_parser("artifact")
    artifact.add_argument("id")
    args = parser.parse_args()
    if hasattr(args, "timeout") and args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        args.url, args.token = resolve_server(args.url, args.token)
        if args.cmd == "panels":
            response = request(args.url, args.token, "nb-analysis/panels")
        elif args.cmd == "request":
            payload = json.loads(args.json)
            if not isinstance(payload, dict):
                parser.error("--json must contain an object")
            listing = request(args.url, args.token, "nb-analysis/panels")
            if listing.get("protocol_version") != 2:
                raise RuntimeError("The running Jupyter server has an older bridge; restart that server before sending requests")
            panels = listing["panels"]
            matches = [panel for panel in panels if panel["panel_id"] == args.panel]
            if len(matches) != 1:
                raise RuntimeError("panel is not connected; run 'nbide panels' and bind explicitly")
            panel = matches[0]
            payload.update(panel_id=args.panel, operation=args.operation)
            payload.update(expected_path=panel["path"], expected_session_id=panel["session_id"], expected_kernel_id=panel["kernel_id"], expected_kernel_epoch=panel["kernel_epoch"])
            key = args.idempotency_key or str(uuid.uuid4())
            payload["idempotency_key"] = key
            print(f"nbide idempotency_key={key}", file=sys.stderr, flush=True)
            response = request(args.url, args.token, "nb-analysis/requests", "POST", payload)
            if args.wait:
                print(f"nbide request_id={response['request_id']}", file=sys.stderr, flush=True)
                response = wait_for_result(args.url, args.token, response["request_id"], args.timeout)
        elif args.cmd == "artifact":
            response = download_artifact(args.url, args.token, args.id)
        elif args.wait:
            response = wait_for_result(args.url, args.token, args.id, args.timeout)
        else:
            response = request(args.url, args.token, "nb-analysis/result/" + args.id)
    except (RuntimeError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        parser.exit(1, f"nbide: {error}\n")
    print(json.dumps(response, indent=2))
    if response.get("status") == "unknown" or (response.get("result") and response["result"].get("ok") is False):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
