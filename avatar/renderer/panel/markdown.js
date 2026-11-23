// A small Markdown reader for her panels: text in, a plain tree out (no HTML ever comes from the text, so nothing
// in it can run or load anything). What it knows: # headings, paragraphs, - and 1. lists (one level of nesting),
// > quotes, ``` code blocks, | tables |, --- rules, $$ math blocks $$, and inline **bold**, *italic*, ~~struck~~,
// `code`, $math$ and [links](...) (shown as text: a panel never opens anything).
//
// Block nodes: {t:'h', level, kids} {t:'p', kids} {t:'list', ordered, start, items:[{kids, sub}]} {t:'quote', blocks}
// {t:'code', text} {t:'table', head, align, rows} {t:'hr'} {t:'math', tex}
// Inline nodes: {t:'text', v} {t:'b'|'i'|'s', kids} {t:'code', v} {t:'math', tex} {t:'link', url, kids} {t:'br'}

const FENCE = /^\s*(```|~~~)/;
const HEADING = /^(#{1,4})\s+(.*)$/; // closing #s trimmed by headingText, not a regex (it backtracked)
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const BULLET = /^(\s*)[-*+•]\s+(.*)$/;
const NUMBERED = /^(\s*)(\d{1,3})[.)]\s+(.*)$/;
const QUOTE = /^\s*>\s?(.*)$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function cells(line) {
  let body = line.trim();
  if (body.startsWith('|')) body = body.slice(1);
  if (body.endsWith('|') && !body.endsWith('\\|')) body = body.slice(0, -1);
  const out = [];
  let cell = '';
  for (let i = 0; i < body.length; i++) {
    if (body[i] === '\\' && body[i + 1] === '|') {
      cell += '|';
      i++;
    } else if (body[i] === '|') {
      out.push(cell.trim());
      cell = '';
    } else cell += body[i];
  }
  out.push(cell.trim());
  return out;
}

function alignOf(cell) {
  const c = cell.trim();
  if (c.startsWith(':') && c.endsWith(':')) return 'center';
  if (c.endsWith(':')) return 'right';
  return 'left';
}

// "Title ##" -> "Title": closing hashes after a space go, by hand (a regex for it backtracked on long space runs).
function headingText(raw) {
  let text = raw.trimEnd();
  let end = text.length;
  while (end > 0 && text[end - 1] === '#') end--;
  if (end < text.length && (end === 0 || /\s/.test(text[end - 1]))) text = text.slice(0, end).trimEnd();
  return text;
}

const startsBlock = (line) =>
  FENCE.test(line) || HEADING.test(line) || RULE.test(line) || BULLET.test(line) || NUMBERED.test(line) ||
  QUOTE.test(line) || /^\s*\$\$/.test(line);

export function parseBlocks(text) {
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    if (FENCE.test(line)) {
      const fence = line.trim().slice(0, 3);
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) body.push(lines[i++]);
      i++; // the closing fence (or the end)
      blocks.push({ t: 'code', text: body.join('\n') });
      continue;
    }
    if (/^\s*\$\$/.test(line)) {
      const first = line.trim().slice(2);
      if (first.endsWith('$$') && first.length > 2) {
        blocks.push({ t: 'math', tex: first.slice(0, -2).trim() });
        i++;
        continue;
      }
      const body = first ? [first] : [];
      i++;
      while (i < lines.length && !lines[i].includes('$$')) body.push(lines[i++]);
      if (i < lines.length) body.push(lines[i++].split('$$')[0]);
      blocks.push({ t: 'math', tex: body.join(' ').trim() });
      continue;
    }
    let m = HEADING.exec(line);
    if (m) {
      blocks.push({ t: 'h', level: m[1].length, kids: parseInline(headingText(m[2])) });
      i++;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push({ t: 'hr' });
      i++;
      continue;
    }
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      const head = cells(line);
      const align = cells(lines[i + 1]).map(alignOf);
      const rows = [];
      i += 2;
      while (i < lines.length && TABLE_ROW.test(lines[i])) rows.push(cells(lines[i++]));
      const width = head.length;
      blocks.push({
        t: 'table',
        head: head.map(parseInline),
        align: Array.from({ length: width }, (_, k) => align[k] || 'left'),
        rows: rows.map((r) => Array.from({ length: width }, (_, k) => parseInline(r[k] ?? ''))),
      });
      continue;
    }
    if (QUOTE.test(line)) {
      const body = [];
      while (i < lines.length && QUOTE.test(lines[i])) body.push(QUOTE.exec(lines[i++])[1]);
      blocks.push({ t: 'quote', blocks: parseBlocks(body.join('\n')) });
      continue;
    }
    if (BULLET.test(line) || NUMBERED.test(line)) {
      const ordered = !BULLET.test(line);
      const pattern = ordered ? NUMBERED : BULLET;
      const indent = pattern.exec(line)[1].length;
      const start = ordered ? Number(NUMBERED.exec(line)[2]) : 1;
      const items = [];
      while (i < lines.length && lines[i].trim()) {
        const own = pattern.exec(lines[i]);
        if (own && own[1].length <= indent) {
          items.push({ raw: ordered ? own[3] : own[2], sub: [] });
          i++;
          continue;
        }
        const deeper = BULLET.exec(lines[i]) || NUMBERED.exec(lines[i]);
        if (deeper && deeper[1].length > indent && items.length) {
          items[items.length - 1].sub.push(lines[i++]);
          continue;
        }
        if (startsBlock(lines[i]) || !items.length) break;
        items[items.length - 1].raw += ` ${lines[i++].trim()}`; // a wrapped line goes on with its item
      }
      blocks.push({
        t: 'list',
        ordered,
        start,
        items: items.map((it) => ({
          kids: parseInline(it.raw),
          sub: it.sub.length ? parseBlocks(dedent(it.sub)) : [],
        })),
      });
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !(para.length && startsBlock(lines[i])) && !isTableStart(lines, i)) {
      para.push(lines[i++]);
    }
    if (!para.length) para.push(lines[i++]); // a line nothing else took
    const kids = [];
    para.forEach((l, k) => {
      if (k) kids.push(l.endsWith('  ') ? { t: 'br' } : { t: 'text', v: ' ' });
      kids.push(...parseInline(l.trim()));
    });
    blocks.push({ t: 'p', kids: merge(kids) });
  }
  return blocks;
}

function isTableStart(lines, i) {
  return TABLE_ROW.test(lines[i]) && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]);
}

function dedent(lines) {
  const indent = Math.min(...lines.filter((l) => l.trim()).map((l) => l.match(/^\s*/)[0].length));
  return lines.map((l) => l.slice(indent)).join('\n');
}

// Neighbouring text nodes joined, so the tree stays small.
function merge(nodes) {
  const out = [];
  for (const n of nodes) {
    const last = out[out.length - 1];
    if (n.t === 'text' && last && last.t === 'text') last.v += n.v;
    else out.push(n);
  }
  return out;
}

// Spans that take nothing inside them (code, math), then the ones that nest. Each finds its earliest match.
const INLINE = [
  { t: 'code', re: /`([^`]+)`/ },
  // $x^2$ but not "$5 and $10": no space just inside the dollars, and no digit right after the closing one.
  { t: 'math', re: /\$(?!\s)([^$\n]*?[^\s$\\])\$(?!\d)|\$([^\s$])\$(?!\d)/ },
  { t: 'link', re: /\[([^\]\n]+)\]\(([^)\s]*)\)/ },
  { t: 'b', re: /\*\*(?=\S)([\s\S]*?\S)\*\*(?!\*)|__(?=\S)([\s\S]*?\S)__(?!_)/ }, // ***x*** is bold round italic
  { t: 's', re: /~~(?=\S)([\s\S]*?\S)~~/ },
  { t: 'i', re: /\*(?=[^\s*])([^*]*?[^\s*])\*|(?<![\w])_(?=[^\s_])([^_]*?[^\s_])_(?![\w])/ },
];

