/**
 * Headless checks for the keyboard.
 *
 * Worth testing properly despite being a small feature, because two of its
 * failure modes are silent: a note pool that quietly includes out-of-scale
 * notes just sounds slightly wrong, and a `claims()` that is too greedy eats
 * the transport shortcuts without any error. Both are easy to introduce while
 * adding the note-map and beat modes on top.
 *
 *   node tests/test_keyboard.mjs
 */

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const mod = (rel) => pathToFileURL(path.join(root, rel)).href;

const { Reader } = await import(mod('web/js/reader.js'));
const { KeyboardEngine, MODES, REGISTERS, PIANO_LOW, PIANO_HIGH, noteName } = await import(
  mod('web/js/keyboard.js')
);

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

const audioDoc = JSON.parse(await readFile(path.join(root, 'out/stream_audio.json'), 'utf8'));
const visualDoc = JSON.parse(await readFile(path.join(root, 'out/stream_visual.json'), 'utf8'));

/** Minimal stand-in for the audio engine: records what it was asked to play. */
function fakeAudio() {
  return {
    started: true,
    played: [],
    playNote(midi, velocity, options) {
      this.played.push({ midi, velocity, options });
      return { midi };
    },
    setKeyboardCrushed() {},
  };
}

function makeKeyboard(overrides = {}) {
  const reader = new Reader(audioDoc, visualDoc);
  const audio = fakeAudio();
  const keyboard = new KeyboardEngine(audio, reader);
  keyboard.enabled = true;
  Object.assign(keyboard, overrides);
  if (overrides.register) keyboard.setRegister(overrides.register);
  return { keyboard, audio, reader };
}

/** A keydown event, near enough for the engine's purposes. */
function keyEvent(key, extra = {}) {
  return { key, code: `Key_${key}`, repeat: false, preventDefault() {}, ...extra };
}

// ---------------------------------------------------------------------------
console.log('scale and pool');

test('the piece exports a scale for the keyboard to use', () => {
  assert.ok(audioDoc.meta.scale, 'no scale block in the export');
  const { offsets, span, root: scaleRoot, source } = audioDoc.meta.scale;
  assert.ok(Array.isArray(offsets) && offsets.length > 0);
  assert.equal(span, 12);
  assert.ok(Number.isInteger(scaleRoot));
  assert.ok(['mapping', 'mod_reduce', 'default'].includes(source), `odd source: ${source}`);
});

test('every note in the pool belongs to the scale', () => {
  const { keyboard } = makeKeyboard();
  const { offsets, span, root: scaleRoot } = keyboard.scale;
  const allowed = new Set(offsets);
  assert.ok(keyboard.pool.length > 0, 'empty pool');
  for (const midi of keyboard.pool) {
    const degree = (((midi - scaleRoot) % span) + span) % span;
    assert.ok(allowed.has(degree), `${noteName(midi)} (${midi}) is not in the scale`);
  }
});

test('the pool is sorted and free of duplicates', () => {
  const { keyboard } = makeKeyboard();
  assert.deepEqual(keyboard.pool, [...keyboard.pool].sort((a, b) => a - b));
  assert.equal(new Set(keyboard.pool).size, keyboard.pool.length);
});

test('every register stays inside the piano', () => {
  for (const name of Object.keys(REGISTERS)) {
    const { keyboard } = makeKeyboard({ register: name });
    const bounds = keyboard.registerBounds();
    assert.ok(bounds.low >= PIANO_LOW, `${name} goes below A0`);
    assert.ok(bounds.high <= PIANO_HIGH, `${name} goes above C8`);
    assert.ok(bounds.low <= bounds.high, `${name} has inverted bounds`);
    for (const midi of keyboard.pool) {
      assert.ok(midi >= PIANO_LOW && midi <= PIANO_HIGH, `${name} left the piano`);
    }
  }
});

test('registers actually differ in pitch', () => {
  const bass = makeKeyboard({ register: 'bass' }).keyboard;
  const treble = makeKeyboard({ register: 'treble' }).keyboard;
  assert.ok(
    Math.max(...bass.pool) < Math.max(...treble.pool),
    'bass and treble cover the same ground',
  );
});

