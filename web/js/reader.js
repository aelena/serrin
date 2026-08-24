/**
 * dataReader -- the single source of truth for "where are we in the stream".
 *
 * Section 4.2 asks for one central tick() that both engines listen to. The way
 * this implementation guarantees that is by *not* keeping a cursor at all: the
 * frame index is a pure function of transport time. The audio scheduler runs
 * ahead of the clock and the visual loop runs at the clock, but both compute
 * their position from the same expression, so they cannot drift apart -- there
 * is no shared mutable counter to get out of step.
 *
 * The design doc leaves two things open here (4.2). This picks:
 *   * tick resolution -- data-driven. One row (or one aggregated window) is one
 *     frame, and `rate` says how many frames a second. A musical grid would
 *     impose a metre the data does not have.
 *   * stream exhaustion -- author's choice, four policies, default `vary`.
 */

import { Tempo } from './tempo.js';

/** Deterministic 32-bit hash -> the JS half of the seed-reproducibility promise. */
export function hash32(value, salt = 0) {
  let h = (2166136261 ^ salt) >>> 0;
  const text = String(value);
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  h ^= h >>> 15;
  h = Math.imul(h, 2246822507) >>> 0;
  h ^= h >>> 13;
  return h >>> 0;
}

/** mulberry32: same role as the Python side's SplitMix -- small and specified. */
export function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const LOOP_POLICIES = ['vary', 'loop', 'pingpong', 'once'];

export class Reader {
  /**
   * @param {object} audioDoc  parsed stream_audio.json
   * @param {object} visualDoc parsed stream_visual.json
   */
  constructor(audioDoc, visualDoc) {
    this.audio = audioDoc;
    this.visual = visualDoc;
    this.meta = audioDoc.meta ?? {};

    // The grid is the tempo's business now; `rate` is kept as a read-only
    // convenience because plenty of call sites only want the number.
    this.tempo = Tempo.fromMeta(this.meta);
    this.length = this.meta.frames ?? (audioDoc.voices?.[0]?.freq?.length ?? 0);
    this.voiceCount = audioDoc.voices?.length ?? 0;
    this.names = this.meta.voices ?? audioDoc.voices.map((v) => v.name);
    this.seed = Number(this.meta.seed ?? 0) || hash32(this.meta.label ?? 'serrin');
    this.mode = this.meta.mode ?? 'closed';
    this.loopPolicy = this.meta.loop_policy ?? 'vary';
    this.entryOrder = this.meta.voice_entry_order ?? this.names.map((_, i) => i);

    // The two documents must describe the same stream, or the fork has come
    // apart somewhere in the pipeline and nothing downstream is trustworthy.
    const visualFrames = visualDoc.voices?.[0]?.x?.length ?? 0;
    if (visualFrames && visualFrames !== this.length) {
      console.warn(
        `serrin: audio has ${this.length} frames but visual has ${visualFrames}; ` +
          'the two exports are from different renders.',
      );
      this.length = Math.min(this.length, visualFrames);
    }

    this.glyphs = visualDoc.glyphs ?? ' .:-=+*#%@';
    this.speed = 1.0;
    this._varyCache = new Map();
  }

  get rate() {
    return this.tempo.rate;
  }

  get duration() {
    return this.length / this.rate;
  }

  get bars() {
    return this.tempo.bars(this.length);
  }

  /** Frames elapsed after `seconds` of transport time, at the current speed. */
  framePosition(seconds) {
    return seconds * this.rate * this.speed;
  }

  /**
   * Seconds of transport time at which frame `n` begins.
   *
   * The single place swing is applied. Both engines derive their timing from
   * this one expression -- the audio scheduler directly, the visual loop by
   * releasing frames as their audio onsets arrive -- so the feel lands on the
   * picture as well as the sound, with no second implementation to keep in step.
   */
  frameOnset(n) {
    return this.tempo.onset(n, this.speed);
  }

