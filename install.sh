#!/bin/bash
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$SRC"
cd "$DEST"
PY_INPUT="${NBIDE_PYTHON:-python3}"
PY="$(command -v "$PY_INPUT" || true)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "ERROR: Python executable not found: $PY_INPUT" >&2
  exit 1
fi
FRONTEND="$DEST/jupyter-data/labextensions/jupyterlab-nb-analysis-bridge"
if [ ! -f "$FRONTEND/package.json" ]; then
  echo "ERROR: Built JupyterLab extension missing: $FRONTEND/package.json" >&2
  exit 1
fi
PY_DIR="${PY%/*}"
export PATH="$PY_DIR:$PATH"
echo "Using Python: $PY"
"$PY" -c 'import jupyterlab,jupyter_server; assert jupyterlab.__version__.split(".")[0] == "4" and jupyter_server.__version__.split(".")[0] == "2"'
PY_ARCH="$("$PY" -c 'import platform; print(platform.machine())')"
HOST_ARM64=false
if [ "$(uname -s)" = "Darwin" ] && [ "$PY_ARCH" = "x86_64" ]; then
  if [ "$(uname -m)" = "arm64" ] || [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = "1" ]; then
    HOST_ARM64=true
  fi
fi
if [ "$HOST_ARM64" = true ]; then
  echo "Detected x86_64 Python on Apple Silicon; requiring a binary cryptography package to avoid a cross-architecture Rust/OpenSSL build."
  if ! "$PY" -m pip install --only-binary=cryptography .; then
    echo "ERROR: Installation failed in an x86_64 Python environment on Apple Silicon." >&2
    echo "If pip reported no compatible cryptography wheel, activate this Conda environment and run: conda install -c conda-forge cryptography" >&2
    echo "Then retry, or use a native arm64 Python environment. Review the pip output above for other errors." >&2
    exit 1
  fi
else
  "$PY" -m pip install .
fi
NBIDE_FRONTEND="$FRONTEND" NBIDE_SERVER_CONFIG="$DEST/jupyter-config/jupyter_server_config.d/nb_analysis_bridge.json" "$PY" - <<'PY'
import hashlib
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

source = Path(os.environ["NBIDE_FRONTEND"])
server_config_source = Path(os.environ["NBIDE_SERVER_CONFIG"])
parent = Path(sys.prefix) / "share/jupyter/labextensions"
target = parent / source.name
parent.mkdir(parents=True, exist_ok=True)
if target.is_symlink():
    raise SystemExit(f"Refusing to replace symlinked extension: {target}")
def digest(root):
    value = hashlib.sha256()
    for item in sorted(path for path in root.rglob("*") if path.is_file()):
        value.update(str(item.relative_to(root)).encode())
        value.update(item.read_bytes())
    return value.hexdigest()
if target.exists() and digest(source) == digest(target):
    print(f"JupyterLab extension already current: {target}")
else:
    stage = parent / f".{source.name}.install-{uuid.uuid4().hex[:8]}"
    shutil.copytree(source, stage)
    backup = None
    if target.exists():
        backups = Path(sys.prefix) / "share/jupyter/nb-analysis-bridge-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / f"{source.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        target.rename(backup)
    try:
        stage.rename(target)
    except Exception:
        if backup is not None:
            backup.rename(target)
        raise
    print(f"Installed JupyterLab extension: {target}")
    if backup is not None:
        print(f"Previous extension preserved: {backup}")

config_target = Path(sys.prefix) / "etc/jupyter/jupyter_server_config.d" / server_config_source.name
config_target.parent.mkdir(parents=True, exist_ok=True)
if config_target.exists() and config_target.read_bytes() != server_config_source.read_bytes():
    backups = Path(sys.prefix) / "share/jupyter/nb-analysis-bridge-backups"
    backups.mkdir(parents=True, exist_ok=True)
    config_backup = backups / f"{server_config_source.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    config_target.rename(config_backup)
    print(f"Previous server config preserved: {config_backup}")
if not config_target.exists():
    shutil.copy2(server_config_source, config_target)
print(f"Enabled Jupyter Server extension: {config_target}")
PY
"$PY" - <<'PY'
from jupyter_server.extension.manager import ExtensionPackage
package = ExtensionPackage(name="nb_analysis_bridge", enabled=True)
if not package.validate():
    raise SystemExit("ERROR: Jupyter Server extension validation failed")
print("Jupyter Server extension validated")
PY

echo
echo "NB Analysis Bridge installed in the selected Python environment."
echo "Existing Jupyter servers are not restarted. Restart JupyterLab when ready to load the bridge."
