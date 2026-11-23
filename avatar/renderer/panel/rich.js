// Text as a tree of elements, her panels' and her chat window's alike: Markdown (markdown.js) and TeX (tex.js) in, DOM
// out. Every word goes in as a text node, never as markup, so nothing in it can run or load anything.

import { parseBlocks } from './markdown.js';
import { parseTex } from './tex.js';

function h(tag, cls, kids) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  for (const kid of kids || []) if (kid != null) el.append(kid);
  return el;
}

// Letters in a formula are variables, set in italics; digits, operators, \text{...} and words like "sin" stay
// upright. A hyphen is a minus.
function mathText(v) {
  const out = document.createDocumentFragment();
  for (const run of v.replace(/-/g, '−').match(/[A-Za-z]+|[^A-Za-z]+/g) || []) {
    out.append(/[A-Za-z]/.test(run) ? h('i', 'var', run) : document.createTextNode(run));
  }
  return out;
}

export function tex(nodes) {
  return nodes.map((n) => {
    if (n.t === 'text') return mathText(n.v);
    if (n.t === 'word') return document.createTextNode(n.v);
    if (n.t === 'sup' || n.t === 'sub') return h(n.t, null, tex(n.kids));
    if (n.t === 'frac') return h('span', 'frac', [h('span', 'num', tex(n.num)), h('span', 'den', tex(n.den))]);
    if (n.t === 'sqrt') return h('span', 'sqrt', ['√', h('span', 'radicand', tex(n.kids))]);
    return h('span', null, tex(n.kids || []));
  });
}

export function inline(nodes) {
  return nodes.map((n) => {
    switch (n.t) {
      case 'text':
        return document.createTextNode(n.v);
      case 'br':
        return h('br');
      case 'b':
        return h('strong', null, inline(n.kids));
      case 'i':
        return h('em', null, inline(n.kids));
      case 's':
        return h('del', null, inline(n.kids));
      case 'code': {
        const c = h('code');
        c.textContent = n.v;
        return c;
      }
      case 'math':
        return h('span', 'math', tex(parseTex(n.tex)));
      case 'link': {
        const link = h('span', 'link', inline(n.kids));
        if (/^https?:\/\//i.test(n.url || '')) link.dataset.url = n.url; // a panel opens nothing; her chat window does
        return link;
      }
      default:
        return null;
    }
  });
}

export function blocks(list) {
  return list.map((b) => {
    switch (b.t) {
      case 'h':
        return h(`h${Math.min(b.level + 1, 5)}`, null, inline(b.kids));
      case 'p':
        return h('p', null, inline(b.kids));
      case 'hr':
        return h('hr');
      case 'code': {
        const pre = h('pre');
        pre.textContent = b.text;
        return pre;
      }
      case 'math':
        return h('div', 'math display', tex(parseTex(b.tex)));
      case 'quote':
        return h('blockquote', null, blocks(b.blocks));
      case 'list': {
        const list = h(b.ordered ? 'ol' : 'ul', null, b.items.map((it) => h('li', null, [...inline(it.kids), ...blocks(it.sub)])));
        if (b.ordered && b.start !== 1) list.start = b.start;
        return list;
      }
      case 'table': {
        const cell = (tag, kids, k) => {
          const c = h(tag, null, inline(kids));
          c.classList.add(`align-${b.align[k]}`);
          return c;
        };
        return h('div', 'table-wrap', [
          h('table', null, [
            h('thead', null, [h('tr', null, b.head.map((c, k) => cell('th', c, k)))]),
            h('tbody', null, b.rows.map((r) => h('tr', null, r.map((c, k) => cell('td', c, k))))),
          ]),
        ]);
      }
      default:
        return null;
    }
  });
}

export const markdown = (text) => blocks(parseBlocks(text));
