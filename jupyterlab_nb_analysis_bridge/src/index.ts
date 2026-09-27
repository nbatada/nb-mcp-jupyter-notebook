import { JupyterFrontEnd, JupyterFrontEndPlugin } from '@jupyterlab/application';
import { CodeCell } from '@jupyterlab/cells';
import { INotebookTracker, NotebookPanel } from '@jupyterlab/notebook';
import { ServerConnection } from '@jupyterlab/services';
import { PageConfig } from '@jupyterlab/coreutils';

const panelIds = new WeakMap<NotebookPanel, string>();
const kernelEpochs = new WeakMap<NotebookPanel, number>();
const observedPanels = new WeakSet<NotebookPanel>();
const browserClientKey = 'nb-analysis-bridge-browser-client-id';
let browserClientId: string = crypto.randomUUID();
try {
  const stored = sessionStorage.getItem(browserClientKey);
  if (stored) {
    browserClientId = stored;
  } else {
    sessionStorage.setItem(browserClientKey, browserClientId);
  }
} catch {
  // The bridge remains usable when browser storage is disabled; the panel TTL
  // then handles stale registrations after a page reload.
}

function observePanel(panel: NotebookPanel): void {
  if (observedPanels.has(panel)) {
    return;
  }
  observedPanels.add(panel);
  kernelEpochs.set(panel, 0);
  const bump = () => kernelEpochs.set(panel, (kernelEpochs.get(panel) ?? 0) + 1);
  panel.sessionContext.kernelChanged.connect(bump);
  panel.sessionContext.connectionStatusChanged.connect((_context, status) => {
    if (status !== 'connected') {
      bump();
    }
  });
  panel.sessionContext.statusChanged.connect((_context, status) => {
    if (status === 'restarting' || status === 'dead') {
      bump();
    }
  });
}

async function sourceHash(source: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(source));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function panelId(panel: NotebookPanel): string {
  let id = panelIds.get(panel);
  if (!id) {
    id = crypto.randomUUID();
    panelIds.set(panel, id);
  }
  return id;
}

function timingNow(): number {
  return performance.timeOrigin + performance.now();
}

