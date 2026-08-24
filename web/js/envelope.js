/**
 * The runtime half of the intensity envelope (section 5.1).
 *
 * Python bakes a curve into the export; this evaluates it, and can also *replace*
 * it with a stroke drawn live on a tablet. Both end up as the same thing -- an
 * array of samples plus interpolation -- which is the invariant section 5.1 asks
 * for: nothing downstream knows or cares where the curve came from.
 *
 * On the open question in section 8 ("if the stroke is drawn live, can it be
 * recorded for identical playback?"): yes. A live stroke is captured as
 * (t, intensity) points against the transport clock, so it serialises exactly
 * like an offline one. What is *not* reproducible is the drawing gesture's
 * timing relative to a different-length piece -- hence `normalizeTime`.
 */

import { hash32, rng } from './reader.js';

const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

export class Envelope {
  /**
   * @param {number[]} curve  evenly spaced intensity samples in [0,1]
   * @param {object}   origin provenance, carried through to any re-export
   */
  constructor(curve = [1], origin = { kind: 'constant' }) {
    this.curve = curve.length ? curve : [1];
    this.origin = origin;
  }

  static fromExport(envelopeDoc) {
    if (!envelopeDoc || !envelopeDoc.curve?.length) {
      return new Envelope([1], { kind: 'constant', level: 1 });
    }
    return new Envelope(envelopeDoc.curve, envelopeDoc.origin ?? { kind: 'baked' });
  }

  static constant(level = 1) {
    return new Envelope([level], { kind: 'constant', level });
  }

  /** Resample irregular (t, v) points -- a stroke -- onto an even grid. */
  static fromPoints(points, resolution = 512, origin = { kind: 'stroke' }) {
    if (!points.length) return Envelope.constant(1);
    const sorted = [...points].sort((a, b) => a[0] - b[0]);
    const lo = sorted[0][0];
    const hi = sorted[sorted.length - 1][0];
    const span = hi - lo || 1;
    const curve = new Array(resolution);
    let cursor = 0;
    for (let i = 0; i < resolution; i += 1) {
      const t = lo + (i / (resolution - 1)) * span;
      while (cursor < sorted.length - 2 && sorted[cursor + 1][0] < t) cursor += 1;
      const [t0, v0] = sorted[cursor];
      const [t1, v1] = sorted[Math.min(cursor + 1, sorted.length - 1)];
      const k = t1 === t0 ? 0 : (t - t0) / (t1 - t0);
      curve[i] = clamp01(v0 + (v1 - v0) * clamp01(k));
    }
    return new Envelope(curve, { ...origin, captured: points.length });
  }

  /** Intensity at normalized piece time. */
  at(t) {
    const curve = this.curve;
    if (curve.length === 1) return curve[0];
    const x = clamp01(t) * (curve.length - 1);
    const i = Math.floor(x);
    const j = Math.min(curve.length - 1, i + 1);
    return curve[i] + (curve[j] - curve[i]) * (x - i);
  }

  toJSON() {
    return { origin: this.origin, resolution: this.curve.length, curve: this.curve };
  }
}

/**
 * Voice activation, section 5.1.1. Mirrors serrin/envelope.py exactly -- the
 * same exponent, the same rounding -- so an offline preview and the live engine
 * agree about how many voices are up at a given intensity.
 */
export function activeVoiceCount(intensity, total, minimum = 1) {
  if (total <= 0) return 0;
  const shaped = clamp01(intensity) ** 1.6;
  return Math.max(minimum, Math.min(total, Math.round(minimum + shaped * (total - minimum))));
}

export function voiceGates(intensity, entryOrder, minimum = 1) {
  const live = activeVoiceCount(intensity, entryOrder.length, minimum);
  const allowed = new Set(entryOrder.slice(0, live));
  return entryOrder.map((_, index) => allowed.has(index));
}

/**
 * Which pedals are notionally on at this intensity.
 *
 * Phase 1 renders the chain offline, so this cannot actually switch a pedal off
 * in the audio path -- it reports what *would* be on, which the panel displays
 * and the visual engine uses to decide how much dirt to draw. When the chain is
 * ported to JS (roadmap step 5) this becomes the real gate.
 */
export function pedalGates(intensity, chain) {
  const slots = chain?.chain ?? [];
  return slots.map((slot) => ({
    pedal: slot.pedal,
    threshold: slot.at_intensity ?? 0,
    on: (slot.enabled ?? true) && intensity >= (slot.at_intensity ?? 0),
  }));
}

/**
 * Live stroke capture. Pointer Events, so a stylus, a finger and a mouse all
 * land in the same handler -- section 5.1 asks explicitly for pen input.
 */
export class StrokeRecorder {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {(env: Envelope) => void} onCommit called when the stroke ends
   */
  constructor(canvas, onCommit) {
    this.canvas = canvas;
    this.onCommit = onCommit;
    this.points = [];
    this.drawing = false;
    this.usePressure = false;

    canvas.style.touchAction = 'none'; // otherwise a stylus scrolls the page
    canvas.addEventListener('pointerdown', (e) => this._begin(e));
    canvas.addEventListener('pointermove', (e) => this._extend(e));
    canvas.addEventListener('pointerup', (e) => this._end(e));
    canvas.addEventListener('pointercancel', () => this._abort());
    canvas.addEventListener('pointerleave', (e) => {
      if (this.drawing) this._end(e);
    });
  }

