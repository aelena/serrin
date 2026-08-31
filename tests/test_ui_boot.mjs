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

// ---------------------------------------------------------------------------
console.log('the column picker');
//
// It went missing: a refactor deleted the body and left the call, so
// _sourceReport threw for every CSV. Nothing caught it because nothing had ever
// asked a Studio to render a CSV source report.

const CSV_SOURCE = {
  kind: 'csv',
  rows: 140256,
  problems: ['skipped 8 line(s) of preamble; the header looks like line 9: time, G(i)'],
  table: {
    delimiter: ',', header_line: 9, preamble_lines: 8,
    columns: 6, data_rows: 140256, dropped_rows: 6, named_header: true,
  },
  columns: [
    { name: 'time', reason: 'monotonic', chosen: false, low: 0, high: 140255 },
    { name: 'G(i)', reason: '', chosen: true, low: 0, high: 1012.4 },
    { name: 'H_sun', reason: '', chosen: true, low: 0, high: 64.2 },
    { name: 'flat', reason: 'constant', chosen: false, low: 7, high: 7 },
    { name: 'Int', reason: 'not numeric', chosen: false, low: null, high: null },
  ],
};

test('every method _sourceReport calls actually exists', () => {
  // The generic version of the bug, so the next deleted body fails here.
  const studio = makeStudio();
  for (const name of ['_sourceReport', '_tableReport', '_columnPicker', '_graphReport', '_safe']) {
    assert.equal(typeof studio[name], 'function', `${name} is not a function`);
  }
});

test('a csv source report renders without throwing', () => {
  const studio = makeStudio();
  studio.source = CSV_SOURCE;
  const html = studio._sourceReport('csv');
  assert.ok(html.includes('preamble'), 'the problem list is missing');
  assert.ok(html.includes('header on line <b>9</b>'), 'the table report is missing');
  assert.ok(html.includes('data-column="G(i)"'), 'the column picker is missing');
  // And crucially: no "failed to render" from the _safe wrapper.
  assert.ok(!html.includes('failed to render'), html);
});

test('the picker says why each column would be dropped', () => {
  const studio = makeStudio();
  studio.source = CSV_SOURCE;
  const html = studio._columnPicker();
  assert.ok(html.includes('monotonic'), 'no reason for time');
  assert.ok(html.includes('constant'), 'no reason for flat');
  assert.ok(html.includes('not numeric'), 'no reason for Int');
  assert.ok(html.includes('2 of 5 columns usable'), html);
});

test('unusable columns cannot be ticked', () => {
  const studio = makeStudio();
  studio.source = CSV_SOURCE;
  const html = studio._columnPicker();
  // Serrin does not clean data, so an unusable column is not an option -- it is
  // a thing to fix upstream, and offering the box would imply otherwise.
  const timeRow = html.slice(html.indexOf('data-column="time"'));
  assert.ok(timeRow.slice(0, 120).includes('disabled'), timeRow.slice(0, 120));
});

test('a column with no range does not crash on toPrecision', () => {
  // `Int` comes back with low: null, which is what a non-numeric column gets.
  const studio = makeStudio();
  studio.source = { columns: [{ name: 'Int', reason: 'not numeric', chosen: false, low: null, high: null }] };
  assert.doesNotThrow(() => studio._columnPicker());
});

test('no columns, no picker', () => {
  const studio = makeStudio();
  studio.source = { kind: 'csv', columns: [] };
  assert.equal(studio._columnPicker(), '');
});

// ---------------------------------------------------------------------------
console.log('a bug in the page is not a complaint about the data');

test('a section that throws says so, and says whose fault it is', () => {
  const studio = makeStudio();
  studio.source = CSV_SOURCE;
  studio._columnPicker = () => {
    throw new Error('deliberate');
  };
  const html = studio._sourceReport('csv');
  assert.ok(html.includes('failed to render'), html);
  assert.ok(html.includes('bug in Serrin'), 'it did not disown the failure');
  // The rest of the report survives: one broken section is not a blank panel.
  assert.ok(html.includes('header on line <b>9</b>'), 'the table report was lost too');
});

// ---------------------------------------------------------------------------
console.log('reordering the pedal chain');
//
// Remove-then-insert is where the off-by-one lives: once the pedal is out, every
// index above it has shifted down by one. It only shows up dragging downwards.

