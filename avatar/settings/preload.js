'use strict';
// The settings page's only door to the main process: messages to and from the settings process, and its own
// title-bar buttons. No file system or Node access.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('settingsHost', {
  hello: () => ipcRenderer.invoke('settings:hello'),
  onMessage: (cb) => ipcRenderer.on('settings:message', (_e, msg) => cb(msg)),
  send: (msg) => ipcRenderer.send('settings:send', msg && typeof msg === 'object' ? JSON.parse(JSON.stringify(msg)) : null),
  window: (what) => ipcRenderer.send('settings:window', String(what)),
});
