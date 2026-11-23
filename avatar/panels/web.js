'use strict';
// Web pages on her panels (show_page): each in a sandboxed view of its own, inside the panel's window, on a session
// that holds nothing of the user's. See docs/visuals.md.
//
// The session (`partition` without "persist:") lives in memory only: no cookies, logins or history of the user's
// browser, nothing kept after the app quits. Every request is checked (`allowed`): http(s), ws(s), data: and blob:
// only, and never an address on this PC or the home network (names are looked up first; the answer is cached a
// minute). Downloads are cancelled, pop-ups refused, dialogs off, and every permission (camera, microphone,
// notifications, location...) refused. The page has no preload and no Node: it's a web page and nothing more.
//
// The address comes from Octavia's spec only, never from the panel's own page, which only says where on itself the
// page goes (`setBounds`).

const dns = require('node:dns');
const net = require('node:net');

const PARTITION = 'octavia-web'; // in memory (no "persist:"): nothing of the user's, nothing kept
const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36';
const DNS_TTL_MS = 60_000;
const LOCAL_NAMES = /(^|\.)(localhost|local|lan|internal|home\.arpa|test)$/i;

// An IPv6 address as its eight 16-bit groups (with a dotted IPv4 tail turned into two), or null.
function groups6(addr) {
  let text = addr.toLowerCase();
  const tail = /(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(text);
  if (tail) {
    const [a, b, c, d] = tail.slice(1).map(Number);
    text = `${text.slice(0, tail.index)}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`;
  }
  const [head, rest] = text.split('::');
  const left = head ? head.split(':') : [];
  const right = rest !== undefined && rest ? rest.split(':') : [];
  const fill = rest !== undefined ? 8 - left.length - right.length : 0;
  const all = [...left, ...Array(Math.max(0, fill)).fill('0'), ...right].map((g) => parseInt(g || '0', 16));
  return all.length === 8 && all.every((g) => Number.isInteger(g) && g >= 0 && g <= 0xffff) ? all : null;
}

function private4([a, b, c]) {
  return (
    a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && (b === 168 || (b === 0 && (c === 0 || c === 2)) || (b === 88 && c === 99))) ||
    (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) ||
    (a === 203 && b === 0 && c === 113)
  ); // prettier-ignore
}

// True for an address on this PC, the home network, link-local, carrier NAT, multicast, documentation or reserved,
// however it's written (IPv4 inside IPv6 too: ::ffff:7f00:1 is 127.0.0.1). Anything not an address counts as private.
function privateAddress(ip) {
  const addr = String(ip).replace(/^\[|\]$/g, '').split('%')[0];
  const kind = net.isIP(addr);
  if (kind === 4) return private4(addr.split('.').map(Number));
  if (kind !== 6) return true;
  const g = groups6(addr);
  if (!g) return true;
  const zeros = (n) => g.slice(0, n).every((x) => x === 0);
  // ::/96 (IPv4-compatible, :: and ::1 among them), ::ffff:0:0/96 (IPv4-mapped), 64:ff9b::/96 (NAT64): their IPv4
  if (zeros(6) || (zeros(5) && g[5] === 0xffff) || (g[0] === 0x64 && g[1] === 0xff9b && g.slice(2, 6).every((x) => x === 0))) {
    if (zeros(6) && g[6] === 0 && g[7] <= 1) return true; // :: and ::1
    return private4([g[6] >> 8, g[6] & 0xff, g[7] >> 8]);
  }
  return (
    (g[0] & 0xfe00) === 0xfc00 || // unique local
    (g[0] & 0xffc0) === 0xfe80 || // link-local
    (g[0] & 0xffc0) === 0xfec0 || // site-local
    (g[0] & 0xff00) === 0xff00 || // multicast
    g[0] === 0x2002 || // 6to4: an IPv4 inside
    (g[0] === 0x2001 && g[1] === 0x0db8) || // documentation
    (g[0] === 0x2001 && g[1] < 0x200) || // Teredo and other special use
    g[0] < 0x2000 || g[0] >= 0x4000 // outside global unicast 2000::/3
  );
}

class Guard {
  // `lookup(host)` -> Promise<string[]> (tests stand one in); `allowLocal`: checks only, a test server on this PC.
  constructor({ lookup, allowLocal = false } = {}) {
    this.lookup = lookup || ((host) => dns.promises.lookup(host, { all: true, verbatim: true }).then((r) => r.map((a) => a.address)));
    this.allowLocal = allowLocal;
    this.cache = new Map(); // host -> { ok, at }
  }

