/**
 * Audio engine: native Web Audio oscillators, nothing above them.
 *
 * Section 4.3 is unusually specific and this follows it literally -- sawtooth,
 * square, triangle, sine, no samples, no synthesis library. The effects are the
 * three the doc allows: filter, simple delay, and a bitcrusher that shares the
 * pipeline's aesthetic.
 *
 * Two implementation choices worth stating, because both were live options:
 *
 * **Persistent oscillators, not one node per note.** Each voice owns one
 * oscillator for the life of the session; events are gain envelopes on top. At
 * eight voices and 12 frames a second, spawning nodes per event would mean ~100
 * node allocations a second and a garbage-collection pause exactly when the
 * piece gets dense. Persistent oscillators also let frequency *glide*, which is
 * what makes held data read as a drone rather than a stutter of identical notes.
 *
 * **Bitcrush via WaveShaper, not an AudioWorklet.** A quantizing transfer curve
 * gets the stepped, aliased character with zero setup cost and no separate
 * module file to serve. An AudioWorklet would allow true sample-rate reduction
 * (the other half of the lo-fi sound) -- noted as the upgrade path.
 */

const WAVEFORMS = ['sawtooth', 'square', 'triangle', 'sine'];

/** Quantizing transfer curve: the 8->4->2 bit staircase, as a WaveShaper. */
function crushCurve(bits, length = 4096) {
  const steps = 2 ** Math.max(1, Math.min(16, bits));
  const curve = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const x = (i / (length - 1)) * 2 - 1;
    curve[i] = Math.round(x * steps) / steps;
  }
  return curve;
}

class Voice {
  constructor(ctx, destination, index, waveform, name) {
    this.index = index;
    this.name = name;
    this.ctx = ctx;
    this.muted = false;
    this.lastFreq = 0;

    this.osc = ctx.createOscillator();
    this.osc.type = WAVEFORMS.includes(waveform) ? waveform : WAVEFORMS[index % WAVEFORMS.length];

    // Per-voice band shaping: higher voices get rolled off a little so eight
    // simultaneous sawtooths do not turn into one undifferentiated hiss.
    this.filter = ctx.createBiquadFilter();
    this.filter.type = 'lowpass';
    this.filter.frequency.value = 8000 - index * 420;
    this.filter.Q.value = 1.2;

    this.gain = ctx.createGain();
    this.gain.gain.value = 0;

    // Per-voice trim, controlled by the envelope's voice gating.
    this.level = ctx.createGain();
    this.level.gain.value = 1;

    this.osc.connect(this.filter).connect(this.gain).connect(this.level).connect(destination);
    this.osc.start();
  }

  /**
   * Schedule one event.
   * @param {number} time    AudioContext time of the onset
   * @param {number} freq    Hz
   * @param {number} amp     0..1
   * @param {number} seconds note length
   * @param {boolean} retrigger whether the data says this is a new note
   */
  play(time, freq, amp, seconds, retrigger) {
    if (this.muted || amp <= 0.0005) {
      this.gain.gain.cancelScheduledValues(time);
      this.gain.gain.setTargetAtTime(0, time, 0.01);
      return;
    }

    const safeFreq = Math.max(18, Math.min(this.ctx.sampleRate / 2.2, freq || 20));
    if (retrigger || this.lastFreq === 0) {
      this.osc.frequency.setValueAtTime(safeFreq, time);
    } else {
      // No retrigger: the data has not moved enough to count as a new note, so
      // slide instead. This is what turns flat stretches into drones.
      this.osc.frequency.setTargetAtTime(safeFreq, time, Math.max(0.008, seconds * 0.35));
    }
    this.lastFreq = safeFreq;

    const attack = retrigger ? 0.004 : Math.min(0.08, seconds * 0.4);
    const hold = Math.max(0.01, seconds * 0.55);
    const g = this.gain.gain;
    g.cancelScheduledValues(time);
    g.setValueAtTime(Math.max(0.0001, g.value), time);
    g.linearRampToValueAtTime(amp, time + attack);
    g.setTargetAtTime(0.0001, time + attack + hold, Math.max(0.01, seconds * 0.3));
  }

  setLevel(value, time) {
    this.level.gain.setTargetAtTime(value, time, 0.05);
  }

  silence(time) {
    this.gain.gain.cancelScheduledValues(time);
    this.gain.gain.setTargetAtTime(0, time, 0.02);
  }
}

export class AudioEngine {
  constructor(reader) {
    this.reader = reader;
    this.ctx = null;
    this.voices = [];
    this.started = false;

    // Live controls (section 4.5). Defaults chosen to be listenable, not safe.
    this.masterGain = 0.55;
    this.crushBits = 8; // 8 = effectively off
    this.delayMix = 0.22;
    this.filterCutoff = 9000;
    this.soloVoice = null;
    this.mutes = new Set();
    // "Audio-vs-visual mapping balance" (4.5): audio's share of the weight.
    this.balance = 0.5;
  }