async function api(path: string, body?: object): Promise<any> {
  const base = PageConfig.getBaseUrl().replace(/\/$/, '');
  const response = await ServerConnection.makeRequest(
    `${base}/nb-analysis/${path}`,
    body ? { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } } : {},
    ServerConnection.makeSettings()
  );
  if (!response.ok) {
    throw new Error(`Bridge ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

function openPanels(tracker: INotebookTracker): NotebookPanel[] {
  const panels: NotebookPanel[] = [];
  tracker.forEach(panel => {
    if (!panel.isDisposed) {
      panels.push(panel);
    }
  });
  return panels;
}

function identity(panel: NotebookPanel) {
  observePanel(panel);
  return {
    path: panel.context.path,
    session_id: panel.sessionContext.session?.id ?? null,
    kernel_id: panel.sessionContext.session?.kernel?.id ?? null,
    kernel_epoch: kernelEpochs.get(panel) ?? 0
  };
}

function verifyBinding(panel: NotebookPanel, request: any): void {
  if (panel.isDisposed) {
    throw new Error('BOUND_PANEL_CLOSED');
  }
  const current = identity(panel);
  if (
    request.expected_path !== current.path ||
    request.expected_session_id !== current.session_id ||
    request.expected_kernel_id !== current.kernel_id ||
    request.expected_kernel_epoch !== current.kernel_epoch
  ) {
    throw new Error('BOUND_NOTEBOOK_OR_KERNEL_CHANGED');
  }
}

async function processRequest(panel: NotebookPanel, request: any, receivedAt: number): Promise<void> {
  const bound = identity(panel);
  let result: any = { panel_id: panelId(panel), ok: false, ...bound };
  const timing: Record<string, number | null> = {
    t2_frontend_received_epoch_ms: receivedAt,
    t3_cell_inserted_epoch_ms: null,
    t4_execution_requested_epoch_ms: null,
    t5_kernel_busy_observed_epoch_ms: null,
    t6_execution_resolved_epoch_ms: null,
    t7_output_collected_epoch_ms: null
  };
  try {
    verifyBinding(panel, request);
    const notebook = panel.content;
    const model = notebook.model;
    if (!model) {
      throw new Error('NOTEBOOK_MODEL_UNAVAILABLE');
    }
    async function executeCell(cellId: string, expectedSourceHash: string): Promise<void> {
      if (!bound.kernel_id || !bound.session_id) {
        throw new Error('NO_EXISTING_KERNEL');
      }
      const index = model!.sharedModel.cells.findIndex(cell => cell.id === cellId);
      if (index < 0) {
        throw new Error('CELL_NOT_FOUND');
      }
      await notebook.scrollToItem(index);
      const currentIndex = model!.sharedModel.cells.findIndex(item => item.id === cellId);
      if (currentIndex < 0) {
        throw new Error('CELL_NOT_FOUND');
      }
      const cell = notebook.widgets[currentIndex];
      if (!(cell instanceof CodeCell) || cell.model.sharedModel.id !== cellId) {
        throw new Error('TARGET_IS_NOT_A_CODE_CELL');
      }
      verifyBinding(panel, request);
      const sourceAtCheck = cell.model.sharedModel.getSource();
      if (await sourceHash(sourceAtCheck) !== expectedSourceHash ||
          cell.model.sharedModel.getSource() !== sourceAtCheck) {
        throw new Error('SOURCE_CONFLICT');
      }
      if (panel.sessionContext.session?.kernel?.status === 'busy') {
        throw new Error('KERNEL_BUSY');
      }
      if (cell.isDisposed || !model!.sharedModel.cells.some(item => item.id === cellId)) {
        throw new Error('CELL_NOT_FOUND');
      }
      const onKernelStatus = (_context: typeof panel.sessionContext, status: string) => {
        if (status === 'busy' && timing.t5_kernel_busy_observed_epoch_ms === null) {
          timing.t5_kernel_busy_observed_epoch_ms = timingNow();
        }
      };
      panel.sessionContext.statusChanged.connect(onKernelStatus);
      let reply;
      try {
        timing.t4_execution_requested_epoch_ms = timingNow();
        reply = await CodeCell.execute(cell, panel.sessionContext);
        timing.t6_execution_resolved_epoch_ms = timingNow();
      } finally {
        panel.sessionContext.statusChanged.disconnect(onKernelStatus);
      }
      const outputs: any[] = [];
      const outputModel = cell.model.outputs;
      let remainingCharacters = 10_000_000;
      for (let i = 0; i < outputModel.length; i++) {
        if (i >= 100) {
          outputs.push({ output_type: 'omitted', characters: 0 });
          break;
        }
        const output = outputModel.get(i).toJSON();
        const characters = JSON.stringify(output).length;
        if (characters > remainingCharacters) {
          outputs.push({ output_type: 'omitted', characters });
          break;
        }
        outputs.push(output);
        remainingCharacters -= characters;
      }
      const unchanged = identity(panel);
      if (unchanged.session_id !== bound.session_id || unchanged.kernel_id !== bound.kernel_id || unchanged.kernel_epoch !== bound.kernel_epoch) {
        throw new Error('KERNEL_CHANGED_DURING_EXECUTION');
      }
      const sourceChanged = await sourceHash(cell.model.sharedModel.getSource()) !== expectedSourceHash;
      timing.t7_output_collected_epoch_ms = timingNow();
      result = {
        ...result,
        ok: reply?.content.status === 'ok' && !sourceChanged,
        status: reply?.content.status ?? 'unknown',
        execution_count: reply?.content.execution_count ?? null,
        cell_id: cellId,
        error: sourceChanged ? 'SOURCE_CHANGED_DURING_EXECUTION' : undefined,
        outputs
      };
    }
    if (request.operation === 'insert_code' || request.operation === 'insert_markdown' || request.operation === 'push_execute_read') {
      if (request.operation === 'push_execute_read') {
        if (!bound.kernel_id || !bound.session_id) {
          throw new Error('NO_EXISTING_KERNEL');
        }
        if (panel.sessionContext.session?.kernel?.status === 'busy') {
          throw new Error('KERNEL_BUSY');
        }
      }
      const cells = model.sharedModel.cells;
      let index = cells.length;
      if (request.after_cell_id) {
        const anchor = cells.findIndex(cell => cell.id === request.after_cell_id);
        if (anchor < 0) {
          throw new Error('ANCHOR_CELL_NOT_FOUND');
        }
        index = anchor + 1;
      }
      const cellId = crypto.randomUUID();
      const sourceDigest = await sourceHash(request.source);
      model.sharedModel.insertCell(index, {
        id: cellId,
        cell_type: request.operation === 'insert_markdown' ? 'markdown' : 'code',
        source: request.source,
        metadata: {}
      } as any);
      result = { ...result, cell_id: cellId, source_hash: sourceDigest };
      const insertedIndex = model.sharedModel.cells.findIndex(cell => cell.id === cellId);
      if (insertedIndex < 0) {
        throw new Error('INSERT_NOT_VISIBLE_IN_MODEL');
      }
      timing.t3_cell_inserted_epoch_ms = timingNow();
      await notebook.scrollToItem(insertedIndex);
      result = { ...result, ok: true, visible: true };
      if (request.operation === 'push_execute_read') {
        await executeCell(cellId, sourceDigest);
      }
    } else if (request.operation === 'snapshot') {
      const all = model.sharedModel.cells;
      const start = Number.isInteger(request.start) && request.start >= 0 ? request.start : 0;
      const limit = Number.isInteger(request.limit) && request.limit > 0 ? Math.min(request.limit, 100) : 50;
      const cells = await Promise.all(all.slice(start, start + limit).map(async cell => {
        const source = cell.getSource();
        return {
          cell_id: cell.id,
          cell_type: cell.cell_type,
          source_hash: await sourceHash(source),
          source: source.slice(0, 2_000),
          truncated: source.length > 2_000
        };
      }));
      result = { ...result, ok: true, cells, start, total_cells: all.length };
    } else if (request.operation === 'get_cell') {
      const cell = model.sharedModel.cells.find(cell => cell.id === request.cell_id);
      if (!cell) {
        throw new Error('CELL_NOT_FOUND');
      }
      const source = cell.getSource();
      result = { ...result, ok: true, cell_id: cell.id, cell_type: cell.cell_type,
        source: source.slice(0, 100_000), truncated: source.length > 100_000,
        source_hash: await sourceHash(source) };
    } else if (request.operation === 'update_cell') {
      const cell = model.sharedModel.cells.find(cell => cell.id === request.cell_id);
      if (!cell) {
        throw new Error('CELL_NOT_FOUND');
      }
      if (await sourceHash(cell.getSource()) !== request.expected_source_hash) {
        throw new Error('SOURCE_CONFLICT');
      }
      cell.setSource(request.source);
      result = { ...result, ok: true, cell_id: cell.id, source_hash: await sourceHash(request.source) };
    } else if (request.operation === 'delete_cell') {
      const index = model.sharedModel.cells.findIndex(cell => cell.id === request.cell_id);
      if (index < 0) {
        throw new Error('CELL_NOT_FOUND');
      }
      const cell = model.sharedModel.cells[index];
      if (await sourceHash(cell.getSource()) !== request.expected_source_hash) {
        throw new Error('SOURCE_CONFLICT');
      }
      model.sharedModel.deleteCell(index);
      result = { ...result, ok: true, cell_id: request.cell_id, deleted: true };
    } else if (request.operation === 'execute') {
      await executeCell(request.cell_id, request.expected_source_hash);
    } else if (request.operation === 'shutdown_kernel') {
      if (!bound.kernel_id || !bound.session_id) {
        throw new Error('NO_EXISTING_KERNEL');
      }
      if (panel.sessionContext.session?.kernel?.status === 'busy') {
        throw new Error('KERNEL_BUSY');
      }
      await panel.sessionContext.shutdown();
      result = { ...result, ok: true, status: 'shutdown',
        shutdown_kernel_id: bound.kernel_id, ...identity(panel) };
    } else if (request.operation === 'close_notebook') {
      if (panel.context.model?.dirty) {
        await panel.context.save();
      }
      verifyBinding(panel, request);
      panel.close();
      for (let attempt = 0; attempt < 60 && !panel.isDisposed; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      if (!panel.isDisposed) {
        throw new Error('PANEL_CLOSE_PENDING');
      }
      await api('panels/retire', {
        panel_id: panelId(panel), browser_client_id: browserClientId
      });
      result = { ...result, ok: true, status: 'closed', closed: true };
    } else {
      throw new Error('UNSUPPORTED_OPERATION');
    }
  } catch (error) {
    result = { ...result, ok: false,
      status: timing.t4_execution_requested_epoch_ms === null ? 'not_started' : 'unknown',
      error: String(error) };
  }
  await api(`result/${request.request_id}`, { ...result, request_id: request.request_id, timing });
}

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'nb-analysis-bridge',
  autoStart: true,
  requires: [INotebookTracker],
  activate: (app: JupyterFrontEnd, tracker: INotebookTracker) => {
    const working = new Set<string>();
    let registering = false;
    let opening = false;

    async function registerClient(): Promise<void> {
      try {
        await api('clients', { client_id: browserClientId });
      } catch (error) {
        console.warn('NB Analysis Bridge client registration:', error);
      }
    }

    async function registerPanels(): Promise<void> {
      if (registering) {
        return;
      }
      registering = true;
      try {
        await Promise.all(openPanels(tracker).map(panel => api('panels', {
          panel_id: panelId(panel),
          browser_client_id: browserClientId,
          ...identity(panel),
          kernel_status: panel.sessionContext.session?.kernel?.status ?? 'unknown',
          active: tracker.currentWidget === panel
        })));
      } catch (error) {
        console.warn('NB Analysis Bridge registration:', error);
      } finally {
        registering = false;
      }
    }

    async function pollPanel(panel: NotebookPanel): Promise<void> {
      const id = panelId(panel);
      if (working.has(id)) {
        return;
      }
      working.add(id);
      try {
        const batch = await api('requests/claim', { panel_id: id });
        for (const request of batch.requests ?? []) {
          await processRequest(panel, request, timingNow());
        }
      } catch (error) {
        console.warn('NB Analysis Bridge request:', error);
      } finally {
        working.delete(id);
      }
    }

    async function pollOpenRequests(): Promise<void> {
      if (opening) {
        return;
      }
      opening = true;
      try {
        const batch = await api('open/claim', { client_id: browserClientId });
        for (const request of batch.requests ?? []) {
          let result: any = { client_id: browserClientId, path: request.path, ok: false };
          try {
            const opened = await app.commands.execute('docmanager:open', {
              path: request.path, factory: 'Notebook'
            });
            const panel = opened instanceof NotebookPanel ? opened :
              openPanels(tracker).find(item => item.context.path === request.path);
            if (!panel || panel.isDisposed || panel.context.path !== request.path) {
              throw new Error('NOTEBOOK_PANEL_NOT_OPENED');
            }
            await panel.context.ready;
            if (request.kernel_name) {
              await panel.sessionContext.ready;
              if (panel.sessionContext.session?.kernel?.name !== request.kernel_name) {
                await panel.sessionContext.changeKernel({ name: request.kernel_name });
              }
              if (panel.sessionContext.session?.kernel?.name !== request.kernel_name) {
                throw new Error('REQUESTED_KERNEL_NOT_SELECTED');
              }
            }
            await api('panels', {
              panel_id: panelId(panel), browser_client_id: browserClientId,
              ...identity(panel),
              kernel_status: panel.sessionContext.session?.kernel?.status ?? 'unknown',
              active: tracker.currentWidget === panel
            });
            result = { ...result, ok: true, panel_id: panelId(panel), ...identity(panel) };
          } catch (error) {
            result.error = String(error);
          }
          await api(`open/result/${request.request_id}`, result);
        }
      } catch (error) {
        console.warn('NB Analysis Bridge open request:', error);
      } finally {
        opening = false;
      }
    }

    tracker.currentChanged.connect(() => { void registerPanels(); });
    tracker.widgetAdded.connect(() => { void registerPanels(); });
    void registerClient();
    void registerPanels();
    setInterval(() => { void registerClient(); }, 1000);
    setInterval(() => { void registerPanels(); }, 1000);
    setInterval(() => { openPanels(tracker).forEach(panel => { void pollPanel(panel); }); }, 300);
    setInterval(() => { void pollOpenRequests(); }, 300);
  }
};

export default plugin;
