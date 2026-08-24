/**
 * Tempo, runtime side. Mirrors serrin/tempo.py -- same formulas, same defaults.
 *
 * Duplicating the maths in two languages is a real cost, so it is worth being
 * clear about why it is not shared: the pipeline needs tempo to place frames in
 * a rendered file, and the runtime needs it to place them against the audio
 * clock *and* to let the author change it live. A baked-in rate would make the
 * BPM slider a re-render rather than a knob.
 *
 * The cross-check in tests/run_all.py exists for exactly this reason.
 */

/** Delay times as multiples of a beat. Matches NOTE_FRACTIONS in tempo.py. */
export const NOTE_FRACTIONS = {
  '1/1': 4,
  '1/2': 2,
  '1/4.': 1.5,
  '1/4': 1,
  '1/4t': 2 / 3,
  '1/8.': 0.75,
  '1/8': 0.5,
  '1/8t': 1 / 3,
  '1/16': 0.25,
  '1/16t': 1 / 6,
  '1/32': 0.125,
};

/** Swing 1.0 == triplet feel: the offbeat is pushed by a third of a step. */
export const MAX_SWING_OFFSET = 1 / 3;

export class Tempo {
  constructor({ bpm = 120, subdivision = 16, swing = 0, beatsPerBar = 4 } = {}) {
    this.bpm = bpm > 0 ? bpm : 120;
    this.subdivision = subdivision > 0 ? subdivision : 16;
    this.swing = Math.max(0, Math.min(1, swing));
    this.beatsPerBar = beatsPerBar > 0 ? beatsPerBar : 4;
  }

  static fromMeta(meta) {
    const raw = meta?.tempo;
    if (!raw) {
      // An older render with only a rate: recover a tempo rather than special-
      // casing its absence everywhere downstream.
      return Tempo.fromRate(meta?.rate ?? 8);
    }
    return new Tempo({
      bpm: raw.bpm,
      subdivision: raw.subdivision,
      swing: raw.swing,
      beatsPerBar: raw.beats_per_bar,
    });
  }

  static fromRate(rate, subdivision = 16) {
    const stepsPerBeat = subdivision / 4;
    return new Tempo({ bpm: (rate * 60) / stepsPerBeat, subdivision });
  }

  /** A copy with one field changed -- the panel's edit path. */
  with(changes) {
    return new Tempo({
      bpm: changes.bpm ?? this.bpm,
      subdivision: changes.subdivision ?? this.subdivision,
      swing: changes.swing ?? this.swing,
      beatsPerBar: changes.beatsPerBar ?? this.beatsPerBar,
    });
  }

  get stepsPerBeat() {
    return this.subdivision / 4;
  }

  get rate() {
    return (this.bpm / 60) * this.stepsPerBeat;
  }

  get secondsPerStep() {
    return 1 / this.rate;
  }

  get secondsPerBeat() {
    return 60 / this.bpm;
  }

  get stepsPerBar() {
    return this.stepsPerBeat * this.beatsPerBar;
  }

  noteSeconds(note) {
    return (NOTE_FRACTIONS[note] ?? 0.75) * this.secondsPerBeat;
  }

  /**
   * Seconds to push frame `index` later. Offbeats only, and never by a whole
   * step -- the scheduler walks frames in order and would drop any frame whose
   * onset moved backwards past its predecessor.
   */
  swingOffset(index) {
    if (!this.swing || index % 2 === 0) return 0;
    return this.swing * MAX_SWING_OFFSET * this.secondsPerStep;
  }

  /** When frame `index` sounds, in transport seconds, at the given speed. */
  onset(index, speed = 1) {
    return (index * this.secondsPerStep + this.swingOffset(index)) / (speed || 1);
  }

  bars(frames) {
    return frames / this.stepsPerBar;
  }

  /** `bar.beat.step`, 1-based, the way a DAW displays it. */
  formatPosition(index) {
    const bar = Math.floor(index / this.stepsPerBar);
    const withinBar = index - bar * this.stepsPerBar;
    const beat = Math.floor(withinBar / this.stepsPerBeat);
    const step = withinBar - beat * this.stepsPerBeat;
    return `${bar + 1}.${beat + 1}.${Math.floor(step) + 1}`;
  }

  describe() {
    const names = { 4: 'quarters', 8: 'eighths', 16: 'sixteenths', 32: 'thirty-seconds' };
    const note = names[this.subdivision] ?? `1/${this.subdivision} notes`;
    const swing = this.swing ? `, swing ${this.swing.toFixed(2)}` : '';
    return `${+this.bpm.toFixed(2)} BPM in ${note} (${+this.rate.toFixed(3)} fps${swing})`;
  }

  toJSON() {
    return {
      bpm: this.bpm,
      subdivision: this.subdivision,
      swing: this.swing,
      beats_per_bar: this.beatsPerBar,
      rate: this.rate,
    };
  }
}
