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
 *   random   -- any key draws a note from the piece's own scale, freshly each
 *               press. Scattershot: the same key rarely repeats a note.
 *   fixed    -- every key keeps one note, chosen for you rather than by you.
 *               No map to author, but pressing `h` three times gives the same
 *               note three times, so it is playable rather than scattershot.
 *   notes    -- the piece's key map. Positions you chose, for melodies.
 *   samples  -- planned. Key -> sample.
 *   beats    -- planned. Step sequencing and live recording over the stream.
 *
 * The two planned modes are listed rather than hidden so the shape of the thing
 * is visible, but they are inert and say so. Nothing here pretends.
 *
 * `notes` differs from `random` in one way worth naming: it claims only the keys
 * the map actually binds. An unbound key falls through to the piece's own
 * shortcuts, which makes a sparse map usable rather than a trap.
 */

import { describeBinding, resolveBinding } from './keymap.js';
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
  random: { label: 'random · a fresh note every press', ready: true },
  fixed: { label: 'random but fixed · every key keeps its note', ready: true },
  notes: { label: "note map · the piece's own layout", ready: true },
  samples: { label: 'samples · key → sample (pending)', ready: false },
  beats: { label: 'beats · sequence and record (pending)', ready: false },
};

/**
 * Keys the piece keeps for itself even while the keyboard is live.
 *
 * Deliberately short. These are the transport and the ways *back out*: play,
 * disarm, and open the panel. `p` is here because arming the keyboard used to
 * lock the author out of the panel -- `p` played a note, and the only way back
 * was Escape, which disarmed the very thing being configured.
 *
 * Everything else single-character is the instrument's, including the digits
 * that mute voices when it is disarmed. That is a deliberate precedence rather
 * than a collision: while you are playing, the keys play.
 */