test("the 'piece' register follows the piece's own note range", () => {
  const { keyboard } = makeKeyboard({ register: 'piece' });
  const bounds = keyboard.registerBounds();
  assert.equal(bounds.low, keyboard.scale.note_low);
  assert.equal(bounds.high, keyboard.scale.note_high);
});

test('a render with no scale block still plays', () => {
  // Older exports predate meta.scale; the instrument must not need re-rendering.
  const reader = new Reader({ ...audioDoc, meta: { ...audioDoc.meta, scale: undefined } }, visualDoc);
  const keyboard = new KeyboardEngine(fakeAudio(), reader);
  assert.equal(keyboard.scale.source, 'fallback');
  assert.ok(keyboard.pool.length > 0);
});

// ---------------------------------------------------------------------------
console.log('which keys it claims');

test('claims letters and digits when live', () => {
  const { keyboard } = makeKeyboard();
  for (const key of ['a', 'q', 'z', '4', ';', '/']) {
    assert.ok(keyboard.claims(keyEvent(key)), `did not claim ${key}`);
  }
});

test('never claims the keys the piece reserves', () => {
  const { keyboard } = makeKeyboard();
  for (const key of [' ', 'Escape', 'Tab', 'F11']) {
    assert.ok(!keyboard.claims(keyEvent(key)), `claimed reserved key ${key}`);
  }
});

test('never claims modifier combinations', () => {
  const { keyboard } = makeKeyboard();
  for (const flag of ['ctrlKey', 'metaKey', 'altKey']) {
    assert.ok(!keyboard.claims(keyEvent('r', { [flag]: true })), `claimed ${flag}+r`);
  }
});

test('never claims arrows or function keys', () => {
  const { keyboard } = makeKeyboard();
  for (const key of ['ArrowUp', 'Enter', 'Backspace', 'F5', 'Shift']) {
    assert.ok(!keyboard.claims(keyEvent(key)), `claimed ${key}`);
  }
});

test('claims nothing while switched off', () => {
  const { keyboard } = makeKeyboard();
  keyboard.enabled = false;
  assert.ok(!keyboard.claims(keyEvent('a')));
});

test('claims nothing in a mode that is not implemented', () => {
  // `notes` graduated; these two have not. The assertion is about the state of
  // the world, so it moves as modes land rather than being loosened.
  const { keyboard } = makeKeyboard();
  for (const mode of ['samples', 'beats']) {
    assert.equal(keyboard.setMode(mode), false, `${mode} reports itself ready`);
    assert.ok(!keyboard.claims(keyEvent('a')), `${mode} claimed a key anyway`);
    assert.equal(keyboard.press(keyEvent('a')), null);
  }
  assert.equal(keyboard.setMode('random'), true);
});

test('the mode registry says which modes work', () => {
  const ready = Object.entries(MODES)
    .filter(([, mode]) => mode.ready)
    .map(([name]) => name);
  assert.deepEqual(ready.sort(), ['notes', 'random']);
  // The pending ones stay listed and stay disabled: the shape is visible and
  // nothing pretends.
  assert.equal(MODES.samples.ready, false);
  assert.equal(MODES.beats.ready, false);
});

// ---------------------------------------------------------------------------
console.log('playing');

test('a press plays one note from the pool', () => {
  const { keyboard, audio } = makeKeyboard();
  const played = keyboard.press(keyEvent('a'));
  assert.ok(played, 'nothing played');
  assert.ok(keyboard.pool.includes(played.midi), 'played outside the pool');
  assert.equal(audio.played.length, 1);
  assert.equal(audio.played[0].midi, played.midi);
  assert.equal(played.name, noteName(played.midi));
});

test('held keys do not machine-gun', () => {
  const { keyboard, audio } = makeKeyboard();
  keyboard.press(keyEvent('a'));
  keyboard.press(keyEvent('a', { repeat: true }));
  keyboard.press(keyEvent('a')); // same code, still held
  assert.equal(audio.played.length, 1, 'a held key fired more than once');
  keyboard.release(keyEvent('a'));
  keyboard.press(keyEvent('a'));
  assert.equal(audio.played.length, 2, 'a re-press after release did not fire');
});

