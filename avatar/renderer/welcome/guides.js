// The guided connections: scripted steps OCTAVIA talks through, one at a time, each with a small drawing of what to
// look for on the page it opens. Scripted on purpose, so it can't wander: only "I'm stuck" asks a model.
//
// A step: say (what it says, out loud when there's a voice), open (a page key the setup process may open), picture
// (a drawing spec), field (an input of the setup to fill: its id, what to check it as, and an extra value's id),
// tips (shown when stuck and no model can answer).

export const GUIDES = {
  discord: {
    title: 'Discord',
    steps: [
      { say: "Let's make your own Discord bot. I'll open Discord's developer page.", open: 'discord',
        picture: { button: 'New Application' },
        then: 'Click New Application, give it any name, and create it.',
        tips: ['Sign in to Discord in the browser first.', 'Any name works. OCTAVIA is a good one.'] },
      { say: 'Now open Bot on the left, press Reset Token, and copy the token.',
        picture: { side: ['General Information', 'OAuth2', 'Bot'], at: 2, button: 'Reset Token' },
        field: { id: 'discord-token', check: 'discord-token', label: 'Paste the token here' },
        tips: ['Discord may ask for your password or a code before it shows the token.',
          "The token only shows once. If you lost it, reset it again."] },
      { say: 'On that same page, turn on Message Content Intent, then save.',
        picture: { side: ['General Information', 'OAuth2', 'Bot'], at: 2, toggle: 'Message Content Intent' },
        tips: ['It sits under Privileged Gateway Intents, further down the Bot page.', 'Press Save Changes after.'] },
      { say: 'Open OAuth2, then URL Generator. Tick bot, then the permissions below, open the link it makes, and add the bot to your server.',
        picture: { side: ['General Information', 'OAuth2', 'Bot'], at: 1,
          checks: ['bot', 'Send Messages', 'Read Message History', 'Attach Files', 'Connect', 'Speak'] },
        tips: ["The link is at the bottom of the page once 'bot' is ticked.", "You need a server you own or manage. Make one if you don't have one."] },
      { say: "Last one. In Discord, turn on Developer Mode, then right-click your name and copy your user ID.",
        open: 'discord-dev-mode',
        picture: { side: ['My Account', 'Advanced'], at: 1, toggle: 'Developer Mode' },
        field: { id: 'discord-owner', check: 'discord-owner', label: 'Paste your user ID here' },
        tips: ['Developer Mode is in Discord settings, under Advanced.', 'Right-click your own name anywhere, then Copy User ID.'] },
    ],
  },
  ticktick: {
    title: 'TickTick',
    steps: [
      { say: "I'll open TickTick's settings. Sign in if it asks.", open: 'ticktick', picture: { side: ['Account', 'Account & Security'], at: 1 },
        tips: ['Use TickTick on the web, not the app.'] },
      { say: 'Under Account and Security, find API Token, create one, and copy it.',
        picture: { side: ['Account', 'Account & Security'], at: 1, button: 'API Token' },
        field: { id: 'ticktick', check: 'ticktick', label: 'Paste the token here' },
        tips: ["If you don't see API Token, scroll down the Account & Security page."] },
    ],
  },
  canvas: {
    title: 'Canvas',
    steps: [
      { say: "What's your school's Canvas address? It's the one in your browser when you use Canvas.",
        picture: { address: 'yourschool.instructure.com' },
        field: { id: 'canvas-url', label: 'e.g. https://yourschool.instructure.com' },
        tips: ['Open Canvas the way you normally do and copy the start of the address, up to the first slash.'] },
      { say: "I'll open your Canvas settings. Scroll to Approved Integrations and press New Access Token.",
        open: 'canvas', picture: { side: ['Profile', 'Settings'], at: 1, button: '+ New Access Token' },
        field: { id: 'canvas-token', check: 'canvas-token', extra: 'canvas-url', label: 'Paste the token here' },
        tips: ['Any purpose works, like OCTAVIA. Leave the expiry empty or pick a date far off.',
          'Some schools turn tokens off. If the button is missing, ask your school.'] },
    ],
  },
  gmail: {
    title: 'Gmail',
    steps: [
      { say: "What's your Gmail address?", picture: { address: 'Your Gmail address' },
        field: { id: 'gmail', label: 'Your Gmail address' }, tips: ['The address you sign in to Gmail with.'] },
      { say: "Google needs 2-Step Verification on for this. I'll open the app passwords page. Name it OCTAVIA, press Create, and copy the code.",
        open: 'gmail', picture: { button: 'Create', address: 'App name: OCTAVIA' },
        field: { id: 'gmail-password', check: 'gmail-password', extra: 'gmail', label: 'Paste the 16 letters here' },
        tips: ["If the page says the setting isn't available, turn on 2-Step Verification first.",
          'School and work accounts often have app passwords turned off.'] },
    ],
  },
  fish: {
    title: 'Fish Audio',
    steps: [
      { say: "I'll open Fish Audio's API keys page. Make a free account if you need one.", open: 'fish',
        picture: { button: 'Create key' }, tips: ['The free tier is enough to start.'] },
      { say: 'Create a key and copy it.', picture: { button: 'Copy' },
        field: { id: 'fish', check: 'fish', label: 'Paste the key here' },
        tips: ['It looks like a long string of letters and numbers.'] },
    ],
  },
  anthropic: {
    title: 'Anthropic',
    steps: [
      { say: "I'll open Anthropic's console.", open: 'anthropic', picture: { button: 'Create Key' },
        tips: ['You need billing set up on the console for the key to work.'] },
      { say: "Create a key and copy it. It starts with s k dash ant.", picture: { button: 'Copy' },
        field: { id: 'anthropic', check: 'anthropic', label: 'Paste the key here' },
        tips: ['It starts with sk-ant-.'] },
    ],
  },
};

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

