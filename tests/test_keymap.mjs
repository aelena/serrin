/**
 * Headless checks for key maps and the `notes` mode.
 *
 * Two properties are worth guarding because both fail silently:
 *
 * * **Degrees follow the scale.** A map bound to degrees has to stay in key when
 *   the piece's scale changes; a map bound to pitches would go quietly out of
 *   tune, and nothing would raise.
 * * **`notes` claims only bound keys.** Claiming everything would swallow the
 *   piece's own shortcuts, so a nine-key map would make thirty other keys dead.
 *
 * There is also a cross-check that the browser's default map matches the CLI's,
 * since both exist and a divergence would mean "the same piece" plays two
 * different layouts.
 *
 *   node tests/test_keymap.mjs
 */

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const mod = (rel) => pathToFileURL(path.join(root, rel)).href;

const {
  ALL_KEYS,
  KEY_ROWS,
  PIANO_HIGH,
  PIANO_LOW,
  chromaticKeymap,
  defaultKeymap,
  degreeToMidi,
  describeBinding,
  fallbackLabel,
  noteName,
  resolveBinding,
} = await import(mod('web/js/keymap.js'));
const { KeyboardEngine, MODES } = await import(mod('web/js/keyboard.js'));
const { Reader } = await import(mod('web/js/reader.js'));

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

const PENTATONIC = { offsets: [0, 3, 5, 7, 10], span: 12, root: 45, name: 'pentatonic_minor' };
const MAJOR = { offsets: [0, 2, 4, 5, 7, 9, 11], span: 12, root: 48, name: 'ionian' };

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

function makeEngine(overrides = {}) {
  const reader = new Reader(audioDoc, visualDoc);
  const audio = fakeAudio();
  const keyboard = new KeyboardEngine(audio, reader);
  keyboard.enabled = true;
  Object.assign(keyboard, overrides);
  return { keyboard, audio };
}

const keyEvent = (code, key = 'x', extra = {}) => ({
  code,
  key,
  repeat: false,
  preventDefault() {},
  ...extra,
});

// ---------------------------------------------------------------------------
console.log('degrees and bindings');

test('degree zero is the root', () => {
  assert.equal(degreeToMidi(0, PENTATONIC), 45);
  assert.equal(noteName(45), 'A2');
});

test('degrees walk the scale and wrap into octaves', () => {
  // Degree 5 of a 5-note scale is the root an octave up, not an error.
  const notes = [0, 1, 2, 3, 4, 5].map((d) => degreeToMidi(d, PENTATONIC));
  assert.deepEqual(notes, [45, 48, 50, 52, 55, 57]);
  assert.equal(notes[5] - notes[0], 12);
});

test('negative degrees go down rather than crashing', () => {
  assert.equal(degreeToMidi(-1, PENTATONIC), 45 - 2); // the degree below the root
  assert.ok(degreeToMidi(-6, PENTATONIC) < 45);
});

test('every degree lands in the scale', () => {
  for (const scale of [PENTATONIC, MAJOR]) {
    const allowed = new Set(scale.offsets);
    for (let degree = -10; degree < 20; degree += 1) {
      const midi = degreeToMidi(degree, scale);
      const inScale = (((midi - scale.root) % scale.span) + scale.span) % scale.span;
      assert.ok(allowed.has(inScale), `degree ${degree} left the scale (${midi})`);
    }
  }
});

test('the same degree follows the scale when the scale changes', () => {
  // The whole reason bindings are degrees: a map must not go out of key when
  // the chain or the mapping moves the piece into another scale.
  const binding = { kind: 'degree', degree: 2, octave: 0 };
  const a = resolveBinding(binding, PENTATONIC).midi;
  const b = resolveBinding(binding, MAJOR).midi;
  assert.notEqual(a, b);
  for (const [scale, midi] of [[PENTATONIC, a], [MAJOR, b]]) {
    const inScale = (((midi - scale.root) % scale.span) + scale.span) % scale.span;
    assert.ok(new Set(scale.offsets).has(inScale));
  }
});

test('everything stays inside the piano', () => {
  for (let degree = -60; degree < 60; degree += 1) {
    const midi = degreeToMidi(degree, PENTATONIC, 3);
    assert.ok(midi >= PIANO_LOW && midi <= PIANO_HIGH, `degree ${degree} -> ${midi}`);
  }
  assert.equal(resolveBinding({ kind: 'note', midi: 9999 }, PENTATONIC).midi, PIANO_HIGH);
});

test('an octave offset shifts by a full span', () => {
  const binding = { kind: 'degree', degree: 1, octave: 0 };
  const base = resolveBinding(binding, PENTATONIC).midi;
  assert.equal(resolveBinding(binding, PENTATONIC, 1).midi, base + 12);
  assert.equal(resolveBinding(binding, PENTATONIC, -1).midi, base - 12);
});

