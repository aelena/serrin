/**
 * Capture a session exactly as the browser would, and write it to a file.
 *
 * Not a test suite -- a fixture generator, named so `run_all.py` does not pick
 * it up as one. It exists so the cross-language check can be real: the browser
 * writes the session, Python reads it back and re-renders, and the fingerprints
 * have to match. Testing each half separately would leave the seam between them
 * untested, and the seam is where a format goes wrong.
 *
 *   node tests/session_fixture.mjs <repo-root> <out.json>
 */

import { readFile, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const root = process.argv[2];
const target = process.argv[3];
if (!root || !target) {
  console.error('usage: node tests/session_fixture.mjs <repo-root> <out.json>');
  process.exit(2);
}

const mod = (rel) => pathToFileURL(path.join(root, rel)).href;
const { Reader } = await import(mod('web/js/reader.js'));
const { Tempo } = await import(mod('web/js/tempo.js'));
const { Envelope } = await import(mod('web/js/envelope.js'));
const { KeyboardEngine } = await import(mod('web/js/keyboard.js'));
const { capture } = await import(mod('web/js/session.js'));

const audioDoc = JSON.parse(await readFile(path.join(root, 'out/stream_audio.json'), 'utf8'));
const visualDoc = JSON.parse(await readFile(path.join(root, 'out/stream_visual.json'), 'utf8'));
const reader = new Reader(audioDoc, visualDoc);

const audio = {
  started: true,
  masterGain: 0.4,
  crushBits: 5,
  delayMix: 0.3,
  delayNote: '1/16',
  filterCutoff: 3000,
  balance: 0.4,
  keyboardCrushed: true,
  mutes: new Set([3]),
  soloVoice: null,
  setMaster(v) { this.masterGain = v; },
  setCrush(v) { this.crushBits = v; },
  setDelayMix(v) { this.delayMix = v; },
  setDelayNote(v) { this.delayNote = v; },
  setCutoff(v) { this.filterCutoff = v; },
  setKeyboardCrushed(v) { this.keyboardCrushed = v; },
  syncDelay() {},
  playNote() {},
};

const visual = {
  showGlyphs: true,
  showBars: false,
  showBanding: true,
  corruption: 1.4,
  invert: true,
  hidden: new Set([7]),
  showKeys: true,
  balance: 0.4,
};

const app = {
  reader,
  audio,
  visual,
  transport: { targetFrames: reader.length, counter: 0, retime() {} },
  envelope: Envelope.fromExport(audioDoc.envelope),
  entryStrategy: 'sparse',
  presetId: 'gritty_01',
  presets: [
    { id: 'gritty_01', audio: '../out/stream_audio.json', visual: '../out/stream_visual.json' },
  ],
  setEnvelope(envelope) { this.envelope = envelope; },
  setEntryStrategy(strategy) { this.entryStrategy = strategy; },
  setTempo(tempo) { this.reader.tempo = tempo; },
};

app.keyboard = new KeyboardEngine(audio, reader);
app.keyboard.enabled = true;
app.keyboard.setRegister('treble');
app.keyboard.level = 0.55;

// A hand-drawn envelope and a tempo moved by ear: the two things that only
// exist because someone was listening, and the two most likely to be lost.
app.setEnvelope(
  Envelope.fromPoints(
    [
      [0, 0.05],
      [0.3, 0.9],
      [0.55, 0.35],
      [1, 0.6],
    ],
    256,
    { kind: 'stroke', label: 'fixture' },
  ),
);
app.setTempo(new Tempo({ bpm: 104, subdivision: 16, swing: 0.22 }));

await writeFile(target, `${JSON.stringify(capture(app, 'tuned by ear'), null, 2)}\n`);
console.log(`wrote ${target}`);