  _sample(event) {
    const rect = this.canvas.getBoundingClientRect();
    const t = clamp01((event.clientX - rect.left) / rect.width);
    let v = clamp01(1 - (event.clientY - rect.top) / rect.height);
    if (this.usePressure && event.pressure > 0 && event.pointerType === 'pen') {
      // Pressure as a second axis: press harder for more intensity at the same
      // height. Off by default because it surprises people who expected height.
      v = clamp01(v * 0.6 + event.pressure * 0.4);
    }
    return [t, v];
  }

  _begin(event) {
    this.canvas.setPointerCapture?.(event.pointerId);
    this.drawing = true;
    this.points = [this._sample(event)];
    this.canvas.dispatchEvent(new CustomEvent('stroke:start'));
  }

  _extend(event) {
    if (!this.drawing) return;
    const [t, v] = this._sample(event);
    const last = this.points[this.points.length - 1];
    // Monotonic in time: a stroke that doubles back would stop being a function
    // of t. Backwards movement rewrites the value at the point already reached.
    if (t < last[0]) {
      last[1] = v;
    } else {
      this.points.push([t, v]);
    }
    this.canvas.dispatchEvent(new CustomEvent('stroke:move'));
  }

  _end() {
    if (!this.drawing) return;
    this.drawing = false;
    if (this.points.length < 2) return;
    this.onCommit(
      Envelope.fromPoints(this.points, 512, {
        kind: 'stroke',
        pressure: this.usePressure,
        drawnAt: new Date().toISOString(),
      }),
    );
  }

  _abort() {
    this.drawing = false;
    this.points = [];
  }

  clear() {
    this.points = [];
  }
}

/**
 * The same equations and archetypes as the Python side, for when the author
 * wants to try a different curve without re-rendering the stream.
 */
const sigmoid = (t, centre = 0.5, steepness = 10) => 1 / (1 + Math.exp(-steepness * (t - centre)));

export const EQUATIONS = {
  ramp: (t) => t,
  ramp_down: (t) => 1 - t,
  sigmoid: (t, p = {}) => sigmoid(t, p.centre ?? 0.5, p.steepness ?? 10),
  arc: (t, p = {}) => {
    const peak = p.peak ?? 0.62;
    const steep = p.steepness ?? 9;
    const decay = p.decay ?? 3;
    if (t <= peak) return sigmoid(t, peak * 0.55, steep);
    return sigmoid(peak, peak * 0.55, steep) * Math.exp((-decay * (t - peak)) / Math.max(1e-6, 1 - peak));
  },
  plateau: (t, p = {}) => {
    const rise = p.rise ?? 0.2;
    const fall = p.fall ?? 0.8;
    if (t < rise) return clamp01(t / Math.max(1e-6, rise));
    if (t < fall) return 1;
    return clamp01((1 - t) / Math.max(1e-6, 1 - fall));
  },
  pulse: (t, p = {}) => {
    const floor = p.floor ?? 0.15;
    return floor + (1 - floor) * (0.5 - 0.5 * Math.cos(2 * Math.PI * (p.cycles ?? 4) * t));
  },
  flat: (t, p = {}) => p.level ?? 1,
};

export const ARCHETYPES = {
  build_up: [[0.25, 0.25], [0.7, 0.8], [1, 1]],
  crescendo: [[0.15, 0.1], [0.85, 1], [1, 1]],
  climax: [[0.15, 0.2], [0.5, 0.7], [0.7, 1], [0.9, 0.35], [1, 0]],
  fade_out: [[0.4, 1], [1, 0]],
  dismantling: [[0.3, 1], [0.7, 0.4], [1, 0.05]],
  full_arc: [[0.2, 0.35], [0.45, 0.75], [0.65, 1], [0.85, 0.4], [1, 0]],
};

export function envelopeFromEquation(name, params = {}, resolution = 512) {
  const fn = EQUATIONS[name];
  if (!fn) throw new Error(`unknown equation ${name}`);
  const curve = Array.from({ length: resolution }, (_, i) =>
    clamp01(fn(i / (resolution - 1), params)),
  );
  return new Envelope(curve, { kind: 'equation', equation: name, params });
}

export function envelopeFromArchetype(name, curvature = 1) {
  const phases = ARCHETYPES[name];
  if (!phases) throw new Error(`unknown archetype ${name}`);
  const points = [[0, phases[0][1] * 0.25]];
  let level = points[0][1];
  let cursor = 0;
  for (const [end, target] of phases) {
    const width = end - cursor;
    for (let step = 1; step <= 8; step += 1) {
      const k = step / 8;
      points.push([cursor + width * k, level + (target - level) * k ** curvature]);
    }
    cursor = end;
    level = target;
  }
  return Envelope.fromPoints(points, 512, { kind: 'archetype', archetype: name, curvature });
}

/**
 * Mode B's reactive intensity (5.1-B): no arc, so intensity follows the data --
 * spikes and turbulence push it up, stillness lets it settle back.
 */
export class ReactiveIntensity {
  constructor(seed = 0, attack = 0.35, release = 0.02) {
    this.value = 0.5;
    this.attack = attack;
    this.release = release;
    this.jitter = rng(hash32(`${seed}/reactive`));
  }

  update(visualFrame) {
    if (!visualFrame?.length) return this.value;
    let peak = 0;
    for (const v of visualFrame) peak = Math.max(peak, v.density, v.glitch ? 1 : 0);
    // Fast up, slow down: an anomaly should register immediately and take a
    // while to forgive, which is how tension accumulates without a plan.
    const target = 0.25 + 0.75 * peak;
    const k = target > this.value ? this.attack : this.release;
    this.value += (target - this.value) * k;
    return clamp01(this.value);
  }
}
