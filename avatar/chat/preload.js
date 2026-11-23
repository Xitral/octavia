'use strict';
// The chat page's only door to the main process: messages to and from Octavia, its own title-bar buttons, copying,
// opening a link she wrote in the user's own browser, and the paths of files dropped on it. No file system or Node.

const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('chatHost', {
  hello: () => ipcRenderer.invoke('chat:hello'),
  onMessage: (cb) => ipcRenderer.on('chat:message', (_e, msg) => cb(msg)),
  send: (msg) => ipcRenderer.send('chat:send', msg && typeof msg === 'object' ? JSON.parse(JSON.stringify(msg)) : null),
  window: (what, on) => ipcRenderer.send('chat:window', String(what), on === true),
  copy: (text) => ipcRenderer.send('chat:copy', String(text)),
  open: (url) => ipcRenderer.send('chat:open', String(url)),
  pathsOf: (files) => Array.from(files || []).map((file) => webUtils.getPathForFile(file)).filter(Boolean),
});
