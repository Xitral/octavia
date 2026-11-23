'use strict';
// The renderer's only door to the main process. It can fetch its settings and the model's and props' bytes,
// hear protocol messages and window layouts, and report back; it gets no file system or Node access.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('avatarHost', {
  init: () => ipcRenderer.invoke('avatar:init'),
  loadModel: () => ipcRenderer.invoke('avatar:load-model'),
  // A configured prop's file, by the prop's name (null when it has none or it can't be read; rejects, saying
  // why, when it's too big).
  loadProp: (name) => ipcRenderer.invoke('avatar:load-prop', String(name)),
  onMessage: (cb) => ipcRenderer.on('avatar:message', (_e, msg) => cb(msg)),
  // Where her framing sits in the window, whenever the window moves between home and pointing.
  onLayout: (cb) => ipcRenderer.on('avatar:layout', (_e, layout) => cb(layout)),
  onCursor: (cb) => ipcRenderer.on('avatar:cursor', (_e, p) => cb(p)),
  emit: (msg) => ipcRenderer.send('avatar:emit', msg),
  setIgnoreMouse: (ignore) => ipcRenderer.send('avatar:ignore-mouse', ignore === true),
  drag: (phase) => ipcRenderer.send('avatar:drag', phase),
});
