'use strict';
// The setup page's only door to the main process: messages to and from the setup process, and its own title-bar
// buttons. No file system or Node access.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('welcomeHost', {
  hello: () => ipcRenderer.invoke('welcome:hello'),
  onMessage: (cb) => ipcRenderer.on('welcome:message', (_e, msg) => cb(msg)),
  send: (msg) => ipcRenderer.send('welcome:send', msg && typeof msg === 'object' ? JSON.parse(JSON.stringify(msg)) : null),
  window: (what) => ipcRenderer.send('welcome:window', String(what)),
});