  // Whether a request for `url` may go out.
  async allowed(url) {
    let u;
    try {
      u = new URL(url);
    } catch {
      return false;
    }
    if (u.protocol === 'data:' || u.protocol === 'blob:') return true;
    if (!['http:', 'https:', 'ws:', 'wss:'].includes(u.protocol)) return false;
    if (u.username || u.password) return false;
    if (this.allowLocal) return true;
    const host = u.hostname.replace(/^\[|\]$/g, '').toLowerCase().replace(/\.+$/, ''); // "localhost." is localhost
    if (!host) return false;
    if (net.isIP(host)) return !privateAddress(host);
    if (LOCAL_NAMES.test(host) || !host.includes('.')) return false;
    const known = this.cache.get(host);
    if (known && Date.now() - known.at < DNS_TTL_MS) return known.ok;
    let ok = false;
    try {
      const addresses = await this.lookup(host);
      ok = addresses.length > 0 && addresses.every((a) => !privateAddress(a));
    } catch {
      ok = false; // a name that doesn't resolve can't load anyway
    }
    if (this.cache.size > 500) this.cache.clear();
    this.cache.set(host, { ok, at: Date.now() });
    return ok;
  }
}

// Sets up the web pages' session once: the request guard, no downloads, no permissions, a plain browser's name.
function webSession(electronSession, guard) {
  const ses = electronSession.fromPartition(PARTITION, { cache: true });
  if (ses.__octaviaReady) return ses;
  ses.__octaviaReady = true;
  ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  if (typeof ses.setDevicePermissionHandler === 'function') ses.setDevicePermissionHandler(() => false);
  if (typeof ses.setDisplayMediaRequestHandler === 'function') ses.setDisplayMediaRequestHandler((_req, callback) => callback({}));
  ses.on('will-download', (event, item) => {
    event.preventDefault();
    try {
      item.cancel();
    } catch {
      // already gone
    }
  });
  ses.setUserAgent(UA);
  ses.webRequest.onBeforeRequest((details, callback) => {
    guard.allowed(details.url).then(
      (ok) => callback({ cancel: !ok }),
      () => callback({ cancel: true }),
    );
  });
  return ses;
}

const WEB_PREFERENCES = {
  partition: PARTITION,
  sandbox: true,
  contextIsolation: true,
  nodeIntegration: false,
  nodeIntegrationInSubFrames: false,
  nodeIntegrationInWorker: false,
  webviewTag: false,
  disableDialogs: true,
  spellcheck: false,
  navigateOnDragDrop: false,
  autoplayPolicy: 'user-gesture-required',
  safeDialogs: true,
};

// One page on one panel. `make(prefs)` returns the view (a WebContentsView, or in checks an off-screen window).
class PageView {
  constructor({ url, make, report, guard }) {
    this.url = url;
    this.guard = guard;
    this.report = report; // (state message) -> the panel's page and Octavia
    this.view = make({ ...WEB_PREFERENCES });
    this.wc = this.view.webContents;
    this.code = 0;
    this.closed = false;
    this.wc.setWindowOpenHandler(() => ({ action: 'deny' })); // pop-ups and new tabs: ↗ opens the browser instead
    // WebRTC's UDP never passes the request guard: none of it except through a proxy (there is none).
    if (typeof this.wc.setWebRTCIPHandlingPolicy === 'function') this.wc.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
    const onlyWeb = (event, target) => {
      if (!/^https?:/i.test(String(target && target.url ? target.url : target))) event.preventDefault();
    };
    this.wc.on('will-navigate', onlyWeb);
    this.wc.on('will-redirect', onlyWeb);
    this.wc.on('did-start-loading', () => this.send('loading'));
    this.wc.on('did-navigate', (_e, navUrl, code) => {
      this.code = Number(code) || 0;
      this.current = navUrl;
    });
    this.wc.on('did-finish-load', () => this.send('loaded'));
    this.wc.on('did-fail-load', (_e, code, description, validatedUrl, isMainFrame) => {
      if (!isMainFrame || code === -3) return; // -3: cut short by the next navigation
      this.send(code === -20 ? 'blocked' : 'failed', { error: code === -20 ? 'that address is blocked' : String(description || code), url: validatedUrl });
    });
    this.wc.on('render-process-gone', (_e, details) => this.send('failed', { error: `the page crashed (${details.reason})` }));
    this.wc.on('page-title-updated', () => this.state === 'loaded' && this.send('loaded'));
  }

  load() {
    this.wc.loadURL(this.url).catch(() => {}); // a failure comes as did-fail-load
  }

  // Shows a new address in the same view.
  navigate(url) {
    this.url = url;
    this.code = 0;
    this.load();
  }

  send(state, extra = {}) {
    if (this.closed) return;
    this.state = state;
    let title = '';
    let current = this.url;
    try {
      title = this.wc.getTitle();
      current = this.wc.getURL() || this.url;
    } catch {
      // gone
    }
    this.report({ state, url: extra.url || current, title: String(title || '').slice(0, 200), code: this.code, error: extra.error || '' });
  }

  currentUrl() {
    try {
      return this.wc.getURL() || this.url;
    } catch {
      return this.url;
    }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    try {
      if (!this.wc.isDestroyed()) this.wc.close();
    } catch {
      // gone already
    }
    if (typeof this.view.destroy === 'function') this.view.destroy();
  }
}

module.exports = { Guard, PageView, PARTITION, WEB_PREFERENCES, privateAddress, webSession };
