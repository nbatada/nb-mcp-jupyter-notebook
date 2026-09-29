# nb-mcp-jupyter-notebook

An MCP bridge for controlling an **open, live JupyterLab notebook** from Codex Desktop. Cells are inserted into JupyterLab's browser model and executed through that notebook panel's existing kernel. The bridge does not run a second analysis kernel or edit an open `.ipynb` behind JupyterLab's back.

## Install

Use your chosen Python environment (Python 3.10 or newer) with JupyterLab 4 and Jupyter Server 2. The repository includes a built JupyterLab extension, so Node.js is unnecessary for ordinary installation.

```bash
git clone https://github.com/nbatada/nb-mcp-jupyter-notebook.git
cd nb-mcp-jupyter-notebook
NBIDE_PYTHON=/absolute/path/to/your/python bash install.sh
codex mcp add nb-mcp-jupyter-notebook -- /absolute/path/to/your/nbide-mcp
```

Use the `nbide-mcp` executable installed in the **same environment used for installation**. The notebook kernel may be in a different environment. If the MCP is already registered, keep its existing registration. Restart Codex Desktop to refresh its MCP tool inventory, and restart an existing JupyterLab server when it is safe to load the new server and frontend extensions. Installation does not restart a running server or kernel.

For a new notebook, `open_notebook(create=true)` uses Jupyter Server's default kernelspec. Pass `kernel_name` for a particular notebook, or set the optional `NBIDE_DEFAULT_KERNEL_NAME` in the MCP environment to choose a different default. The selected name must be available on that Jupyter server. Opening an existing notebook leaves its kernel unchanged.

## Use

1. `start_jupyter` reuses or starts a loopback JupyterLab server for an explicit project root.
2. `open_notebook` opens an existing notebook in a connected JupyterLab client, or creates a new one when `create=true`. If no client is connected, open its returned URL in Codex's embedded browser, then discover the panel.
3. `list_notebooks` and `bind_notebook` select the exact live panel. For an authorized analysis block, `push_execute_read` inserts one visible cell, executes it in that panel's kernel, and returns bounded output and plot artifact references.
4. On explicit cleanup, `shutdown_kernel` refuses busy or shared kernels, `close_notebook` saves unsaved edits before closing the panel, and `stop_jupyter` refuses a server with open panels, sessions, or terminals.

Keep the token-bearing browser URL local; it grants access to that Jupyter server. If an execution or open request has an uncertain outcome, inspect its request ID and the live panel before retrying.

## Idle-kernel memory safeguard

When `start_jupyter` launches a **new** server, it defaults to culling kernels after 24 hours of idle time, checked every five minutes. Only disconnected kernels are eligible by default; busy kernels are never culled. Pass `idle_kernel_timeout_hours=0` to disable culling or an integer from 1 to 168 to change the timeout. `cull_connected=true` also makes connected idle kernels eligible and should be an explicit user choice because it discards their in-memory state. The returned `cull_policy_applied` field is false when an existing server was reused; its settings are not changed.

Jupyter measures idle time since kernel activity, not time since a notebook tab closed. A kernel already idle for over 24 hours may be culled soon after the tab disconnects. A server started outside this MCP keeps its own Jupyter culling configuration.

## Build the frontend from source

```bash
cd jupyterlab_nb_analysis_bridge
npm ci
npm run build
```

The prebuilt assets in `jupyter-data/` are generated from `jupyterlab_nb_analysis_bridge/src/`. Focused checks run with `python -m unittest discover -s tests -q`.

## x86_64 Conda Python on Apple Silicon

If the selected Python is x86_64 while the Mac is Apple Silicon, `install.sh` requires a binary `cryptography` distribution rather than attempting a cross-architecture Rust/OpenSSL source build. If pip cannot resolve a compatible wheel, activate that same x86_64 Conda environment, run `conda install -c conda-forge cryptography`, and retry the installer. A native arm64 environment is another option when the project does not require x86_64. If a source build is necessary, use matching x86_64 Rust and OpenSSL build dependencies following [cryptography's installation guidance](https://cryptography.io/en/latest/installation/). The installer reports the binary-only route before copying Jupyter extensions; see [pip's binary-only option](https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-only-binary).
