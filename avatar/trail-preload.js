'use strict';
// The trail windows' only door to the main process: they hear what to draw and say when they're done. No file
// system or Node access, and nothing else of the avatar's.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('trailHost', {
  onDraw: (cb) => ipcRenderer.on('trail:draw', (_e, msg) => cb(msg)),
  onBoom: (cb) => ipcRenderer.on('trail:boom', (_e, msg) => cb(msg)),
  done: (id) => ipcRenderer.send('trail:done', id),
});