function chainOf(...names) {
  // chainSlots is a getter reaching into manifest.preset.chain -- and it returns
  // that array rather than a copy, which is exactly why splicing it in place
  // persists. Building the manifest is therefore the honest fixture; assigning
  // chainSlots would test a shape the real code never has.
  const studio = makeStudio();
  studio.manifest = {
    name: 'p',
    source: {},
    preset: { name: 'p', chain: names.map((name) => ({ pedal: name, params: {} })) },
    performance: {},
    runtime: {},
  };
  studio.paint = () => {};
  return studio;
}
const order = (studio) => studio.chainSlots.map((slot) => slot.pedal).join(' ');

test('dragging the first card to the end', () => {
  const studio = chainOf('a', 'b', 'c', 'd');
  assert.equal(studio.moveSlot(0, 4), true);
  assert.equal(order(studio), 'b c d a');
});

test('dragging the last card to the front', () => {
  const studio = chainOf('a', 'b', 'c', 'd');
  assert.equal(studio.moveSlot(3, 0), true);
  assert.equal(order(studio), 'd a b c');
});

test('dragging one step down lands one step down, not two', () => {
  // The off-by-one: without the `before > from` correction this gives 'b c a d'.
  const studio = chainOf('a', 'b', 'c', 'd');
  studio.moveSlot(0, 2);
  assert.equal(order(studio), 'b a c d');
});

test('dragging one step up', () => {
  const studio = chainOf('a', 'b', 'c', 'd');
  studio.moveSlot(2, 1);
  assert.equal(order(studio), 'a c b d');
});

test('dropping a card on itself changes nothing', () => {
  const studio = chainOf('a', 'b', 'c');
  assert.equal(studio.moveSlot(1, 1), false);
  assert.equal(studio.moveSlot(1, 2), false);  // the gap just after itself
  assert.equal(order(studio), 'a b c');
});

test('a no-op drag does not dirty the piece', () => {
  // Otherwise picking a card up and putting it back asks you to save.
  const studio = chainOf('a', 'b', 'c');
  studio.dirty = false;
  studio.moveSlot(1, 1);
  assert.equal(studio.dirty, false);
});

test('a real move dirties the piece', () => {
  // Position feeds each pedal's randomness, so a reorder is an edit of the
  // sound, not a cosmetic tidy.
  const studio = chainOf('a', 'b', 'c');
  studio.dirty = false;
  studio.moveSlot(0, 2);
  assert.equal(studio.dirty, true);
});

test('an out-of-range source is refused rather than losing a pedal', () => {
  const studio = chainOf('a', 'b');
  assert.equal(studio.moveSlot(5, 0), false);
  assert.equal(studio.moveSlot(-1, 0), false);
  assert.equal(order(studio), 'a b');
});

test('a drag with no piece open is refused rather than throwing', () => {
  // chainSlots reaches into the manifest, so this used to throw rather than
  // return -- reachable by closing a piece mid-drag.
  const studio = makeStudio();
  studio.manifest = null;
  assert.equal(studio.moveSlot(0, 1), false);
});

test('every permutation survives a move', () => {
  // Cheap invariant: whatever the indices, nothing is lost or duplicated.
  for (let from = 0; from < 5; from += 1) {
    for (let before = 0; before <= 5; before += 1) {
      const studio = chainOf('a', 'b', 'c', 'd', 'e');
      studio.moveSlot(from, before);
      assert.equal(
        order(studio).split(' ').sort().join(''),
        'abcde',
        `from ${from} before ${before} lost or duplicated a pedal`,
      );
    }
  }
});

// ---------------------------------------------------------------------------
console.log('save and render are told apart');

test('the render button says it will save first when the piece is dirty', () => {
  const studio = makeStudio();
  studio.manifest = { name: 'p', source: {}, preset: {}, performance: {}, runtime: {} };
  studio.dirty = true;
  studio._paintHeader();
  assert.equal(dom.elements.get('studio-render').textContent, 'save + render');
});

test('and just render when it is clean', () => {
  const studio = makeStudio();
  studio.manifest = { name: 'p', source: {}, preset: {}, performance: {}, runtime: {} };
  studio.dirty = false;
  studio._paintHeader();
  assert.equal(dom.elements.get('studio-render').textContent, 'render');
});

test('save is only offered when there is something to save', () => {
  const studio = makeStudio();
  studio.manifest = { name: 'p', source: {}, preset: {}, performance: {}, runtime: {} };
  studio.dirty = false;
  studio._paintHeader();
  assert.equal(dom.elements.get('studio-save').disabled, true);
  studio.dirty = true;
  studio._paintHeader();
  assert.equal(dom.elements.get('studio-save').disabled, false);
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
