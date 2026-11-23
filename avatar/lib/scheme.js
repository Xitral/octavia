'use strict';
// Serves the renderer and its two libraries over a custom privileged scheme. ES modules and import maps
// need a real origin (file:// module loading is unreliable), and serving only whitelisted folders keeps the
// renderer from reading anything else on disk.

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const { protocol } = require('electron');

const SCHEME = 'avatar';
const ORIGIN = `${SCHEME}://app`;
const APP_DIR = path.join(__dirname, '..');

// URL prefix -> folder on disk. Nothing outside these folders is reachable.
const ROOTS = [
  ['renderer/', path.join(APP_DIR, 'renderer')],
  ['node_modules/three/build/', path.join(APP_DIR, 'node_modules', 'three', 'build')],
  ['node_modules/three/examples/jsm/', path.join(APP_DIR, 'node_modules', 'three', 'examples', 'jsm')],
  ['node_modules/@pixiv/three-vrm/lib/', path.join(APP_DIR, 'node_modules', '@pixiv', 'three-vrm', 'lib')],
];

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.jpg': 'image/jpeg',
  '.png': 'image/png',
};
// Folders an app adds for itself (the panels: the pictures Octavia downloaded and cleaned), as [prefix, folder].
const extraRoots = [];

// Must run before the app is ready.
function registerScheme() {
  protocol.registerSchemesAsPrivileged([
    // corsEnabled: a live block's sandboxed frame (an opaque origin) loads three.js and its own script as modules,
    // which are fetched with CORS; only the files it needs say it may (LIVE_SHARED).
    { scheme: SCHEME, privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true } },
  ]);
}

function resolve(urlPath) {
  let rel;
  try {
    rel = decodeURIComponent(urlPath).replace(/^\/+/, '');
  } catch {
    return null;
  }
  for (const [prefix, dir] of [...ROOTS, ...extraRoots]) {
    if (!rel.startsWith(prefix)) continue;
    const pattern = extraRoots.find((r) => r[0] === prefix && r[1] === dir);
    if (pattern && !(pattern[2] instanceof RegExp && pattern[2].test(rel.slice(prefix.length)))) return null;
    const file = path.resolve(dir, rel.slice(prefix.length));
    if (file.startsWith(dir + path.sep)) return file;
  }
  return null;
}

// The import map has to be an inline script, so the page's CSP allows exactly that script by its hash
// (computed here, so editing the map can't silently break the page). The browser hashes the script as parsed,
// with any Windows line endings already turned into plain newlines, so the same is done here.
//
// A live block's frame (renderer/live/frame.html) runs the program Octavia wrote for it, so its policy differs: it
// may turn that program's text into code ('unsafe-eval'), but it may never fetch, connect, frame or open anything,
// and its scripts come only from this app (named by the app's origin: the sandboxed frame's own origin is opaque).
// The panel's page may frame that one page and nothing else.
const LIVE_FRAME = 'renderer/live/frame.html';
const LIVE_SHARED = ['renderer/live/', 'renderer/icons/', 'node_modules/three/build/', 'node_modules/three/examples/jsm/'];

function importMapHash(html) {
  const match = html.match(/<script type="importmap">([\s\S]*?)<\/script>/);
  const script = match ? match[1].replace(/\r\n?/g, '\n') : '';
  return match ? `'sha256-${crypto.createHash('sha256').update(script).digest('base64')}'` : '';
}

function contentSecurityPolicy(html, rel = '') {
  const hash = importMapHash(html);
  if (rel === LIVE_FRAME) {
    return [
      "default-src 'none'",
      `script-src ${ORIGIN} 'unsafe-eval' ${hash}`,
      `style-src ${ORIGIN} 'unsafe-inline'`,
      'img-src blob: data:',
      "connect-src 'none'",
      "worker-src 'none'",
      "frame-src 'none'",
      "form-action 'none'",
      "base-uri 'none'",
    ].join('; ');
  }
  const framing = rel === 'renderer/panel/index.html' ? [`frame-src ${ORIGIN}/${LIVE_FRAME}`] : [];
  // The setup window: its orb in a frame of its own, and the voice previews it plays (made on this PC).
  if (rel === 'renderer/welcome/index.html') framing.push(`frame-src ${ORIGIN}/renderer/welcome/orb-frame.html`, 'media-src data:');
  return [
    "default-src 'none'",
    `script-src 'self' ${hash}`,
    "style-src 'self'",
    // GLTFLoader turns the textures embedded in the .vrm into blob: URLs and fetches them.
    "img-src 'self' blob: data:",
    'connect-src blob: data:',
    ...framing,
  ].join('; ');
}

// `extra`: more [prefix, folder] pairs this app serves (only files directly in them whose names match `name`).
function handleScheme(extra = []) {
  for (const [prefix, dir, name] of extra) if (dir) extraRoots.push([prefix, path.resolve(dir), name]);
  protocol.handle(SCHEME, async (request) => {
    const url = new URL(request.url);
    const file = url.host === 'app' ? resolve(url.pathname) : null;
    if (!file) return new Response('not found', { status: 404 });
    let body;
    try {
      body = await fs.readFile(file);
    } catch {
      return new Response('not found', { status: 404 });
    }
    const ext = path.extname(file).toLowerCase();
    const headers = { 'content-type': MIME[ext] || 'application/octet-stream' };
    const rel = path.relative(APP_DIR, file).split(path.sep).join('/');
    if (ext === '.html') headers['content-security-policy'] = contentSecurityPolicy(body.toString('utf8'), rel);
    // The live frame's files may be read by that frame (an opaque origin, so its requests are cross-origin).
    if (LIVE_SHARED.some((prefix) => rel.startsWith(prefix))) headers['access-control-allow-origin'] = '*';
    return new Response(body, { headers });
  });
}

module.exports = { ORIGIN, registerScheme, handleScheme, contentSecurityPolicy };
