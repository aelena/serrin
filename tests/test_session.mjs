/**
 * Headless checks for session capture and restore.
 *
 * The two failure modes worth guarding are both silent. A field that `capture`
 * forgets is a setting that quietly does not come back; and an `apply` that
 * pretends to restore the render layer would leave the author convinced a chain
 * edit took hold when nothing changed. So the tests check round-trip
 * completeness, and check that a mismatched render is *reported* rather than
 * swallowed.
 *
 *   node tests/test_session.mjs
 */

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const mod = (rel) => pathToFileURL(path.join(root, rel)).href;

const { Reader } = await import(mod('web/js/reader.js'));
const { Tempo } = await import(mod('web/js/tempo.js'));
const { Envelope, envelopeFromArchetype } = await import(mod('web/js/envelope.js'));
const { KeyboardEngine } = await import(mod('web/js/keyboard.js'));
const { FORMAT, apply, capture } = await import(mod('web/js/session.js'));

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

/**
 * A stand-in for the running app. Mirrors the real object's shape closely
 * enough that capture/apply exercise the same paths; the engines are replaced
 * because Web Audio and Canvas do not exist here.
 */
function makeApp() {
  const reader = new Reader(audioDoc, visualDoc);
  const audio = {
    started: true,
    masterGain: 0.55,
    crushBits: 8,
    delayMix: 0.22,
    delayNote: '1/8.',
    filterCutoff: 9000,
    balance: 0.5,
    keyboardCrushed: false,
    mutes: new Set(),
    soloVoice: null,
    played: [],
    playNote(midi) {
      this.played.push(midi);
      return { midi };
    },
    setMaster(v) {
      this.masterGain = v;
    },
    setCrush(v) {
      this.crushBits = v;
    },
    setDelayMix(v) {
      this.delayMix = v;
    },
    setDelayNote(v) {
      this.delayNote = v;
    },
    setCutoff(v) {
      this.filterCutoff = v;
    },
    setKeyboardCrushed(v) {
      this.keyboardCrushed = v;
    },
    syncDelay() {},
  };
  const visual = {
    showGlyphs: true,
    showBars: true,
    showBanding: true,
    corruption: 1,
    invert: false,
    hidden: new Set(),
    showKeys: false,
    balance: 0.5,
  };
  const app = {
    reader,
    audio,
    visual,
    transport: { targetFrames: reader.length, counter: 0, retime() {} },
    envelope: Envelope.fromExport(audioDoc.envelope),
    entryStrategy: 'variance',
    presetId: 'gritty_01',
    presets: [{ id: 'gritty_01', audio: '../out/stream_audio.json', visual: '../out/stream_visual.json' }],
    setEnvelope(envelope) {
      this.envelope = envelope;
    },
    setEntryStrategy(strategy) {
      this.entryStrategy = strategy;
    },
    setTempo(tempo) {
      this.reader.tempo = tempo;
      this.transport.retime();
    },
  };
  app.keyboard = new KeyboardEngine(audio, reader);
  return app;
}

// ---------------------------------------------------------------------------
console.log('capture');

test('captures the declared format and the render identity', () => {
  const session = capture(makeApp());
  assert.equal(session.format, FORMAT);
  assert.equal(session.label, audioDoc.meta.label);
  assert.equal(session.fingerprint, audioDoc.meta.fingerprint);
  assert.ok(session.saved_at, 'no timestamp');
});

test('the source block can reconstruct the ingest', () => {
  const session = capture(makeApp());
  assert.equal(session.source.path, audioDoc.meta.ingest.source);
  assert.deepEqual(session.source.columns, audioDoc.meta.ingest.columns);
  assert.equal(session.source.granularity, audioDoc.meta.ingest.granularity);
  assert.equal(session.source.bit_depth, audioDoc.meta.bit_depth);
  assert.ok(session.source.tempo.bpm > 0);
});

test('the source records the LIVE tempo, not the rendered one', () => {
  // If the author moved the BPM slider, that is the grid they mean to keep.
  const app = makeApp();
  app.setTempo(new Tempo({ bpm: 77, subdivision: 8, swing: 0.4 }));
  const session = capture(app);
  assert.equal(session.source.tempo.bpm, 77);
  assert.equal(session.source.tempo.swing, 0.4);
  assert.notEqual(session.source.tempo.bpm, audioDoc.meta.tempo.bpm);
});

test('the preset block is a preset the CLI would accept', () => {
  const session = capture(makeApp());
  const preset = session.preset;
  assert.ok(Array.isArray(preset.chain), 'no chain array');
  assert.ok(preset.name, 'no name');
  assert.ok(preset.ingest.tempo, 'no tempo in ingest');
  // rate and tempo together would be a conflict for the loader to resolve.
  assert.equal(preset.ingest.rate, undefined, 'left a stale rate beside the tempo');
  assert.equal(preset.piece.mode, session.runtime.transport.mode);
});

test('captures every runtime group', () => {
  const session = capture(makeApp());
  for (const group of ['transport', 'audio', 'visual', 'keyboard', 'voices', 'envelope']) {
    assert.ok(session.runtime[group] !== undefined, `missing runtime.${group}`);
  }
});

test('notes are carried', () => {
  assert.equal(capture(makeApp(), 'more crush').notes, 'more crush');
});

// ---------------------------------------------------------------------------
console.log('apply');

