'use strict';
// A panel page's only door to the main process: it fetches its spec, says how tall it came out, and asks to be
// closed or dragged. No file system or Node access.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('panelHost', {
  hello: () => ipcRenderer.invoke('panel:hello'),
  onSpec: (cb) => ipcRenderer.on('panel:spec', (_e, payload) => cb(payload)),
  onGo: (cb) => ipcRenderer.on('panel:go', () => cb()),
  onFold: (cb) => ipcRenderer.on('panel:fold', () => cb()),
  // `refit`: a height after the window changed size, not the report of a newly laid out spec.
  size: (height, notes, refit) =>
    ipcRenderer.send('panel:size', Number(height), Array.isArray(notes) ? notes.map(String) : [], refit === true),
  close: (by) => ipcRenderer.send('panel:close', by === 'timer' ? 'timer' : 'user'),
  folded: () => ipcRenderer.send('panel:folded'),
  pin: (on) => ipcRenderer.send('panel:pin', on === true),
  onPinned: (cb) => ipcRenderer.on('panel:pinned', (_e, state) => cb(state && typeof state === 'object' ? state : {})),
  onTicks: (cb) => ipcRenderer.on('panel:ticks', (_e, on) => cb(on === true)),
  tick: () => ipcRenderer.send('panel:tick'),
  typing: (on, restore) => ipcRenderer.send('panel:typing', on === true, restore === true),
  sent: (values, field, untouched, choice) =>
    ipcRenderer.send(
      'panel:sent',
      values && typeof values === 'object' ? { ...values } : {},
      String(field || ''),
      Array.isArray(untouched) ? untouched.map(String) : [],
      choice === true,
    ),
  ink: (strokes) => ipcRenderer.send('panel:ink', Number(strokes)),
  layout: (boxes) => ipcRenderer.send('panel:layout', boxes && typeof boxes === 'object' ? boxes : {}),
  // An explainer's player: a press of its buttons, and where its pointer points (the page's own pixels, or null).
  control: (action, step) => ipcRenderer.send('panel:control', String(action || ''), Number.isFinite(step) ? step : null),
  pointer: (rect) =>
    ipcRenderer.send(
      'panel:pointer',
      rect ? { x: Number(rect.x), y: Number(rect.y), width: Number(rect.width), height: Number(rect.height) } : null,
    ),
  mouse: (over) => ipcRenderer.send('panel:mouse', over === true),
  resize: (phase, edge) => ipcRenderer.send('panel:resize', phase === 'start' ? 'start' : 'end', String(edge || '')),
  drag: (phase) => ipcRenderer.send('panel:drag', phase === 'start' ? 'start' : 'end'),
  // A web page on the panel: where on the panel it goes (null: out of sight), how it's loading, the pointer over it.
  web: (rect) =>
    ipcRenderer.send(
      'panel:web',
      rect ? { x: Number(rect.x), y: Number(rect.y), width: Number(rect.width), height: Number(rect.height) } : null,
    ),
  onPage: (cb) => ipcRenderer.on('panel:page', (_e, state) => cb(state && typeof state === 'object' ? state : {})),
  onHover: (cb) => ipcRenderer.on('panel:hover', (_e, over) => cb(over === true)),
  // A live block's program: it ran, failed (and why), reported something, or the user moved one of its controls.
  live: (address, event) => {
    const e = event && typeof event === 'object' ? event : {};
    const values = e.values && typeof e.values === 'object' ? { ...e.values } : null;
    ipcRenderer.send('panel:live', String(address || ''), {
      type: String(e.type || ''),
      message: e.message == null ? null : String(e.message),
      line: Number.isInteger(e.line) ? e.line : null,
      state: e.state == null ? null : String(e.state),
      values,
      hung: e.hung === true,
      frame: e.frame == null ? null : String(e.frame).slice(0, 48),
    });
  },
  // The user clicked ↗: the panel's page ("page") or a picture's source, in their own browser.
  open: (which) => ipcRenderer.send('panel:open', String(which || '')),
});
