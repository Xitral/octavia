'use strict';
// A marks page's only door to the main process: it fetches what to draw, hears new marks and clears, and says when
// it has drawn them. No file system or Node access.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('marksHost', {
  hello: () => ipcRenderer.invoke('marks:hello'),
  onDraw: (cb) => ipcRenderer.on('marks:draw', (_e, payload) => cb(payload)),
  onClear: (cb) => ipcRenderer.on('marks:clear', () => cb()),
  painted: (id) => ipcRenderer.send('marks:painted', String(id)),
});
