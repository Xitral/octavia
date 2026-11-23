'use strict';
// The stdin/stdout side of docs/avatar-protocol.md: JSON lines in, JSON lines out.

const fs = require('node:fs');
const net = require('node:net');
const readline = require('node:readline');

let stdoutBroken = false;

// Octavia parses every stdout line as a message, so logs go to stderr and only protocol messages go here.
function emit(msg) {
  if (stdoutBroken) return;
  try {
    process.stdout.write(`${JSON.stringify(msg)}\n`);
  } catch {
    stdoutBroken = true;
  }
}

function log(...parts) {
  try {
    process.stderr.write(`[avatar] ${parts.join(' ')}\n`);
  } catch {
    // stderr is gone too; nothing left to tell.
  }
}

// Parses one line into a message object, or null for anything that isn't `{"type": "..."}`.
function parseLine(line) {
  const text = line.trim();
  if (!text) return null;
  try {
    const msg = JSON.parse(text);
    return msg && typeof msg === 'object' && !Array.isArray(msg) && typeof msg.type === 'string' ? msg : null;
  } catch {
    return null;
  }
}

// Electron's own process.stdin reports end-of-file at once on Windows even when the parent keeps the
// pipe open, so read file descriptor 0 directly: as a pipe socket when it is one (Octavia's case), else as
// a plain stream (a console, a file, or the NUL device when there is no stdin at all).
function openStdin() {
  try {
    return new net.Socket({ fd: 0, readable: true, writable: false });
  } catch {
    return fs.createReadStream(null, { fd: 0, autoClose: false });
  }
}

// Calls onMessage for every valid line and onEnd once stdin closes. A missing or unreadable stdin
// (e.g. `--preview` started from a shortcut) counts as closed.
function readMessages(onMessage, onEnd) {
  let ended = false;
  const end = () => {
    if (!ended) {
      ended = true;
      onEnd();
    }
  };
  // A broken stdout pipe raises asynchronously; swallow it here so it can't crash the app.
  process.stdout.on('error', () => {
    stdoutBroken = true;
  });
  let input;
  try {
    input = openStdin();
  } catch {
    end();
    return;
  }
  input.on('error', end);
  const rl = readline.createInterface({ input, crlfDelay: Infinity, terminal: false });
  rl.on('line', (line) => {
    const msg = parseLine(line);
    if (msg) onMessage(msg);
  });
  rl.on('close', end);
}

module.exports = { emit, log, parseLine, readMessages };