test('different keys are independently held', () => {
  const { keyboard, audio } = makeKeyboard();
  keyboard.press(keyEvent('a'));
  keyboard.press(keyEvent('s'));
  keyboard.press(keyEvent('d'));
  assert.equal(audio.played.length, 3, 'polyphony is broken');
});

test('the note sequence is deterministic from the seed', () => {
  // The instrument is reproducible even though the playing is not: the tenth
  // press of a given piece is always the same note.
  const first = makeKeyboard().keyboard;
  const second = makeKeyboard().keyboard;
  const notesA = [];
  const notesB = [];
  for (let i = 0; i < 24; i += 1) {
    notesA.push(first.press(keyEvent('a', { code: `k${i}` })).midi);
    notesB.push(second.press(keyEvent('z', { code: `k${i}` })).midi);
  }
  assert.deepEqual(notesA, notesB, 'the same press index gave different notes');
  assert.ok(new Set(notesA).size > 3, 'the draw is barely moving');
});

test('never repeats the same note twice in a row', () => {
  const { keyboard } = makeKeyboard();
  let previous = null;
  for (let i = 0; i < 200; i += 1) {
    const played = keyboard.press(keyEvent('a', { code: `k${i}` }));
    assert.notEqual(played.midi, previous, `repeated ${played.name} at press ${i}`);
    previous = played.midi;
  }
});

test('level and timbre reach the audio engine', () => {
  const { keyboard, audio } = makeKeyboard();
  keyboard.level = 0.31;
  keyboard.waveform = 'triangle';
  keyboard.press(keyEvent('a'));
  assert.equal(audio.played[0].velocity, 0.31);
  assert.equal(audio.played[0].options.waveform, 'triangle');
  assert.ok(audio.played[0].options.decay < 0.5, 'not a bleep -- too long');
});

test('onNote fires for visual feedback', () => {
  const { keyboard } = makeKeyboard();
  const seen = [];
  keyboard.onNote = (played) => seen.push(played);
  keyboard.press(keyEvent('a'));
  assert.equal(seen.length, 1);
  assert.equal(typeof seen[0].name, 'string');
});

test('panic forgets everything held', () => {
  const { keyboard, audio } = makeKeyboard();
  keyboard.press(keyEvent('a'));
  keyboard.panic();
  keyboard.press(keyEvent('a'));
  assert.equal(audio.played.length, 2, 'panic did not release the held key');
});

test('switching piece keeps the settings and takes the new scale', () => {
  const { keyboard } = makeKeyboard({ register: 'treble', level: 0.4 });
  const other = new Reader(
    { ...audioDoc, meta: { ...audioDoc.meta, scale: { ...audioDoc.meta.scale, offsets: [0, 4, 7], root: 36, span: 12, name: 'triad', source: 'mapping', note_low: 36, note_high: 72 } } },
    visualDoc,
  );
  keyboard.adopt(other);
  assert.equal(keyboard.level, 0.4, 'lost the level');
  assert.equal(keyboard.register, 'treble', 'lost the register');
  assert.equal(keyboard.scale.name, 'triad');
  for (const midi of keyboard.pool) {
    assert.ok([0, 4, 7].includes((((midi - 36) % 12) + 12) % 12), 'stale pool');
  }
});

// ---------------------------------------------------------------------------
console.log('note names');

test('MIDI note names use the usual convention', () => {
  assert.equal(noteName(60), 'C4');
  assert.equal(noteName(69), 'A4');
  assert.equal(noteName(21), 'A0');
  assert.equal(noteName(108), 'C8');
});

test('describe() says where the scale came from', () => {
  const { keyboard } = makeKeyboard();
  const text = keyboard.describe();
  assert.ok(text.includes(keyboard.scale.name), 'no scale name');
  assert.ok(/\d+ notes/.test(text), 'no pool size');
  if (keyboard.scale.source === 'default') {
    assert.ok(text.includes('declares none'), 'silently pretended the piece has a key');
  }
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
