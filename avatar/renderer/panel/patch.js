// What changed between two versions of a panel, block by block, so the page redraws only that: a block that's the
// same is kept as it is, a diagram whose only change is which nodes glow just has them lit, a steps block whose
// steps only changed state or got new ones at the end keeps the ones it had, anything else is rebuilt. Pure.
//
// planUpdate(old, next) -> [{action, index, from?}] for each block of `next`:
//   keep (old block `from` as it is) | highlight (relight old block `from`) | steps (old block `from`: keep its steps,
//   restate them, add `added` new ones) | scene (old block `from`: change only the elements that changed) | rebuild |
//   live (old block `from`: the same program, only its controls or values changed: it keeps running) | add (a block
//   that wasn't there). A rebuilt live block keeps `from`, so the values the user set carry over. Old blocks are
//   matched by id first, so one moved or inserted before doesn't make the rest redraw.

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function withoutHighlights(block) {
  return { ...block, nodes: (block.nodes || []).map(({ highlight, ...node }) => node) };
}

function stepsOnlyGrew(a, b) {
  const items = (block) => (block.items || []).map(({ state, ...item }) => item);
  const before = items(a);
  const after = items(b);
  if (after.length < before.length) return false;
  const { items: _a, ...restA } = a;
  const { items: _b, ...restB } = b;
  return same(restA, restB) && before.every((item, k) => same(item, after[k]));
}

// Which old block a new one is: the one with its id; else one exactly the same not yet taken (a block that moved, or
// stayed while one was inserted before it); else the one at its place, when it's the same kind.
function match(old, next) {
  const taken = new Set();
  const pick = (j) => (j >= 0 && !taken.has(j) ? (taken.add(j), j) : undefined);
  const firstPass = next.map((block) => {
    if (block.id) return pick(old.findIndex((b) => b.id === block.id));
    const exact = old.findIndex((b, j) => !taken.has(j) && same(b, block));
    return exact >= 0 ? pick(exact) : undefined;
  });
  return firstPass.map((j, index) => {
    if (j !== undefined) return j;
    const here = old[index];
    return here && !here.id && here.kind === next[index].kind ? pick(index) : undefined;
  });
}

// A worksheet with the same blanks, in the same order: marked, or sent again (its typed answers stay).
function sameBlanks(a, b) {
  const ids = (x) => (x.blanks || []).map((blank) => blank.id).join('\u0000');
  return ids(a) === ids(b);
}

export function planUpdate(old, next) {
  const from = match(old, next);
  return next.map((block, index) => {
    const j = from[index];
    const before = j === undefined ? null : old[j];
    if (!before) return { action: 'add', index };
    if (same(before, block)) return { action: 'keep', index, from: j };
    if (before.kind !== block.kind) return { action: 'rebuild', index };
    if (block.kind === 'diagram' && same(withoutHighlights(before), withoutHighlights(block))) {
      return { action: 'highlight', index, from: j };
    }
    if (block.kind === 'steps' && stepsOnlyGrew(before, block)) {
      return { action: 'steps', index, from: j, added: block.items.length - before.items.length };
    }
    if (block.kind === 'sheet' && sameBlanks(before, block)) return { action: 'sheet', index, from: j };
    if (block.kind === 'live') {
      const same = before.code === block.code && (Number(before.height) || 340) === (Number(block.height) || 340);
      return same ? { action: 'live', index, from: j } : { action: 'rebuild', index, from: j };
    }
    if (block.kind === 'film') return { action: 'film', index, from: j }; // it changes itself: never redrawn
    if (block.kind === 'scene' && before.width === block.width && before.height === block.height) {
      return { action: 'scene', index, from: j };
    }
    return { action: 'rebuild', index };
  });
}
