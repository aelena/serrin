/**
 * Headless checks for the runtime logic that does not need a DOM.
 *
 * Reader and Envelope are the two modules where a bug is silent -- a wrong frame
 * index or a wrong intensity does not throw, it just makes the piece subtly
 * wrong -- so they get tested against a real rendered stream rather than a
 * fixture. Run after a render:
 *
 *   node tests/test_runtime.mjs
 *
 * The engines themselves (audio.js, visual.js, panel.js) touch Web Audio, Canvas
 * and the DOM, so they are checked by node --check for syntax and by eye in the
 * browser. Putting a headless DOM in this project to unit-test a canvas painter
 * would cost more than it catches.
 */

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
// pathToFileURL, not a bare path: Windows rejects `c:\...` as an ESM specifier.
const mod = (rel) => pathToFileURL(path.join(root, rel)).href;

const { Reader, hash32, rng } = await import(mod('web/js/reader.js'));
const { Envelope, activeVoiceCount, voiceGates, envelopeFromArchetype, ReactiveIntensity } =
  await import(mod('web/js/envelope.js'));

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

// ---------------------------------------------------------------------------
const audioDoc = JSON.parse(await readFile(path.join(root, 'out/stream_audio.json'), 'utf8'));
const visualDoc = JSON.parse(await readFile(path.join(root, 'out/stream_visual.json'), 'utf8'));

console.log('reader');

test('reads the rendered pair without complaint', () => {
  const reader = new Reader(audioDoc, visualDoc);
  assert.ok(reader.length > 0, 'no frames');
  assert.ok(reader.voiceCount > 0 && reader.voiceCount <= 8, 'voice count outside 1..8');
  assert.equal(reader.names.length, reader.voiceCount);
});

test('frame() returns aligned audio and visual for every voice', () => {
  const reader = new Reader(audioDoc, visualDoc);
  const frame = reader.frame(17);
  assert.equal(frame.audio.length, reader.voiceCount);
  assert.equal(frame.visual.length, reader.voiceCount);
  for (let v = 0; v < reader.voiceCount; v += 1) {
    assert.equal(frame.audio[v].voice, v);
    assert.equal(frame.visual[v].voice, v);
    assert.ok(frame.audio[v].freq > 0, 'zero frequency');
    assert.ok(frame.visual[v].density >= 0 && frame.visual[v].density <= 1);
  }
});

test('the fork is a real fork, not the same number twice', () => {
  // The whole point of section 3.5. If audio pitch and visual position moved
  // together the mapping layer would have collapsed.
  const reader = new Reader(audioDoc, visualDoc);
  const samples = 400;
  const freqs = [];
  const ys = [];
  for (let i = 0; i < samples; i += 1) {
    const frame = reader.frame(i);
    freqs.push(frame.audio[0].freq);
    ys.push(frame.visual[0].y);
  }
  const correlation = pearson(freqs, ys);
  assert.ok(
    Math.abs(correlation) < 0.6,
    `audio pitch and visual y correlate at r=${correlation.toFixed(3)}; the fork has collapsed`,
  );
});

test("'once' policy ends the stream", () => {
  const reader = new Reader(audioDoc, visualDoc);
  reader.loopPolicy = 'once';
  assert.ok(reader.frame(reader.length - 1) !== null);
  assert.equal(reader.frame(reader.length), null);
});

test("'loop' wraps and keeps counting passes", () => {
  const reader = new Reader(audioDoc, visualDoc);
  reader.loopPolicy = 'loop';
  const first = reader.frame(3);
  const later = reader.frame(reader.length + 3);
  assert.equal(later.index, first.index);
  assert.equal(later.pass, 1);
  assert.equal(later.audio[0].freq, first.audio[0].freq, 'plain loop must not vary');
});

test("'pingpong' reverses on odd passes", () => {
  const reader = new Reader(audioDoc, visualDoc);
  reader.loopPolicy = 'pingpong';
  assert.equal(reader.frame(reader.length).index, reader.length - 1);
  assert.equal(reader.frame(reader.length + 1).index, reader.length - 2);
});

test("'vary' repeats without repeating identically (5.1.2)", () => {
  const reader = new Reader(audioDoc, visualDoc);
  reader.loopPolicy = 'vary';
  const first = reader.frame(5);
  const second = reader.frame(reader.length + 5);
  assert.notEqual(second.index, first.index, 'phase offset did not shift the read position');
  const variation = reader.variationFor(1);
  assert.ok(variation.phase >= 0 && variation.phase < reader.length);
});

test('variation is deterministic from the seed', () => {
  const a = new Reader(audioDoc, visualDoc);
  const b = new Reader(audioDoc, visualDoc);
  for (const pass of [1, 2, 7, 31]) {
    assert.deepEqual(a.variationFor(pass), b.variationFor(pass));
  }
});

