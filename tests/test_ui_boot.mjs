/**
 * Does the UI survive being built at all?
 *
 * This suite exists because of a shipped crash on load. `visible` moved from a
 * field to a getter in three classes and one stale assignment survived, so
 * `new Studio()` threw before the first frame and the page rendered nothing.
 * Every other test passed, because none of them had ever *constructed* a view.
 *
 * So these tests do the least interesting and most valuable thing: build each
 * class, run its first paint, click a few things, and assert nothing threw. No
 * layout, no appearance -- just that the code runs and asks the document for
 * elements the markup actually has.
 *
 *   node tests/test_ui_boot.mjs
 */

import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { readFile } from 'node:fs/promises';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const mod = (rel) => pathToFileURL(path.join(root, rel)).href;

const { installDom } = await import(mod('tests/dom_stub.mjs'));
const dom = await installDom(path.join(root, 'web/index.html'));

// Imported after the stub is installed: these modules touch `document` while
// they are still evaluating.
const { ViewState } = await import(mod('web/js/views.js'));
const { Reader } = await import(mod('web/js/reader.js'));
const { Envelope } = await import(mod('web/js/envelope.js'));
const { KeyboardEngine } = await import(mod('web/js/keyboard.js'));
const { Panel } = await import(mod('web/js/panel.js'));
const { Studio } = await import(mod('web/js/studio.js'));
const { DebugConsole } = await import(mod('web/js/console.js'));

let passed = 0;
let failed = 0;
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`  FAIL ${name}\n       ${error.message}`);
  }
}

async function asyncTest(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`  FAIL ${name}\n       ${error.message}`);
  }
}

const audioDoc = JSON.parse(await readFile(path.join(root, 'out/stream_audio.json'), 'utf8'));
const visualDoc = JSON.parse(await readFile(path.join(root, 'out/stream_visual.json'), 'utf8'));

/** An app object shaped like the real one, with the engines stubbed out. */
function makeApp(views = new ViewState('studio')) {
  const reader = new Reader(audioDoc, visualDoc);
  const audio = {
    started: false,
    masterGain: 0.55,
    crushBits: 8,
    delayMix: 0.22,
    delayNote: '1/8.',
    filterCutoff: 9000,
    balance: 0.5,
    keyboardCrushed: false,
    mutes: new Set(),
    soloVoice: null,
    journal: [],
    voiceLevels: () => [],
    setMaster() {}, setCrush() {}, setDelayMix() {}, setDelayNote() {},
    setCutoff() {}, setKeyboardCrushed() {}, syncDelay() {}, setJournalling() {},
    toggleMute: () => false, setSolo: () => null, playNote: () => null,
    delaySeconds: () => 0.375, graph: () => null, silenceAll() {},
  };
  const visual = {
    balance: 0.5, showGlyphs: true, showBars: true, showBanding: true,
    corruption: 1, invert: false, hidden: new Set(), showKeys: false,
    toggleVoice: () => true, flashKey() {}, clear() {}, render() {}, update() {},
  };
  const app = {
    views,
    reader,
    audio,
    visual,
    transport: {
      targetFrames: reader.length, counter: 0, lastFrame: null, lastState: {},
      retime() {}, pause() {}, stop() {}, progress: () => 0,
      setDurationSeconds() {}, toggle: async () => false, play: async () => {},
      seekNormalized() {},
    },
    envelope: Envelope.fromExport(audioDoc.envelope),
    entryStrategy: 'variance',
    entryOrder: reader.entryOrder,
    presetId: 'gritty_01',
    presets: [{ id: 'gritty_01', audio: 'a.json', visual: 'v.json', note: '' }],
    intensity: 1,
    gates: null,
    setEnvelope(envelope) { this.envelope = envelope; },
    setEntryStrategy(strategy) { this.entryStrategy = strategy; },
    setTempo(tempo) { this.reader.tempo = tempo; },
    playStreams: async () => {},
    adoptRender: async () => {},
  };
  app.keyboard = new KeyboardEngine(audio, reader);
  return app;
}

// ---------------------------------------------------------------------------
console.log('the markup has what the code asks for');

test('every id the code fetches exists in index.html', () => {
  // The whole point of building the registry from the real markup: a getElementById
  // that returns null is silent in a browser and a failure here.
  const app = makeApp();
  app.console = new DebugConsole(app);
  app.studio = new Studio(app);
  app.panel = new Panel(app);
  assert.deepEqual(
    [...dom.missing],
    [],
    `the code asked for ids the markup does not have: ${[...dom.missing].join(', ')}`,
  );
});

// ---------------------------------------------------------------------------
console.log('constructing the views');

test('the console builds and paints', () => {
  const app = makeApp();
  const view = new DebugConsole(app);
  assert.equal(view.visible, false);
  view.log('hello', 'system');
  assert.ok(view.entries.length > 0);
});

