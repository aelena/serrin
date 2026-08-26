/**
 * The keyboard: playing along with the piece, live.
 *
 * This is the first thing in serrin that is *performed* rather than generated,
 * so it sits slightly outside the project's central promise. Everything else is
 * reproducible from `source + chain + seed`; a person hitting keys is not. The
 * compromise taken here is the same one the live envelope stroke takes: the
 * randomness is seed-derived, so a given *sequence of presses* always produces
 * the same notes. The performance is not reproducible, but the instrument is.
 *
 * Structured as a mode registry because the modes are arriving in stages:
 *
 *   random   -- implemented. Any key draws a note from the piece's own scale.
 *   notes    -- planned. Fixed key -> note maps, for playing actual melodies.
 *   samples  -- planned. Key -> sample. Needs a map editor, as does `notes`.
 *   beats    -- planned. Step sequencing and live recording over the stream.
 *
 * The three planned modes are listed rather than hidden so the shape of the
 * thing is visible, but they are inert and say so. Nothing here pretends.
 */

import { hash32, rng } from './reader.js';

/** The piano, in MIDI. A0 to C8 -- the bound the register options live inside. */
export const PIANO_LOW = 21;
export const PIANO_HIGH = 108;

/**
 * Registers, as MIDI ranges. Named rather than exposed as two number boxes
 * because the useful question is "where does this sit against the noise", not
 * "which semitone is the ceiling".
 */
export const REGISTERS = {
  bass: { label: 'bass · C1–C3', low: 24, high: 48 },
  mid: { label: 'mid · C3–C6', low: 48, high: 84 },
  treble: { label: 'treble · C5–C8', low: 72, high: 108 },
  full: { label: 'the whole piano · A0–C8', low: PIANO_LOW, high: PIANO_HIGH },
  piece: { label: "the piece's own register", low: null, high: null },
};

export const MODES = {
  random: { label: 'random · any key, a note in the scale', ready: true },
  notes: { label: 'note map · key → fixed note (pending)', ready: false },
  samples: { label: 'samples · key → sample (pending)', ready: false },
  beats: { label: 'beats · sequence and record (pending)', ready: false },
};

/** Keys the piece keeps for itself even while the keyboard is live. */
const RESERVED = new Set([' ', 'Escape', 'Tab', 'F5', 'F11', 'F12']);

const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

export function noteName(midi) {
  const rounded = Math.round(midi);
  // MIDI 60 is C4 in the convention almost every DAW uses.
  return `${NOTE_NAMES[((rounded % 12) + 12) % 12]}${Math.floor(rounded / 12) - 1}`;
}

/** Proper modulo -- JS `%` keeps the sign, which breaks notes below the root. */
const mod = (a, n) => ((a % n) + n) % n;

export class KeyboardEngine {
  /**
   * @param {import('./audio.js').AudioEngine} audio
   * @param {import('./reader.js').Reader} reader
   */
  constructor(audio, reader) {
    this.audio = audio;
    this.reader = reader;

    this.enabled = false;
    this.mode = 'random';
    this.register = 'mid';
    this.level = 0.7;
    this.waveform = 'square';
    this.showKeys = false;

    this.pressCount = 0;
    this.lastNote = null;
    this.held = new Map(); // key -> midi, so a key-up knows what it started
    this.onNote = null; // set by main.js for visual feedback

    this.adopt(reader);
  }

  /** Point at a (new) piece: its scale, its register, its seed. */
  adopt(reader) {
    this.reader = reader;
    const meta = reader.meta ?? {};
    // A render from before scales were exported still has to be playable.
    this.scale = meta.scale ?? {
      name: 'pentatonic_minor',
      offsets: [0, 3, 5, 7, 10],
      span: 12,
      root: 33,
      source: 'fallback',
      note_low: 33,
      note_high: 72,
    };
    this.seed = reader.seed ?? 0;
    this.pressCount = 0;
    this.lastNote = null;
    this._rebuildPool();
  }

  // -- the note pool -------------------------------------------------------
  /**
   * Every MIDI note in the current register that belongs to the piece's scale.
   *
   * Built once per change rather than per keypress: it is the same few dozen
   * integers every time, and a keypress should not be doing arithmetic over
   * eighty-eight candidates while the audio thread waits.
   */
  _rebuildPool() {
    const { offsets, span, root } = this.scale;
    const bounds = this.registerBounds();
    const inScale = new Set(offsets);
    const pool = [];
    for (let midi = bounds.low; midi <= bounds.high; midi += 1) {
      if (inScale.has(mod(midi - root, span))) pool.push(midi);
    }
    // A register that misses the scale entirely would leave nothing to play;
    // falling back to the whole piano is better than a silent instrument.
    this.pool = pool.length ? pool : this._poolOver(PIANO_LOW, PIANO_HIGH);
  }