// A small, calm drawing of the page: a window, maybe a side menu with one item lit, and the thing to press.
export function picture(spec = {}) {
  const parts = [];
  parts.push('<rect x="1" y="1" width="318" height="158" rx="10" class="pw"/>');
  parts.push('<circle cx="14" cy="13" r="3" class="pd"/><circle cx="24" cy="13" r="3" class="pd"/><circle cx="34" cy="13" r="3" class="pd"/>');
  let left = 16;
  if (spec.side) {
    parts.push('<rect x="8" y="26" width="108" height="126" rx="6" class="ps"/>');
    spec.side.forEach((item, i) => {
      const y = 44 + i * 22;
      if (i === spec.at) parts.push(`<rect x="12" y="${y - 12}" width="100" height="18" rx="4" class="pl"/>`);
      parts.push(`<text x="18" y="${y}" class="${i === spec.at ? 'ptl' : 'pt'}">${esc(item)}</text>`);
    });
    left = 126;
  }
  const w = 320 - left - 14;
  if (spec.address) {
    parts.push(`<rect x="${left}" y="32" width="${w}" height="22" rx="5" class="pf"/>`);
    parts.push(`<text x="${left + 8}" y="47" class="pt">${esc(spec.address)}</text>`);
  }
  if (spec.button) {
    const bw = Math.min(w, 24 + spec.button.length * 7);
    const y = spec.address ? 70 : 66;
    parts.push(`<rect x="${left}" y="${y}" width="${bw}" height="26" rx="6" class="pb"/>`);
    parts.push(`<text x="${left + bw / 2}" y="${y + 17}" class="pbt" text-anchor="middle">${esc(spec.button)}</text>`);
    parts.push(`<circle cx="${left + bw - 6}" cy="${y + 22}" r="9" class="pring"/>`);
  }
  if (spec.toggle) {
    parts.push(`<text x="${left}" y="84" class="pt">${esc(spec.toggle)}</text>`);
    parts.push(`<rect x="${left + w - 40}" y="72" width="36" height="18" rx="9" class="pon"/><circle cx="${left + w - 13}" cy="81" r="7" class="pknob"/>`);
    parts.push(`<circle cx="${left + w - 22}" cy="81" r="15" class="pring"/>`);
  }
  if (spec.checks) {
    spec.checks.forEach((c, i) => {
      const x = left + (i % 2) * (w / 2);
      const y = 40 + Math.floor(i / 2) * 26;
      parts.push(`<rect x="${x}" y="${y - 10}" width="12" height="12" rx="2" class="pon"/><path d="M${x + 2} ${y - 4} l3 3 l5 -6" class="ptick"/>`);
      parts.push(`<text x="${x + 18}" y="${y}" class="pt">${esc(c)}</text>`);
    });
  }
  return `<svg viewBox="0 0 320 160" class="pic" aria-hidden="true">${parts.join('')}</svg>`;
}
