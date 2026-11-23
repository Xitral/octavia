// Enough TeX for study panels: x^2, a_{n+1}, \frac{a}{b}, \sqrt{x}, \text{...}, Greek letters and the usual symbols
// (\le, \to, \forall, \sum...). Turned into a plain tree, drawn by the panel with spans; anything it doesn't know is
// shown as written, so a formula is never lost, only plainer.
//
// Nodes: {t:'text', v} {t:'word', v} {t:'sup'|'sub', kids} {t:'frac', num, den} {t:'sqrt', kids} {t:'group', kids}
// ('word' is upright text: \text{...} and function names like sin; letters in 'text' are variables.)

const SYMBOLS = {
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', varepsilon: 'ε', zeta: 'ζ', eta: 'η', theta: 'θ',
  iota: 'ι', kappa: 'κ', lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ', tau: 'τ',
  upsilon: 'υ', phi: 'φ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
  Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π', Sigma: 'Σ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
  times: '×', cdot: '·', div: '÷', pm: '±', mp: '∓', le: '≤', leq: '≤', ge: '≥', geq: '≥', ne: '≠', neq: '≠',
  approx: '≈', equiv: '≡', sim: '∼', propto: '∝', infty: '∞', partial: '∂', nabla: '∇', sum: '∑', prod: '∏',
  int: '∫', oint: '∮', to: '→', rightarrow: '→', leftarrow: '←', Rightarrow: '⇒', Leftarrow: '⇐',
  leftrightarrow: '↔', Leftrightarrow: '⇔', iff: '⇔', implies: '⇒', mapsto: '↦', in: '∈', notin: '∉',
  subset: '⊂', subseteq: '⊆', supset: '⊃', supseteq: '⊇', cup: '∪', cap: '∩', emptyset: '∅', varnothing: '∅',
  forall: '∀', exists: '∃', neg: '¬', lnot: '¬', land: '∧', wedge: '∧', lor: '∨', vee: '∨', oplus: '⊕',
  otimes: '⊗', circ: '∘', degree: '°', angle: '∠', perp: '⊥', parallel: '∥', cdots: '⋯', ldots: '…', dots: '…',
  langle: '⟨', rangle: '⟩', lfloor: '⌊', rfloor: '⌋', lceil: '⌈', rceil: '⌉', hbar: 'ħ', ell: 'ℓ',
  mathbb: '', quad: ' ', qquad: '  ', ',': ' ', ';': ' ', '!': '', ' ': ' ', '{': '{', '}': '}', '%': '%',
  '$': '$', '#': '#', '&': '&', '_': '_', '\\': ' ', left: '', right: '', big: '', Big: '',
  lim: 'lim', log: 'log', ln: 'ln', sin: 'sin', cos: 'cos', tan: 'tan', exp: 'exp', max: 'max', min: 'min',
  det: 'det', mod: 'mod',
};
// \mathbb{R} and friends.
const DOUBLE_STRUCK = { R: 'ℝ', N: 'ℕ', Z: 'ℤ', Q: 'ℚ', C: 'ℂ', P: 'ℙ' };
const MAX_DEPTH = 12;

export function parseTex(source) {
  const s = String(source ?? '');
  let i = 0;

  // One "atom": a {group}, a \command (with its arguments), or a single character.
  function atom(depth) {
    if (s[i] === '{') {
      i++;
      return { t: 'group', kids: row(depth + 1, '}') };
    }
    if (s[i] === '\\') {
      i++;
      let name = '';
      if (/[A-Za-z]/.test(s[i] ?? '')) while (/[A-Za-z]/.test(s[i] ?? '')) name += s[i++];
      else name = s[i++] ?? '';
      if (name === 'frac' || name === 'dfrac' || name === 'tfrac') {
        return { t: 'frac', num: arg(depth), den: arg(depth) };
      }
      if (name === 'sqrt') return { t: 'sqrt', kids: arg(depth) };
      if (name === 'text' || name === 'mathrm' || name === 'textbf' || name === 'mathbf' || name === 'operatorname') {
        return { t: 'word', v: rawArg() };
      }
      if (name === 'mathbb') {
        const v = rawArg();
        return { t: 'text', v: [...v].map((c) => DOUBLE_STRUCK[c] || c).join('') };
      }
      if (name === 'vec' || name === 'hat' || name === 'bar' || name === 'overline') {
        return { t: 'group', kids: [...arg(depth), { t: 'text', v: name === 'vec' ? '⃗' : name === 'hat' ? '̂' : '̄' }] };
      }
      if (Object.hasOwn(SYMBOLS, name)) return { t: /^[a-z]{2,}$/.test(SYMBOLS[name]) ? 'word' : 'text', v: SYMBOLS[name] };
      return { t: 'text', v: `\\${name}` };
    }
    return { t: 'text', v: s[i++] };
  }

  // A {braced} argument, or the next atom when there are no braces.
  function arg(depth) {
    while (s[i] === ' ') i++;
    if (depth > MAX_DEPTH || i >= s.length) return [];
    const a = atom(depth + 1);
    return a.t === 'group' ? a.kids : [a];
  }

  function rawArg() {
    while (s[i] === ' ') i++;
    if (s[i] !== '{') return s[i++] ?? '';
    let level = 0;
    let out = '';
    for (i++; i < s.length; i++) {
      if (s[i] === '{') level++;
      else if (s[i] === '}' && level-- === 0) {
        i++;
        break;
      }
      out += s[i];
    }
    return out;
  }

  function row(depth, until) {
    const kids = [];
    while (i < s.length) {
      if (until && s[i] === until) {
        i++;
        break;
      }
      if (s[i] === '^' || s[i] === '_') {
        const t = s[i++] === '^' ? 'sup' : 'sub';
        kids.push({ t, kids: depth > MAX_DEPTH ? [] : arg(depth) });
        continue;
      }
      if (depth > MAX_DEPTH) {
        kids.push({ t: 'text', v: s[i++] });
        continue;
      }
      kids.push(atom(depth));
    }
    return joinText(kids);
  }

  return row(0, null);
}

function joinText(kids) {
  const out = [];
  for (const k of kids) {
    const last = out[out.length - 1];
    if (k.t === 'text' && last && last.t === 'text') last.v += k.v;
    else out.push(k);
  }
  return out;
}

// The formula as one line of text (for measuring, and what a screen reader would say).
export function texText(nodes) {
  return nodes
    .map((n) => {
      if (n.t === 'text' || n.t === 'word') return n.v;
      if (n.t === 'sup') return `^${texText(n.kids)}`;
      if (n.t === 'sub') return `_${texText(n.kids)}`;
      if (n.t === 'frac') return `(${texText(n.num)})/(${texText(n.den)})`;
      if (n.t === 'sqrt') return `√(${texText(n.kids)})`;
      return texText(n.kids);
    })
    .join('');
}