  /** Must be called from a user gesture -- browsers refuse audio otherwise. */
  async start() {
    if (this.started) {
      if (this.ctx.state === 'suspended') await this.ctx.resume();
      return;
    }
    const Ctx = window.AudioContext ?? window.webkitAudioContext;
    this.ctx = new Ctx({ latencyHint: 'interactive' });
    const ctx = this.ctx;

    this.master = ctx.createGain();
    this.master.gain.value = this.masterGain;

    this.crusher = ctx.createWaveShaper();
    this.crusher.curve = crushCurve(this.crushBits);
    this.crusher.oversample = 'none'; // aliasing is the point

    this.tone = ctx.createBiquadFilter();
    this.tone.type = 'lowpass';
    this.tone.frequency.value = this.filterCutoff;
    this.tone.Q.value = 0.7;

    // Simple feedback delay. Time is one frame at the stream's own rate, so the
    // echo lands on the data's grid instead of on an arbitrary musical one.
    this.delay = ctx.createDelay(2.0);
    this.delay.delayTime.value = Math.min(1.5, 3 / this.reader.rate);
    this.feedback = ctx.createGain();
    this.feedback.gain.value = 0.34;
    this.delaySend = ctx.createGain();
    this.delaySend.gain.value = this.delayMix;

    this.bus = ctx.createGain(); // where voices land

    this.bus.connect(this.crusher);
    this.crusher.connect(this.tone);
    this.tone.connect(this.master);
    this.tone.connect(this.delaySend);
    this.delaySend.connect(this.delay);
    this.delay.connect(this.feedback);
    this.feedback.connect(this.delay); // the feedback loop
    this.delay.connect(this.master);
    this.master.connect(ctx.destination);

    const audioVoices = this.reader.audio.voices ?? [];
    this.voices = audioVoices.map(
      (voice, index) => new Voice(ctx, this.bus, index, voice.waveform, voice.name),
    );

    this.started = true;
  }

  get currentTime() {
    return this.ctx ? this.ctx.currentTime : 0;
  }

  /**
   * Schedule one frame. Called by the transport's lookahead loop, ahead of time.
   * @param {object} frame  from Reader.frame()
   * @param {number} time   AudioContext time for this frame's onset
   * @param {object} state  {intensity, gates} from the envelope
   */
  update(frame, time, state = {}) {
    if (!this.started || !frame) return;
    const intensity = state.intensity ?? 1;
    const gates = state.gates;
    const secondsPerFrame = 1 / (this.reader.rate * this.reader.speed);
    // Audio's share of the total weight, so pushing the balance toward visuals
    // actually thins the sound rather than just turning it down.
    const weight = 0.35 + 1.3 * this.balance;

    for (const event of frame.audio) {
      const voice = this.voices[event.voice];
      if (!voice) continue;

      const soloed = this.soloVoice !== null && this.soloVoice !== event.voice;
      const gated = gates ? gates[event.voice] === false : false;
      if (soloed || this.mutes.has(event.voice) || gated) {
        voice.silence(time);
        continue;
      }

      // Intensity does two things to a voice: how loud it is allowed to be, and
      // how bright (the filter opens as the piece builds).
      const amp = event.amp * weight * (0.35 + 0.65 * intensity);
      voice.filter.frequency.setTargetAtTime(
        600 + intensity * (7800 - event.voice * 380),
        time,
        0.12,
      );
      voice.play(time, event.freq, Math.min(1, amp), event.dur * secondsPerFrame, event.gate === 1);
    }

    // Global grit tracks intensity too: the doc wants aggression to rise into
    // the climax and back off at the close (5.1-A).
    if (this.crushBits >= 8) {
      const implied = Math.round(8 - intensity * 4.2);
      this.setCrush(Math.max(3, implied), false);
    }
  }

  /** Panic: kill every voice, e.g. on pause. */
  silenceAll() {
    if (!this.started) return;
    const now = this.ctx.currentTime;
    for (const voice of this.voices) voice.silence(now);
  }

  // -- live controls -------------------------------------------------------
  setMaster(value) {
    this.masterGain = value;
    if (this.master) this.master.gain.setTargetAtTime(value, this.currentTime, 0.05);
  }

  setCrush(bits, sticky = true) {
    if (sticky) this.crushBits = bits;
    if (this.crusher) this.crusher.curve = crushCurve(bits);
  }

  setDelayMix(value) {
    this.delayMix = value;
    if (this.delaySend) this.delaySend.gain.setTargetAtTime(value, this.currentTime, 0.05);
  }

  setCutoff(value) {
    this.filterCutoff = value;
    if (this.tone) this.tone.frequency.setTargetAtTime(value, this.currentTime, 0.05);
  }

  toggleMute(index) {
    if (this.mutes.has(index)) this.mutes.delete(index);
    else this.mutes.add(index);
    return this.mutes.has(index);
  }

  setSolo(index) {
    this.soloVoice = this.soloVoice === index ? null : index;
    return this.soloVoice;
  }

  /** Peak level for the panel's meters, without an analyser per voice. */
  voiceLevels() {
    return this.voices.map((v) => (v.muted ? 0 : v.gain.gain.value));
  }
}
