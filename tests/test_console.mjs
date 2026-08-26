/**
 * Headless checks for the console's code generator and the audio journal.
 *
 * The drawer itself is DOM, and testing that a table renders would test the
 * DOM. What is worth testing is the part with a contract: the journal has to
 * record what was *actually* scheduled -- including the gate-versus-slide
 * decision, which is the one thing `update()` cannot see -- and the
 * reconstruction has to be runnable JavaScript rather than something that
 * merely looks like it.
 *
 *   node tests/test_console.mjs
 */

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const mod = (rel) => pathToFileURL(path.join(root, rel)).href;

const { reconstructJs } = await import(mod('web/js/console.js'));

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

/**
 * A stand-in audio engine with a populated journal.
 *
 * Shaped to match what AudioEngine.graph() and .journal actually provide, so
 * the generator is exercised against the real contract rather than a
 * convenience.
 */
function fakeAudio(events = 3, { started = true } = {}) {
  const journal = [];
  for (let i = 0; i < events; i += 1) {
    journal.push({
      time: 10 + i * 0.125,
      voice: i % 2,
      name: `voice_${i % 2}`,
      waveform: i % 2 ? 'square' : 'sawtooth',
      freq: 220 + i * 37.5,
      amp: 0.4 + i * 0.05,
      attack: 0.004,
      hold: 0.07,
      retrigger: i % 2 === 0,
      calls: [`osc.frequency.setValueAtTime(${220 + i * 37.5}, t)`],
    });
  }
  return {
    started,
    journal,
    masterGain: 0.55,
    crushBits: 5,
    delayMix: 0.22,
    delayNote: '1/8.',
    filterCutoff: 9000,
    keyboardCrushed: false,
    delaySeconds: () => 0.375,
    graph: () => ({
      sampleRate: 48000,
      state: 'running',
      baseLatency: 0.01,
      nodes: [{ id: 'bus', type: 'GainNode', detail: 'voices land here' }],
      edges: ['bus -> crusher -> tone -> master -> destination'],
    }),
  };
}

// ---------------------------------------------------------------------------
console.log('the reconstruction');

test('refuses politely when the graph is not running', () => {
  const source = reconstructJs(fakeAudio(3, { started: false }));
  assert.match(source, /^\/\//, 'should be a comment, not code');
  assert.match(source, /not running/);
});

test('refuses politely when nothing has been scheduled', () => {
  assert.match(reconstructJs(fakeAudio(0)), /nothing scheduled/);
});

test('says outright that it is a reconstruction, not generated source', () => {
  // The honesty requirement. serrin has no generated JavaScript, and a snippet
  // that implied otherwise would be the one genuinely misleading thing in the
  // console.
  const source = reconstructJs(fakeAudio(), audioDoc.meta);
  assert.match(source, /not generated source/);
  assert.match(source, /fixed\s+\/\/ graph driven by data|graph driven by data/);
});

test('carries the render label for provenance', () => {
  const source = reconstructJs(fakeAudio(), audioDoc.meta);
  assert.ok(source.includes(audioDoc.meta.label), 'no label in the header');
});

test('is syntactically valid JavaScript', () => {
  // The whole point of offering it: paste it into a console and it runs. Parsed
  // rather than eyeballed, because a template-string generator is exactly the
  // sort of thing that emits plausible-looking broken code.
  const source = reconstructJs(fakeAudio(6), audioDoc.meta);
  assert.doesNotThrow(() => new vm.Script(source), 'the generated snippet does not parse');
});

test('stays valid with a keyboard note in the journal', () => {
  const audio = fakeAudio(2);
  audio.journal.push({
    time: 10.4,
    voice: -1,
    name: 'keyboard',
    waveform: 'square',
    freq: 440,
    amp: 0.7,
    attack: 0.003,
    hold: 0.22,
    retrigger: true,
    midi: 69,
    calls: ["osc.type = 'square'"],
  });
  const source = reconstructJs(audio, audioDoc.meta);
  assert.doesNotThrow(() => new vm.Script(source));
  assert.match(source, /MIDI 69/);
});

test('rebuilds the whole effect chain', () => {
  const source = reconstructJs(fakeAudio(), audioDoc.meta);
  for (const expected of [
    'createWaveShaper',
    'createBiquadFilter',
    'createDelay',
    'bus.connect(crusher)',
    'delay.connect(feedback)',
    'master.connect(ctx.destination)',
  ]) {
    assert.ok(source.includes(expected), `missing ${expected}`);
  }
});

test('carries the live effect settings, not defaults', () => {
  const audio = fakeAudio();
  audio.crushBits = 3;
  audio.filterCutoff = 1234.5;
  audio.masterGain = 0.21;
  const source = reconstructJs(audio, audioDoc.meta);
  assert.match(source, /\}\)\(3\);/, 'crush bits not carried');
  assert.ok(source.includes('1234.5'), 'cutoff not carried');
  assert.ok(source.includes('0.210'), 'master gain not carried');
});

test('emits one block per journalled event', () => {
  const source = reconstructJs(fakeAudio(5), audioDoc.meta);
  const blocks = source.match(/createOscillator\(\)/g) ?? [];
  assert.equal(blocks.length, 5);
});

test('times are relative to the first event, so it starts immediately', () => {
  // The journal holds absolute AudioContext times, which are meaningless in
  // another context -- pasting it would schedule everything in the past.
  const source = reconstructJs(fakeAudio(3), audioDoc.meta);
  assert.ok(source.includes('t0 + 0.0000'), 'the first event is not at zero');
  assert.ok(!source.includes('t0 + 10.'), 'absolute context time leaked in');
});

test('honours the event limit', () => {
  const source = reconstructJs(fakeAudio(50), audioDoc.meta, 4);
  assert.equal((source.match(/createOscillator\(\)/g) ?? []).length, 4);
});

test('labels slides differently from new notes', () => {
  const source = reconstructJs(fakeAudio(2), audioDoc.meta);
  assert.match(source, /new note/);
  assert.match(source, /slide, the data barely moved/);
});

// ---------------------------------------------------------------------------
console.log('the audio journal contract');

test('the engine exposes the fields the generator needs', async () => {
  // A structural check against the real module: if AudioEngine stops providing
  // one of these, the generator breaks at runtime and nowhere else.
  const source = await readFile(path.join(root, 'web/js/audio.js'), 'utf8');
  for (const member of [
    'setJournalling',
    'journalLimit',
    'graph()',
    'delaySeconds()',
    'this.journal',
  ]) {
    assert.ok(source.includes(member), `audio.js no longer provides ${member}`);
  }
});

test('the journal records the gate decision', () => {
  // The one thing update() cannot see: whether a frame retriggered or slid is
  // decided inside Voice.play, so journalling from outside would lose it.
  const source = fakeAudio(2).journal;
  assert.equal(source[0].retrigger, true);
  assert.equal(source[1].retrigger, false);
});

test('journalling is off by default in the real engine', async () => {
  const source = await readFile(path.join(root, 'web/js/audio.js'), 'utf8');
  assert.match(source, /journalEnabled = false/);
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
