# nb-mcp-jupyter-notebook

An MCP bridge for controlling an **open, live JupyterLab notebook** from Codex Desktop. Cells are inserted into JupyterLab's browser model and executed through that notebook panel's existing kernel. The bridge does not run a second analysis kernel or edit an open `.ipynb` behind JupyterLab's back.

## Install

Use a Python 3.11 environment with JupyterLab 4 and Jupyter Server 2. The repository includes a built JupyterLab extension, so Node.js is unnecessary for ordinary installation.

```bash
git clone https://github.com/nbatada/nb-mcp-jupyter-notebook.git
cd nb-mcp-jupyter-notebook
NBIDE_PYTHON=/absolute/path/to/your/python3.11 bash install.sh
codex mcp add nb-mcp-jupyter-notebook -- /absolute/path/to/your/nbide-mcp
```

Use the `nbide-mcp` executable installed in the **same** Python environment. If the MCP is already registered, keep its existing registration. Restart Codex Desktop to refresh its MCP tool inventory, and restart an existing JupyterLab server when it is safe to load the new server and frontend extensions. Installation does not restart a running server or kernel.

For new notebooks, `open_notebook(create=true)` defaults to the `python3.11` kernelspec and verifies that its executable matches the MCP's Python environment. Set `NBIDE_DEFAULT_KERNEL_NAME` and `NBIDE_DEFAULT_KERNEL_PYTHON` in the MCP environment if your kernelspec has another name or executable. Opening an existing notebook leaves its kernel unchanged.

## Use

1. `start_jupyter` reuses or starts a loopback JupyterLab server for an explicit project root.
2. `open_notebook` opens an existing notebook in a connected JupyterLab client, or creates a new one when `create=true`. If no client is connected, open its returned URL in Codex's embedded browser, then discover the panel.
3. `list_notebooks` and `bind_notebook` select the exact live panel. For an authorized analysis block, `push_execute_read` inserts one visible cell, executes it in that panel's kernel, and returns bounded output and plot artifact references.
4. On explicit cleanup, `shutdown_kernel` refuses busy or shared kernels, `close_notebook` saves unsaved edits before closing the panel, and `stop_jupyter` refuses a server with open panels, sessions, or terminals.

Keep the token-bearing browser URL local; it grants access to that Jupyter server. If an execution or open request has an uncertain outcome, inspect its request ID and the live panel before retrying.

## Build the frontend from source

```bash
cd jupyterlab_nb_analysis_bridge
npm ci
npm run build
```

The prebuilt assets in `jupyter-data/` are generated from `jupyterlab_nb_analysis_bridge/src/`. Focused checks run with `python -m unittest discover -s tests -q`.