test('the panel builds and refreshes', () => {
  // This is the one that would have caught the shipped crash: `new Panel()` ran
  // `this.visible = false` against a getter-only property.
  const app = makeApp();
  const view = new Panel(app);
  assert.equal(view.visible, false);
  view.refresh();
});

test('the studio builds without a server', () => {
  // serve.py is frequently not running, and the studio is now the first screen
  // -- so failing to reach the catalog must not mean a blank page.
  const app = makeApp();
  const view = new Studio(app);
  assert.equal(view.visible, true, 'the studio should be showing on the default view');
});

test('nothing assigns to a getter-only property', () => {
  // The specific shape of the bug: three classes derive `visible` from the view
  // state, so none of them may also assign it. Strict mode makes that a throw,
  // which the constructions above would surface -- this asserts the shape
  // directly so the reason survives.
  for (const view of [DebugConsole, Panel, Studio]) {
    const descriptor = Object.getOwnPropertyDescriptor(view.prototype, 'visible');
    assert.ok(descriptor?.get, `${view.name} lost its visible getter`);
    assert.equal(descriptor.set, undefined, `${view.name} has a setter again`);
  }
});

// ---------------------------------------------------------------------------
console.log('the first paint');

await asyncTest('the studio paints with no piece open', async () => {
  const app = makeApp();
  app.console = new DebugConsole(app);
  const studio = new Studio(app);
  await studio.refreshFromServer(); // fetch rejects; must degrade, not throw
  studio.paint();
  assert.ok(studio.catalog, 'no fallback catalog after a failed load');
});

await asyncTest('the studio paints with a piece open', async () => {
  const app = makeApp();
  app.console = new DebugConsole(app);
  const studio = new Studio(app);
  await studio.loadCatalog();
  studio.folder = '01-test';
  studio.manifest = {
    name: '01-test',
    title: 'Test',
    source: { kind: 'csv', path: 'data.csv', tempo: { bpm: 96, subdivision: 16 } },
    preset: { name: 'c', chain: [{ pedal: 'delta', params: { order: 1 } }] },
    performance: { keymap: { KeyA: { kind: 'degree', degree: 0, octave: 0 } } },
  };
  studio.paint();
});

test('the panel paints every section', () => {
  const app = makeApp();
  app.console = new DebugConsole(app);
  app.studio = new Studio(app);
  const panel = new Panel(app);
  panel.refresh();
  panel.tick(); // not visible: must be a cheap no-op rather than an error
  app.views.go('stage');
  app.views.setOverlay('panel', true);
  panel.tick();
});

test('the console paints every tab', () => {
  const app = makeApp();
  const view = new DebugConsole(app);
  app.views.setOverlay('console', true);
  for (const tab of ['log', 'pipeline', 'meta', 'frame', 'audio']) {
    view.tab = tab;
    view._render();
  }
});

test('the console paints a trace', () => {
  const app = makeApp();
  const view = new DebugConsole(app);
  view.setTrace({
    format: 'serrin-trace/1',
    window: 8,
    stages: [
      {
        index: 0,
        kind: 'ingest',
        name: 'read x.csv',
        channels: [
          {
            name: 'cpu',
            stats: { min: 0, max: 255, unique: 40, entropy: 5.2, change_rate: 0.9, flat_longest: 2 },
            values: [1, 2, 3],
            truncated: true,
          },
        ],
        detail: {
          conversions: [
            {
              name: 'cpu',
              column_index: 1,
              cells: ['32.3'],
              parsed: [32.3],
              aggregated: [32.3],
              bytes: [69],
              range: { low: 0, high: 100, log_scale: false },
              unparseable_cells: 0,
            },
          ],
        },
      },
    ],
  });
  view.tab = 'pipeline';
  view._render();
});

// ---------------------------------------------------------------------------
console.log('the buttons that exist at construction time');

test('the panel header buttons are wired', () => {
  const app = makeApp();
  app.console = new DebugConsole(app);
  app.studio = new Studio(app);
  new Panel(app);
  for (const id of ['panel-close', 'ctl-play', 'ctl-stop', 'ctl-to-studio']) {
    assert.ok(dom.elements.get(id).listeners.has('click'), `${id} has no click handler`);
  }
});

test('the studio header buttons are wired', () => {
  const app = makeApp();
  new Studio(app);
  for (const id of ['studio-close', 'studio-new', 'studio-save', 'studio-render', 'studio-play']) {
    assert.ok(dom.elements.get(id).listeners.has('click'), `${id} has no click handler`);
  }
});

test('the console tabs and buttons are wired', () => {
  const app = makeApp();
  new DebugConsole(app);
  for (const id of ['console-close', 'console-clear', 'console-pause', 'console-copy']) {
    assert.ok(dom.elements.get(id).listeners.has('click'), `${id} has no click handler`);
  }
});