  _poolOver(low, high) {
    const { offsets, span, root } = this.scale;
    const inScale = new Set(offsets);
    const pool = [];
    for (let midi = low; midi <= high; midi += 1) {
      if (inScale.has(mod(midi - root, span))) pool.push(midi);
    }
    return pool;
  }

  registerBounds() {
    const chosen = REGISTERS[this.register] ?? REGISTERS.mid;
    const low = chosen.low ?? this.scale.note_low ?? 48;
    const high = chosen.high ?? this.scale.note_high ?? 84;
    // Clamped to the piano either way: "las octavas del piano" is the bound,
    // whatever a preset's own register happens to be.
    return {
      low: Math.max(PIANO_LOW, Math.min(PIANO_HIGH, Math.min(low, high))),
      high: Math.min(PIANO_HIGH, Math.max(PIANO_LOW, Math.max(low, high))),
    };
  }

  setRegister(name) {
    this.register = name in REGISTERS ? name : 'mid';
    this._rebuildPool();
  }

  setMode(name) {
    this.mode = name in MODES ? name : 'random';
    return MODES[this.mode].ready;
  }

  get ready() {
    return MODES[this.mode]?.ready === true;
  }

  describe() {
    const bounds = this.registerBounds();
    const source =
      this.scale.source === 'default' || this.scale.source === 'fallback'
        ? ` (the piece declares none — assuming ${this.scale.name})`
        : ` from ${this.scale.source}`;
    return (
      `${this.scale.name}${source} · ${this.pool.length} notes ` +
      `between ${noteName(bounds.low)} and ${noteName(bounds.high)}`
    );
  }

  // -- playing -------------------------------------------------------------
  /** True if this event is ours to consume. */
  claims(event) {
    if (!this.enabled || !this.ready) return false;
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    if (RESERVED.has(event.key)) return false;
    // Single-character keys only: letters, digits, punctuation. Excludes the
    // function keys and arrows, which later modes will want for transposing.
    return event.key.length === 1;
  }

  /**
   * Handle a key-down. Returns the note played, or null.
   *
   * Repeats are dropped: browsers fire keydown continuously while a key is
   * held, and in random mode that would machine-gun a different note every few
   * milliseconds.
   */
  press(event) {
    if (!this.claims(event) || event.repeat) return null;
    if (this.held.has(event.code)) return null;

    const midi = this._noteFor(event);
    if (midi === null) return null;

    this.held.set(event.code, midi);
    this.pressCount += 1;
    this.lastNote = midi;

    this.audio.playNote(midi, this.level, {
      waveform: this.waveform,
      // Short and percussive. "Bleep", not "pad": it has to punch through the
      // stream rather than sit under it.
      attack: 0.003,
      decay: 0.22,
    });

    const played = { midi, name: noteName(midi), key: event.key, mode: this.mode };
    this.onNote?.(played);
    return played;
  }

  release(event) {
    this.held.delete(event.code);
  }

  /** Everything currently held, silenced. Called when the mode is switched off. */
  panic() {
    this.held.clear();
  }

  // -- modes ---------------------------------------------------------------
  _noteFor(event) {
    switch (this.mode) {
      case 'random':
        return this._randomNote();
      // The planned modes are unreachable -- `claims()` gates on `ready` -- but
      // spelled out so the next commit has an obvious place to land.
      case 'notes':
      case 'samples':
      case 'beats':
        return null;
      default:
        return null;
    }
  }

  /**
   * A note from the pool, drawn deterministically from (seed, press number).
   *
   * So the tenth key of a performance is always the same note for a given
   * piece, whichever key it was. That keeps the instrument reproducible even
   * though the playing is not -- and it means a recorded key sequence could
   * later be replayed exactly, which is where the beats mode is heading.
   */
  _randomNote() {
    const draw = rng(hash32(`${this.seed}/key/${this.pressCount}`));
    let index = Math.floor(draw() * this.pool.length);
    // Avoid an immediate repeat: two identical notes in a row read as a stuck
    // key rather than as a choice. Stepping is deterministic, so this does not
    // cost reproducibility.
    if (this.pool.length > 1 && this.pool[index] === this.lastNote) {
      index = (index + 1) % this.pool.length;
    }
    return this.pool[index];
  }
}