test('restores the runtime layer', () => {
  const source = makeApp();
  source.audio.setMaster(0.21);
  source.audio.setCrush(3);
  source.audio.setCutoff(2200);
  source.audio.setDelayNote('1/16');
  source.audio.mutes = new Set([1, 4]);
  source.audio.soloVoice = 2;
  source.visual.invert = true;
  source.visual.corruption = 1.75;
  source.visual.hidden = new Set([0]);
  source.keyboard.enabled = true;
  source.keyboard.setRegister('bass');
  source.keyboard.level = 0.33;
  source.keyboard.waveform = 'triangle';
  source.reader.speed = 1.5;
  source.reader.loopPolicy = 'pingpong';
  source.setEntryStrategy('sparse');

  const session = capture(source);
  const target = makeApp();
  const report = apply(target, session);

  assert.equal(target.audio.masterGain, 0.21);
  assert.equal(target.audio.crushBits, 3);
  assert.equal(target.audio.filterCutoff, 2200);
  assert.equal(target.audio.delayNote, '1/16');
  assert.deepEqual([...target.audio.mutes].sort(), [1, 4]);
  assert.equal(target.audio.soloVoice, 2);
  assert.equal(target.visual.invert, true);
  assert.equal(target.visual.corruption, 1.75);
  assert.deepEqual([...target.visual.hidden], [0]);
  assert.equal(target.keyboard.enabled, true);
  assert.equal(target.keyboard.register, 'bass');
  assert.equal(target.keyboard.level, 0.33);
  assert.equal(target.keyboard.waveform, 'triangle');
  assert.equal(target.reader.speed, 1.5);
  assert.equal(target.reader.loopPolicy, 'pingpong');
  assert.equal(target.entryStrategy, 'sparse');
  assert.ok(report.applied.includes('audio'));
  assert.equal(report.warnings.length, 0, `unexpected warnings: ${report.warnings}`);
});

test('a hand-drawn envelope survives the round trip', () => {
  // The concrete answer to section 8: a live stroke is recordable and replays.
  const source = makeApp();
  const drawn = Envelope.fromPoints(
    [
      [0, 0.05],
      [0.35, 0.95],
      [0.6, 0.3],
      [1, 0.7],
    ],
    256,
    { kind: 'stroke' },
  );
  source.setEnvelope(drawn);

  const target = makeApp();
  apply(target, capture(source));
  for (let i = 0; i <= 20; i += 1) {
    const t = i / 20;
    assert.ok(
      Math.abs(target.envelope.at(t) - drawn.at(t)) < 1e-9,
      `envelope differs at t=${t}`,
    );
  }
});

test('an archetype envelope survives too', () => {
  const source = makeApp();
  source.setEnvelope(envelopeFromArchetype('climax'));
  const target = makeApp();
  apply(target, capture(source));
  assert.ok(target.envelope.at(0.7) > target.envelope.at(0.05));
});

test('tempo is restored and the transport re-anchored', () => {
  const source = makeApp();
  source.setTempo(new Tempo({ bpm: 88, subdivision: 8, swing: 0.5, beatsPerBar: 3 }));

  const target = makeApp();
  let retimed = 0;
  target.transport.retime = () => {
    retimed += 1;
  };
  apply(target, capture(source));

  assert.equal(target.reader.tempo.bpm, 88);
  assert.equal(target.reader.tempo.subdivision, 8);
  assert.equal(target.reader.tempo.swing, 0.5);
  assert.equal(target.reader.tempo.beatsPerBar, 3);
  assert.ok(retimed > 0, 'the transport was never re-anchored');
});

test('a session from another render warns and skips the render layer', () => {
  // The honest path: load the runtime half, say plainly that the chain did not
  // re-apply, and name the command that would actually do it.
  const session = capture(makeApp());
  session.fingerprint = 'ffffffffffffffff';
  const target = makeApp();
  const report = apply(target, session);

  assert.equal(report.warnings.length, 1);
  assert.match(report.warnings[0], /render --session/);
  assert.ok(report.skipped.some((item) => item.includes('preset')));
  // The runtime half still landed, which is the point of not throwing.
  assert.ok(report.applied.includes('audio'));
});

test('a foreign format is refused outright', () => {
  const session = capture(makeApp());
  session.format = 'serrin-session/99';
  assert.throws(() => apply(makeApp(), session), /serrin-session\/99/);
});

test('mutes and hidden voices out of range are dropped', () => {
  // A session saved against an eight-voice piece, loaded onto a smaller one.
  const session = capture(makeApp());
  session.runtime.audio.mutes = [0, 99];
  session.runtime.visual.hidden = [1, 250];
  const target = makeApp();
  apply(target, session);
  assert.deepEqual([...target.audio.mutes], [0]);
  assert.deepEqual([...target.visual.hidden], [1]);
});

test('a session with an empty runtime block applies cleanly', () => {
  const report = apply(makeApp(), { format: FORMAT, source: {}, preset: {}, runtime: {} });
  assert.equal(report.warnings.length, 0);
  assert.deepEqual(report.applied, []);
});

test('capture is stable across a round trip', () => {
  // capture -> apply -> capture must be a fixed point, or repeatedly saving and
  // loading would slowly drift the settings.
  const first = capture(makeApp());
  const target = makeApp();
  apply(target, first);
  const second = capture(target);

  for (const group of ['audio', 'visual', 'keyboard', 'voices']) {
    assert.deepEqual(second.runtime[group], first.runtime[group], `${group} drifted`);
  }
  assert.deepEqual(second.source.tempo, first.source.tempo, 'tempo drifted');
  assert.deepEqual(second.preset.ingest, first.preset.ingest, 'ingest drifted');
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