export function parseInline(text) {
  const s = String(text ?? '');
  if (!s) return [];
  let best = null;
  for (const rule of INLINE) {
    const m = rule.re.exec(s);
    if (m && (!best || m.index < best.m.index)) best = { rule, m };
  }
  if (!best) return [{ t: 'text', v: s }];
  const { rule, m } = best;
  const inner = m[1] ?? m[2] ?? '';
  const before = s.slice(0, m.index);
  const after = s.slice(m.index + m[0].length);
  let node;
  if (rule.t === 'code') node = { t: 'code', v: inner };
  else if (rule.t === 'math') node = { t: 'math', tex: inner };
  else if (rule.t === 'link') node = { t: 'link', url: /^https?:\/\//i.test(m[2] || '') ? m[2] : '', kids: parseInline(inner) };
  else node = { t: rule.t, kids: parseInline(inner) };
  return merge([...(before ? [{ t: 'text', v: before }] : []), node, ...parseInline(after)]);
}

// The words of a tree, for measuring and for tests.
export function plain(nodes) {
  return nodes
    .map((n) => (n.t === 'text' || n.t === 'code' ? n.v : n.t === 'math' ? n.tex : n.t === 'br' ? '\n' : n.kids ? plain(n.kids) : ''))
    .join('');
}
