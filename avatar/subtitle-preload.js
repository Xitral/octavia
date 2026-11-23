'use strict';
// The subtitles page's only door to the main process: lines, layouts, her feelings, voice level and the pointer in, "I've faded"
// out. No file system or
// Node access.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('subtitlesHost', {
  hello: () => ipcRenderer.send('subtitles:hello'),
  onLine: (cb) => ipcRenderer.on('subtitles:line', (_e, payload) => cb(payload)),
  onEnd: (cb) => ipcRenderer.on('subtitles:end', (_e, payload) => cb(payload)),
  onLength: (cb) => ipcRenderer.on('subtitles:length', (_e, payload) => cb(payload)),
  onWord: (cb) => ipcRenderer.on('subtitles:word', (_e, payload) => cb(payload)),
  onLayout: (cb) => ipcRenderer.on('subtitles:layout', (_e, payload) => cb(payload)),
  onFeel: (cb) => ipcRenderer.on('subtitles:feel', (_e, payload) => cb(payload)),
  onVoice: (cb) => ipcRenderer.on('subtitles:voice', (_e, level) => cb(level)),
  onPointer: (cb) => ipcRenderer.on('subtitles:pointer', (_e, p) => cb(p)),
  faded: (seq) => ipcRenderer.send('subtitles:faded', Number(seq)),
});