test('hash32 and rng are stable and in range', () => {
  assert.equal(hash32('serrin'), hash32('serrin'));
  assert.notEqual(hash32('serrin'), hash32('serrin', 1));
  const next = rng(hash32('seed'));
  for (let i = 0; i < 200; i += 1) {
    const value = next();
    assert.ok(value >= 0 && value < 1, `rng out of range: ${value}`);
  }
});

// ---------------------------------------------------------------------------
console.log('envelope');

test('baked curve loads from the export', () => {
  const envelope = Envelope.fromExport(audioDoc.envelope);
  assert.ok(envelope.curve.length > 1, 'the export carried no curve');
  for (const t of [0, 0.25, 0.5, 0.75, 1]) {
    const v = envelope.at(t);
    assert.ok(v >= 0 && v <= 1, `intensity out of range at t=${t}: ${v}`);
  }
});

test('a stroke resamples onto an even grid', () => {
  const points = [
    [0, 0],
    [0.3, 0.9],
    [0.55, 0.4],
    [1, 1],
  ];
  const envelope = Envelope.fromPoints(points, 128);
  assert.equal(envelope.curve.length, 128);
  assert.ok(Math.abs(envelope.at(0) - 0) < 0.02);
  assert.ok(Math.abs(envelope.at(1) - 1) < 0.02);
  assert.ok(envelope.at(0.3) > envelope.at(0.55), 'the dip did not survive resampling');
});

test('a stroke round-trips through JSON identically', () => {
  // This is the answer to the open question in section 8: a live stroke is
  // recordable, so it can be replayed exactly.
  const original = Envelope.fromPoints([[0, 0.1], [0.5, 0.8], [1, 0.2]], 64);
  const restored = Envelope.fromExport(JSON.parse(JSON.stringify(original.toJSON())));
  for (let i = 0; i <= 20; i += 1) {
    assert.equal(restored.at(i / 20), original.at(i / 20));
  }
});

test('archetypes rise and fall where they say they do', () => {
  const climax = envelopeFromArchetype('climax');
  assert.ok(climax.at(0.7) > climax.at(0.1), 'climax does not build');
  assert.ok(climax.at(0.7) > climax.at(0.99), 'climax does not resolve');
});

test('voice activation stretches the low end (5.1.1)', () => {
  assert.equal(activeVoiceCount(0, 8), 1, 'silence should still leave one voice');
  assert.equal(activeVoiceCount(1, 8), 8, 'full intensity should use every voice');
  assert.ok(activeVoiceCount(0.5, 8) <= 4, 'half intensity should not be half the voices');
  let previous = 0;
  for (let i = 0; i <= 20; i += 1) {
    const count = activeVoiceCount(i / 20, 8);
    assert.ok(count >= previous, 'voice count must not fall as intensity rises');
    previous = count;
  }
});

test('voice gates follow the entry order', () => {
  const order = [3, 1, 0, 2];
  const gates = voiceGates(0.05, order, 1);
  assert.equal(gates[3], true, 'the first voice in the entry order must be the one that speaks');
  assert.equal(gates.filter(Boolean).length, 1);
  assert.deepEqual(voiceGates(1, order, 1), [true, true, true, true]);
});

test('reactive intensity rises on spikes and settles on calm (5.1-B)', () => {
  const reactive = new ReactiveIntensity(1);
  const calm = [{ density: 0.02, glitch: 0 }];
  const spike = [{ density: 1, glitch: 1 }];
  for (let i = 0; i < 50; i += 1) reactive.update(calm);
  const settled = reactive.value;
  for (let i = 0; i < 5; i += 1) reactive.update(spike);
  assert.ok(reactive.value > settled + 0.1, 'a spike did not raise intensity');
  const raised = reactive.value;
  for (let i = 0; i < 40; i += 1) reactive.update(calm);
  assert.ok(reactive.value < raised, 'intensity never comes back down');
});

// ---------------------------------------------------------------------------
console.log('cross-check with the python side');

test('python and js agree about voice activation', () => {
  // Both implementations use intensity**1.6; if one drifts, an offline preview
  // stops matching what the browser plays.
  const expected = JSON.parse(process.env.SERRIN_PY_GATES ?? 'null');
  if (!expected) {
    console.log('       (skipped: run via tests/run_all.py to compare against python)');
    return;
  }
  for (const [intensity, count] of expected) {
    assert.equal(activeVoiceCount(intensity, 8), count, `mismatch at intensity ${intensity}`);
  }
});

function pearson(a, b) {
  const n = a.length;
  const meanA = a.reduce((s, v) => s + v, 0) / n;
  const meanB = b.reduce((s, v) => s + v, 0) / n;
  let num = 0;
  let devA = 0;
  let devB = 0;
  for (let i = 0; i < n; i += 1) {
    const x = a[i] - meanA;
    const y = b[i] - meanB;
    num += x * y;
    devA += x * x;
    devB += y * y;
  }
  const denom = Math.sqrt(devA * devB);
  return denom === 0 ? 0 : num / denom;
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