test('an absolute note binding is honoured', () => {
  assert.equal(resolveBinding({ kind: 'note', midi: 60 }, PENTATONIC).midi, 60);
});

test('sample and pattern bindings resolve to something that is not a note', () => {
  // They are triggered rather than pitched, and nothing can play them yet -- so
  // they must not quietly fall back to a pitch and sound finished.
  assert.deepEqual(resolveBinding({ kind: 'sample', sample: 'kick' }, PENTATONIC), {
    kind: 'sample',
    sample: 'kick',
  });
  assert.deepEqual(resolveBinding({ kind: 'pattern', pattern: 'beat' }, PENTATONIC), {
    kind: 'pattern',
    pattern: 'beat',
  });
});

test('an incomplete or unknown binding resolves to nothing', () => {
  for (const binding of [
    null,
    {},
    { kind: 'sample' },
    { kind: 'pattern' },
    { kind: 'note' },
    { kind: 'telepathy' },
  ]) {
    assert.equal(resolveBinding(binding, PENTATONIC), null, JSON.stringify(binding));
  }
});

test('descriptions name the note and the degree', () => {
  const text = describeBinding({ kind: 'degree', degree: 2 }, PENTATONIC);
  assert.match(text, /deg 2/);
  assert.match(text, /^[A-G]#?\d/);
  assert.equal(describeBinding(null, PENTATONIC), '');
});

// ---------------------------------------------------------------------------
console.log('the layout');

test('the rows cover four physical rows with no duplicates', () => {
  assert.equal(KEY_ROWS.length, 4);
  assert.equal(new Set(ALL_KEYS).size, ALL_KEYS.length);
});

test('positions are stored, never characters', () => {
  for (const code of ALL_KEYS) {
    assert.ok(/^(Key[A-Z]|Digit\d|Semicolon|Comma|Period|Slash)$/.test(code), code);
  }
});

test('fallback labels are readable', () => {
  assert.equal(fallbackLabel('KeyA'), 'A');
  assert.equal(fallbackLabel('Digit7'), '7');
  assert.equal(fallbackLabel('Semicolon'), ';');
});

test('the default map covers two home rows', () => {
  const keymap = defaultKeymap();
  assert.equal(Object.keys(keymap).length, 19);
  assert.ok(keymap.KeyA && keymap.KeyQ);
  assert.equal(keymap.KeyA.degree, 0);
  assert.equal(keymap.KeyA.octave, 0);
  // The row above sits an octave up, so both hands have range.
  assert.equal(keymap.KeyQ.octave, 1);
});

test('the chromatic map rises with the keys', () => {
  const keymap = chromaticKeymap();
  assert.equal(Object.keys(keymap).length, ALL_KEYS.length);
  // Bottom row lowest, like a piano.
  assert.ok(keymap.KeyZ.degree < keymap.KeyA.degree);
  assert.ok(keymap.KeyA.degree < keymap.KeyQ.degree);
});

test('the browser default map matches the CLI default map', async () => {
  // Both exist, and a divergence would mean "the same piece" plays two
  // different layouts depending on where the map was made.
  const python = await readFile(path.join(root, 'serrin/piece.py'), 'utf8');
  const rows = [...python.matchAll(/\(\s*((?:"Key[A-Z]",?\s*)+)\)/g)].map((match) =>
    [...match[1].matchAll(/"(Key[A-Z])"/g)].map((m) => m[1]),
  );
  assert.ok(rows.length >= 2, 'could not find the CLI rows');
  const keymap = defaultKeymap();
  for (const row of rows) {
    for (const code of row) {
      assert.ok(keymap[code], `${code} is in the CLI map but not the browser map`);
    }
  }
  assert.equal(Object.keys(keymap).length, rows[0].length + rows[1].length);
});

// ---------------------------------------------------------------------------
console.log('the notes mode');

test('notes is a working mode now', () => {
  assert.equal(MODES.notes.ready, true);
  const { keyboard } = makeEngine();
  assert.equal(keyboard.setMode('notes'), true);
});

test('a map is loaded separately from the render', () => {
  // A map belongs to the piece, a scale to the render: switching render must not
  // discard the layout the author built.
  const { keyboard } = makeEngine();
  assert.equal(keyboard.setKeymap(defaultKeymap()), 19);
  keyboard.adopt(new Reader(audioDoc, visualDoc));
  assert.equal(Object.keys(keyboard.keymap).length, 19, 'adopt() wiped the map');
});

test('a bound key plays its note', () => {
  const { keyboard, audio } = makeEngine({ mode: 'notes' });
  keyboard.setKeymap({ KeyA: { kind: 'degree', degree: 0, octave: 0 } });
  const played = keyboard.press(keyEvent('KeyA', 'a'));
  assert.ok(played, 'nothing played');
  assert.equal(played.midi, degreeToMidi(0, keyboard.scale));
  assert.equal(played.code, 'KeyA');
  assert.deepEqual(played.binding, { kind: 'degree', degree: 0, octave: 0 });
  assert.equal(audio.played.length, 1);
});

test('an unbound key is left for the piece', () => {
  // The difference from `random`: a nine-key map must not make thirty other
  // keys dead.
  const { keyboard } = makeEngine({ mode: 'notes' });
  keyboard.setKeymap({ KeyA: { kind: 'degree', degree: 0 } });
  assert.ok(keyboard.claims(keyEvent('KeyA', 'a')));
  assert.ok(!keyboard.claims(keyEvent('KeyZ', 'z')));
  assert.equal(keyboard.press(keyEvent('KeyZ', 'z')), null);
});

test('random still claims everything', () => {
  const { keyboard } = makeEngine({ mode: 'random' });
  keyboard.setKeymap({});
  assert.ok(keyboard.claims(keyEvent('KeyZ', 'z')));
});

test('the map is played in order, deterministically', () => {
  // Unlike random, a mapped key is the same note every time -- that is the point.
  const { keyboard } = makeEngine({ mode: 'notes' });
  keyboard.setKeymap(defaultKeymap());
  const first = keyboard.press(keyEvent('KeyS', 's')).midi;
  keyboard.release(keyEvent('KeyS', 's'));
  const second = keyboard.press(keyEvent('KeyS', 's')).midi;
  assert.equal(first, second);
});

test('an octave shift moves the whole map', () => {
  const { keyboard } = makeEngine({ mode: 'notes' });
  keyboard.setKeymap(defaultKeymap());
  const base = keyboard.press(keyEvent('KeyA', 'a')).midi;
  keyboard.release(keyEvent('KeyA', 'a'));
  assert.equal(keyboard.shiftOctave(1), 1);
  assert.equal(keyboard.press(keyEvent('KeyA', 'a')).midi, base + 12);
});

test('the octave shift is clamped', () => {
  const { keyboard } = makeEngine({ mode: 'notes' });
  for (let i = 0; i < 10; i += 1) keyboard.shiftOctave(1);
  assert.equal(keyboard.octaveOffset, 3);
  for (let i = 0; i < 20; i += 1) keyboard.shiftOctave(-1);
  assert.equal(keyboard.octaveOffset, -3);
});

test('a sample binding does not sneak out as a note', () => {
  const { keyboard, audio } = makeEngine({ mode: 'notes' });
  keyboard.setKeymap({ KeyA: { kind: 'sample', sample: 'kick' } });
  assert.ok(keyboard.claims(keyEvent('KeyA', 'a')), 'a bound key should be claimed');
  assert.equal(keyboard.press(keyEvent('KeyA', 'a')), null);
  assert.equal(audio.played.length, 0, 'a sample binding played a pitch');
});

test('describe() says when there is no map to play', () => {
  const { keyboard } = makeEngine({ mode: 'notes' });
  keyboard.setKeymap({});
  assert.match(keyboard.describe(), /no key map/);
  keyboard.setKeymap(defaultKeymap());
  assert.match(keyboard.describe(), /19 keys mapped/);
});

test('describeKey reports what a position plays', () => {
  const { keyboard } = makeEngine({ mode: 'notes' });
  keyboard.setKeymap(defaultKeymap());
  assert.match(keyboard.describeKey('KeyA'), /deg 0/);
  assert.equal(keyboard.describeKey('KeyZ'), '');
});

test('held keys still do not machine-gun in notes mode', () => {
  const { keyboard, audio } = makeEngine({ mode: 'notes' });
  keyboard.setKeymap(defaultKeymap());
  keyboard.press(keyEvent('KeyA', 'a'));
  keyboard.press(keyEvent('KeyA', 'a', { repeat: true }));
  keyboard.press(keyEvent('KeyA', 'a'));
  assert.equal(audio.played.length, 1);
});

test('modifiers and reserved keys are still never claimed', () => {
  const { keyboard } = makeEngine({ mode: 'notes' });
  keyboard.setKeymap(defaultKeymap());
  assert.ok(!keyboard.claims(keyEvent('KeyA', 'a', { ctrlKey: true })));
  assert.ok(!keyboard.claims(keyEvent('Space', ' ')));
  assert.ok(!keyboard.claims(keyEvent('ArrowUp', 'ArrowUp')));
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
