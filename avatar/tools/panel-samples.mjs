// Sample panels, one of each kind, for panels-demo.mjs.

const T = (hour, minute) => new Date(2026, 10, 1, hour, minute).getTime() / 1000;

export const SAMPLES = {
  film: {
    title: 'Screen history',
    width: 'wide',
    blocks: [
      {
        kind: 'film',
        day: 'Today',
        at: T(14, 34),
        seek: 0,
        earlier: true,
        later: false,
        caption: '',
        frames: [
          { start: T(9, 2), end: T(9, 11), app: 'Canvas Student', title: 'Enzyme kinetics', site: '', text: 'Module 4: Enzyme kinetics\nDue Friday at 11:59\nThe Michaelis constant is the substrate level at half the top speed.' },
          { start: T(9, 11), end: T(9, 13), app: 'Discord', title: 'study-group', site: '', text: '' },
          { start: T(10, 40), end: T(11, 32), app: 'Visual Studio Code', title: 'film.js - octavia', site: '', text: 'export function layoutFilm(frames) {\n  const cells = [];\nNothing snaps: it comes to rest wherever that is.' },
          { start: T(13, 5), end: T(13, 9), app: 'Spotify', title: 'Dearly Beloved', site: '', text: 'Now playing\nKingdom Hearts' },
          { start: T(14, 20), end: T(14, 58), app: 'PowerPoint', title: 'Lecture 12 slides', site: '', text: Array.from({ length: 24 }, (_, k) => `Slide ${k + 1}: one more line of what the slide said, so the words scroll as the playhead crosses the window.`).join('\n') },
          { start: T(15, 30), end: T(15, 31), app: 'Notepad', title: 'notes.txt', site: '', text: 'Ask about the lab on Tuesday' },
        ],
      },
    ],
  },
  explain: {
    title: 'How a CPU cache works',
    blocks: [
      {
        kind: 'text',
        markdown:
          '## The idea\nThe CPU keeps **recently used memory** close by, in small fast caches. A read checks *L1* first, then L2, then L3, and only then RAM.\n\n- Hit: the data is there, a few cycles\n- Miss: go one level further out\n\n| Level | Size | Latency |\n|---|---:|---:|\n| L1 | 48 KB | ~1 ns |\n| L2 | 1 MB | ~4 ns |\n| L3 | 32 MB | ~12 ns |\n| RAM | 32 GB | ~80 ns |',
      },
    ],
  },
  flow: {
    title: 'The Krebs cycle, simplified',
    blocks: [
      {
        kind: 'diagram',
        nodes: [
          { id: 'a', label: 'Acetyl-CoA', tone: 'key' },
          { id: 'c', label: 'Citrate' },
          { id: 'i', label: 'Isocitrate' },
          { id: 'k', label: 'α-Ketoglutarate', note: 'CO₂ out, NADH' },
          { id: 's', label: 'Succinyl-CoA', note: 'CO₂ out, NADH' },
          { id: 'f', label: 'Fumarate', note: 'FADH₂' },
          { id: 'o', label: 'Oxaloacetate', tone: 'glow' },
        ],
        edges: [
          { from: 'a', to: 'c', label: '+ oxaloacetate' },
          { from: 'c', to: 'i' },
          { from: 'i', to: 'k' },
          { from: 'k', to: 's' },
          { from: 's', to: 'f' },
          { from: 'f', to: 'o' },
          { from: 'o', to: 'c', label: 'starts again' },
        ],
        caption: 'Each turn: 2 CO₂, 3 NADH, 1 FADH₂, 1 GTP',
      },
    ],
  },
  decide: {
    title: 'Should this be a background job?',
    width: 'wide',
    blocks: [
      {
        kind: 'diagram',
        direction: 'right',
        nodes: [
          { id: 'q', label: 'Takes over a minute?', shape: 'diamond' },
          { id: 'n', label: 'Run it now', shape: 'round' },
          { id: 'r', label: 'Must survive a restart?', shape: 'diamond' },
          { id: 'l', label: 'run_long_command', tone: 'key', shape: 'round' },
          { id: 'b', label: 'Background task', tone: 'glow', shape: 'round' },
        ],
        edges: [
          { from: 'q', to: 'n', label: 'no' },
          { from: 'q', to: 'r', label: 'yes' },
          { from: 'r', to: 'l', label: 'yes' },
          { from: 'r', to: 'b', label: 'no', dashed: true },
        ],
      },
    ],
  },
  numbers: {
    title: 'Your week of sleep',
    blocks: [
      {
        kind: 'facts',
        items: [
          { label: 'Average', value: '7h 12m' },
          { label: 'Best', value: '8h 40m', note: 'Saturday' },
          { label: 'Sleep score', value: '81' },
        ],
      },
      {
        kind: 'chart',
        style: 'bar',
        labels: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        series: [{ name: 'Hours', values: [6.5, 7.1, 6.2, 7.4, 6.9, 8.7, 7.6] }],
        unit: 'h',
      },
      {
        kind: 'chart',
        style: 'line',
        labels: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        series: [
          { name: 'Resting HR', values: [54, 55, 57, 53, 52, 51, 53] },
          { name: 'HRV', values: [48, 46, 41, 50, 53, 58, 55] },
        ],
      },
    ],
  },
  share: {
    title: 'Where the time went today',
    blocks: [
      {
        kind: 'chart',
        style: 'pie',
        labels: ['Classes', 'Side project', 'DataCamp', 'Gym', 'Gaming'],
        series: [{ values: [3, 2.5, 1, 1.25, 1.5] }],
        unit: 'h',
      },
    ],
  },
  plan: {
    title: 'Tonight',
    blocks: [
      {
        kind: 'steps',
        items: [
          { time: '7:00', title: 'Finish the stats worksheet', text: 'Questions 4 to 9; it is due at **11:59 PM**' },
          { time: '8:15', title: 'DataCamp: joins chapter' },
          { time: '9:00', title: 'Gym', text: 'Push day' },
          { time: '10:30', title: 'Wind down' },
        ],
      },
    ],
  },
  versus: {
    title: 'TCP or UDP',
    width: 'wide',
    blocks: [
      {
        kind: 'compare',
        columns: [
          { title: 'TCP', points: ['Connection first, then data', 'Every byte arrives, **in order**', 'Slower to start', 'Web pages, email, files'] },
          { title: 'UDP', points: ['Just sends', 'Packets can be lost or reordered', 'Low latency', 'Games, calls, video'] },
        ],
      },
      { kind: 'text', markdown: 'Rule of thumb: if a late packet is useless, **UDP**.' },
    ],
  },
  math: {
    title: 'The quadratic formula',
    blocks: [
      {
        kind: 'text',
        markdown:
          'For $ax^2 + bx + c = 0$ with $a \\ne 0$:\n\n$$x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$$\n\nThe part under the root, $\\Delta = b^2 - 4ac$, says how many real roots:\n\n1. $\\Delta > 0$: two\n2. $\\Delta = 0$: one\n3. $\\Delta < 0$: none (two complex)',
      },
    ],
  },
  answer: {
    title: 'Your turn',
    blocks: [
      { kind: 'text', markdown: 'What does the **α** value mean at a MAX node?' },
      {
        kind: 'input',
        fields: [
          { id: 'answer', label: 'In your own words', placeholder: 'Type here (Ctrl+Enter sends)', lines: 3 },
          { id: 'sure', label: 'How sure are you?', choices: ['Guessing', 'Fairly sure', 'Certain'] },
        ],
      },
    ],
  },
  drawing: {
    title: 'A right triangle',
    blocks: [
      {
        kind: 'svg',
        alt: 'A right triangle with sides a, b and hypotenuse c',
        svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 160"><polygon points="20,140 200,140 20,20" fill="none" stroke="#d9a441" stroke-width="2"/><rect x="20" y="124" width="16" height="16" fill="none" stroke="#d9a441"/><text x="110" y="156" fill="#efe6d2" font-size="14" text-anchor="middle">b</text><text x="8" y="84" fill="#efe6d2" font-size="14">a</text><text x="118" y="72" fill="#4fc3ff" font-size="14">c</text></svg>',
        caption: '$a^2 + b^2 = c^2$',
      },
    ],
  },
};

// One panel she walks through, changing it as she goes (panels-demo.mjs --live).
export const LIVE = {
  title: 'Glycolysis, step by step',
  blocks: [
    {
      kind: 'diagram',
      direction: 'right',
      nodes: [
        { id: 'g', label: 'Glucose', tone: 'key' },
        { id: 'p', label: 'Pyruvate', note: '2 per glucose' },
        { id: 'a', label: 'Acetyl-CoA', tone: 'glow' },
      ],
      edges: [
        { from: 'g', to: 'p', label: '10 steps' },
        { from: 'p', to: 'a' },
      ],
    },
    {
      kind: 'steps',
      items: [
        { title: 'Glucose is split in two' },
        { title: 'Each half becomes pyruvate', text: 'net **2 ATP**, 2 NADH' },
      ],
    },
  ],
};