const RESERVED = new Set([' ', 'Escape', 'Tab', 'p', 'P', 'F5', 'F11', 'F12']);

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
    //: code -> the playing voice handle, so a key-up can release the right note.
    //: Was a code -> midi map when notes were fire-and-forget; a held note needs
    //: the handle, not just what it played.
    this.held = new Map();
    this.onNote = null; // set by main.js for visual feedback

    //: How much of the peak a held note holds at. 0 is the original percussive
    //: bleep, which is still a legitimate choice rather than a limitation.
    this.sustain = 0.6;
    this.releaseTime = 0.14;

    //: The piece's map, by physical key position. Empty until a piece is loaded.
    this.keymap = {};
    //: Live octave shift, for reaching outside the map without editing it.
    this.octaveOffset = 0;

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

  /**
   * Load a key map, usually from the piece's performance layer.
   *
   * Kept separate from `adopt` because a map belongs to the *piece* and a scale
   * belongs to the *render*: switching render without switching piece should not
   * discard the layout the author built.
   */
  setKeymap(keymap) {
    this.keymap = keymap && typeof keymap === 'object' ? { ...keymap } : {};
    return Object.keys(this.keymap).length;
  }

  /** What a position plays right now, given the scale and the octave shift. */
  bindingFor(code) {
    return this.keymap[code] ?? null;
  }

  describeKey(code) {
    return describeBinding(this.keymap[code], this.scale, this.octaveOffset);
  }

  /** Shift the whole map by octaves. Returns the new offset. */
  shiftOctave(delta) {
    // Clamped: three octaves either way already exceeds the piano from any
    // sensible root, and unbounded shifting just produces silence.
    this.octaveOffset = Math.max(-3, Math.min(3, this.octaveOffset + delta));
    return this.octaveOffset;
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
    const source =
      this.scale.source === 'default' || this.scale.source === 'fallback'
        ? ` (the piece declares none — assuming ${this.scale.name})`
        : ` from ${this.scale.source}`;
    const shift = this.octaveOffset ? ` · octave ${this.octaveOffset > 0 ? '+' : ''}${this.octaveOffset}` : '';

    if (this.mode === 'notes') {
      const bound = Object.keys(this.keymap).length;
      if (!bound) {
        return `${this.scale.name}${source} · no key map — build one in the studio (F3)`;
      }
      return `${this.scale.name}${source} · ${bound} keys mapped${shift}`;
    }

    const bounds = this.registerBounds();
    const held = this.held.size ? ` · ${this.held.size} held` : '';
    const hold = this.sustain > 0.005 ? '' : ' · percussive';
    return (
      `${this.scale.name}${source} · ${this.pool.length} notes ` +
      `between ${noteName(bounds.low)} and ${noteName(bounds.high)}${shift}${hold}${held}`
    );
  }

  // -- playing -------------------------------------------------------------
  /** True if this event is ours to consume. */
  claims(event) {
    if (!this.enabled || !this.ready) return false;
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    if (RESERVED.has(event.key)) return false;
    // Single-character keys only: letters, digits, punctuation. Excludes the
    // function keys and the arrows, which shift octaves instead.
    if (event.key.length !== 1) return false;
    // In `notes` an unbound position is not ours -- it falls through to the
    // piece's shortcuts, so a map covering nine keys does not swallow the other
    // thirty. `random` has no map and claims everything.
    if (this.mode === 'notes') return Boolean(this.keymap[event.code]);
    return true;
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

    this.pressCount += 1;
    this.lastNote = midi;

    const voice = this.audio.playNote(midi, this.level, {
      waveform: this.waveform,
      // Fast attack either way: it has to punch through the stream rather than
      // sit under it. What sustain changes is what happens after.
      attack: 0.003,
      decay: 0.22,
      sustain: this.sustain,
      release: this.releaseTime,
    });
    // Stored even when null (no audio context yet), so the key still counts as
    // held and does not machine-gun.
    this.held.set(event.code, voice ?? null);

    const played = {
      midi,
      name: noteName(midi),
      key: event.key,
      code: event.code,
      mode: this.mode,
      binding: this.keymap[event.code] ?? null,
    };
    this.onNote?.(played);
    return played;
  }

  /** Let go: release the note this position started, if it is still holding. */
  release(event) {
    const voice = this.held.get(event.code);
    this.held.delete(event.code);
    voice?.release?.();
  }

  /**
   * Everything currently held, released.
   *
   * Called when the keyboard is switched off, when a piece is unloaded, and --
   * importantly -- when the window loses focus. Alt-tabbing mid-chord otherwise
   * leaves a drone behind that nothing on the page can reach any more, because
   * the key-up lands in whatever window took focus.
   */
  panic() {
    for (const voice of this.held.values()) voice?.release?.();
    this.held.clear();
  }

  // -- modes ---------------------------------------------------------------
  _noteFor(event) {
    switch (this.mode) {
      case 'random':
        return this._randomNote();
      case 'fixed':
        return this._fixedNote(event);
      case 'notes':
        return this._mappedNote(event);
      // The planned modes are unreachable -- `claims()` gates on `ready` -- but
      // spelled out so the next commit has an obvious place to land.
      case 'samples':
      case 'beats':
        return null;
      default:
        return null;
    }
  }

  /**
   * The note this physical position is bound to.
   *
   * Sample and pattern bindings resolve to something that is not a note, and
   * nothing can play them yet -- so they report themselves rather than quietly
   * falling back to a pitch, which would make a half-built map sound finished.
   */
  _mappedNote(event) {
    const resolved = resolveBinding(this.keymap[event.code], this.scale, this.octaveOffset);
    if (!resolved) return null;
    if (resolved.kind !== 'note') {
      this.lastSkipped = resolved;
      return null;
    }
    return resolved.midi;
  }

  /**
   * The note this key always plays, drawn from (seed, key position).
   *
   * The difference from `random` is the second half of that pair: `random` keys
   * off the *press number*, so a key gives a new note every time and the
   * instrument is scattershot. Keying off the *position* instead means `h` is
   * always the same note, which makes it playable -- you can learn it, find
   * intervals, come back to a phrase -- without anyone having authored a map.
   *
   * Seed-derived, so a different piece scatters differently while any one piece
   * keeps its layout across reloads. Position rather than character, for the
   * same reason key maps use positions: it survives a change of layout.
   */
  _fixedNote(event) {
    if (!this.pool.length) return null;
    const draw = rng(hash32(`${this.seed}/fixed/${event.code}`));
    const midi = this.pool[Math.floor(draw() * this.pool.length)];
    const span = this.scale?.span ?? 12;
    return Math.max(
      PIANO_LOW,
      Math.min(PIANO_HIGH, midi + this.octaveOffset * span),
    );
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