test('leaving the studio with nothing loaded refuses rather than showing a black screen', () => {
  const app = makeApp();
  app.console = new DebugConsole(app);
  app.reader = null;
  const studio = new Studio(app);
  assert.equal(studio.leave(), false);
  assert.ok(app.views.inStudio, 'it went to an empty stage anyway');
});

// ---------------------------------------------------------------------------
console.log('saying what went wrong');
//
// All three of these were reported as "nothing happened": choosing a file with
// no piece open, an endpoint the running server predates, and two files selected
// where one gets used. None of them threw, none of them logged, and none of them
// said anything -- which is the worst way for software to fail.

const FULL_CATALOG = {
  aggregations: ['mean', 'max'],
  endpoints: [
    '/api/catalog', '/api/source', '/api/pieces', '/api/piece',
    '/api/piece/new', '/api/piece/data', '/api/piece/graph', '/api/render',
  ],
};

/** A studio whose messages are collected instead of painted. */
function makeStudio(catalog = FULL_CATALOG) {
  const app = makeApp();
  app.console = new DebugConsole(app);
  const studio = new Studio(app);
  studio.said = [];
  studio.message = (text, warning = false) => studio.said.push({ text, warning });
  studio._get = async (route) => (route === '/api/catalog' ? catalog : {});
  return studio;
}

await asyncTest('an up-to-date server draws no complaint', async () => {
  const studio = makeStudio();
  await studio.loadCatalog();
  assert.equal(studio.stale, null);
  assert.deepEqual(studio.said, []);
});

await asyncTest('a server older than the page says which endpoints it lacks', async () => {
  // The reported symptom was a bare 404 in the browser console and nothing at
  // all in the UI. The cause was a serve.py started before the endpoint existed,
  // and the fix is to restart it -- so that is what it has to say.
  const studio = makeStudio({
    aggregations: ['mean'],
    endpoints: ['/api/catalog', '/api/pieces', '/api/piece', '/api/render'],
  });
  await studio.loadCatalog();
  assert.deepEqual(studio.stale, ['/api/source', '/api/piece/data']);
  const [warning] = studio.said;
  assert.ok(warning.warning, 'it was not flagged as a warning');
  assert.ok(warning.text.includes('older than this page'), warning.text);
  assert.ok(warning.text.includes('/api/source'), warning.text);
  assert.ok(warning.text.includes('serve.py'), warning.text);
});

await asyncTest('a server too old to advertise anything at all is still caught', async () => {
  const studio = makeStudio({ aggregations: ['mean'] });
  await studio.loadCatalog();
  assert.equal(studio.stale.length, 5);
});

await asyncTest('uploading with no piece open says so instead of returning', async () => {
  const studio = makeStudio();
  studio.folder = null;
  await studio.putData({ name: 'meteo.csv' });
  assert.equal(studio.said.length, 1);
  assert.ok(studio.said[0].warning);
  assert.ok(studio.said[0].text.includes('open or create a piece first'));
});

await asyncTest('exporting a history with no piece open says so too', async () => {
  const studio = makeStudio();
  studio.folder = null;
  await studio.exportHistory();
  assert.ok(studio.said[0].text.includes('open or create a piece first'));
});

// ---------------------------------------------------------------------------
console.log('showing how the file was read');
//
// A metadata preamble is skipped rather than refused, because it is part of the
// format every simulator writes. But skipping it is a judgement, and a judgement
// made silently is indistinguishable from a bug -- so the report is shown always
// and not only when something looks wrong.

test('the report says what parsing decided', () => {
  const studio = makeStudio();
  studio.source = {
    table: {
      delimiter: ';', header_line: 42, preamble_lines: 41,
      columns: 13, data_rows: 13, dropped_rows: 2,
    },
  };
  const html = studio._tableReport();
  assert.ok(html.includes('delimiter <b>;</b>'), html);
  assert.ok(html.includes('line <b>42</b>'), html);
  assert.ok(html.includes('41 preamble lines skipped'), html);
  assert.ok(html.includes('2 lines dropped'), html);
});

test('a tab delimiter is named rather than printed invisibly', () => {
  const studio = makeStudio();
  studio.source = { table: { delimiter: '\t', header_line: 1, columns: 4, data_rows: 9 } };
  assert.ok(studio._tableReport().includes('delimiter <b>tab</b>'));
});

test('an ordinary file reports nothing skipped', () => {
  const studio = makeStudio();
  studio.source = {
    table: {
      delimiter: ',', header_line: 1, preamble_lines: 0,
      columns: 4, data_rows: 200, dropped_rows: 0,
    },
  };
  const html = studio._tableReport();
  assert.ok(!html.includes('skipped'), html);
  assert.ok(!html.includes('dropped'), html);
  assert.ok(html.includes('200 rows'), html);
});

test('no source, no report', () => {
  const studio = makeStudio();
  studio.source = null;
  assert.equal(studio._tableReport(), '');
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