  /**
   * Map an ever-increasing frame counter onto a real index in the data.
   *
   * Returns null once a non-looping stream is spent, which is how the transport
   * learns the piece is over.
   */
  resolve(counter) {
    const n = this.length;
    if (n <= 0) return null;
    if (counter < n) return { index: counter, pass: 0, variation: null };

    switch (this.loopPolicy) {
      case 'once':
        return null;

      case 'loop': {
        const pass = Math.floor(counter / n);
        return { index: counter % n, pass, variation: null };
      }

      case 'pingpong': {
        const pass = Math.floor(counter / n);
        const within = counter % n;
        // Odd passes read backwards. Deltas invert, which is audible: the piece
        // does not just repeat, it un-happens.
        return {
          index: pass % 2 === 0 ? within : n - 1 - within,
          pass,
          variation: null,
        };
      }

      case 'vary':
      default: {
        // Section 5.1.2 policy 1: it repeats, but not identically. Each pass
        // gets a phase offset and a small transposition, both derived from the
        // seed, so the loop is reproducible without being an obvious loop.
        const pass = Math.floor(counter / n);
        const variation = this.variationFor(pass);
        return {
          index: (counter + variation.phase) % n,
          pass,
          variation,
        };
      }
    }
  }

  /** Per-pass variation: deterministic from (seed, pass). Cached, called often. */
  variationFor(pass) {
    if (pass <= 0) return { phase: 0, transpose: 0, tilt: 1, pass: 0 };
    let cached = this._varyCache.get(pass);
    if (cached) return cached;
    const next = rng(hash32(`${this.seed}/pass/${pass}`));
    cached = {
      pass,
      // Read the stream from a different starting point each time round.
      phase: Math.floor(next() * this.length),
      // Small transposition: enough to notice, not enough to change key.
      transpose: [-5, -3, 0, 2, 4, 7][Math.floor(next() * 6)],
      // Slight density tilt on the visual side.
      tilt: 0.85 + next() * 0.35,
    };
    this._varyCache.set(pass, cached);
    return cached;
  }

  /**
   * The frame both engines consume. Shape is stable whatever the loop policy.
   * @returns {null|{counter:number,index:number,t:number,audio:Array,visual:Array}}
   */
  frame(counter) {
    const resolved = this.resolve(counter);
    if (!resolved) return null;
    const { index, pass, variation } = resolved;
    const semitones = variation ? variation.transpose : 0;
    const ratio = semitones ? 2 ** (semitones / 12) : 1;
    const tilt = variation ? variation.tilt : 1;

    const audio = [];
    for (let v = 0; v < this.audio.voices.length; v += 1) {
      const voice = this.audio.voices[v];
      audio.push({
        voice: v,
        name: voice.name,
        waveform: voice.waveform,
        freq: voice.freq[index] * ratio,
        amp: voice.amp[index],
        dur: voice.dur[index],
        gate: voice.gate[index],
      });
    }

    const visual = [];
    for (let v = 0; v < (this.visual.voices?.length ?? 0); v += 1) {
      const voice = this.visual.voices[v];
      visual.push({
        voice: v,
        name: voice.name,
        x: voice.x[index],
        y: voice.y[index],
        density: Math.min(1, voice.density[index] * tilt),
        gray: voice.gray[index],
        glyph: voice.glyph[index],
        glitch: voice.glitch[index],
        flat: voice.flat ? voice.flat[index] : 0,
      });
    }

    return {
      counter,
      index,
      pass,
      t: index / this.length,
      audio,
      visual,
    };
  }

  /** Normalized progress through the *piece*, which is what the envelope wants. */
  pieceProgress(counter, totalFrames) {
    if (this.mode === 'endless' || !totalFrames) {
      // Mode B has no arc, so there is no progress to report (5.1-B).
      return null;
    }
    return Math.max(0, Math.min(1, counter / totalFrames));
  }
}

/** Fetch a rendered pair. Both files must come from the same render. */
export async function loadStreams(audioUrl, visualUrl) {
  const [audioRes, visualRes] = await Promise.all([fetch(audioUrl), fetch(visualUrl)]);
  for (const [res, url] of [
    [audioRes, audioUrl],
    [visualRes, visualUrl],
  ]) {
    if (!res.ok) {
      throw new Error(
        `cannot load ${url} (${res.status}). Render one with: ` +
          'python -m serrin render -i data/monitoring.csv -c presets/gritty_01.json',
      );
    }
  }
  const [audioDoc, visualDoc] = await Promise.all([audioRes.json(), visualRes.json()]);
  return new Reader(audioDoc, visualDoc);
}
