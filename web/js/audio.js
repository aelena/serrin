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

/** A4 = MIDI 69 = 440 Hz. Same convention as serrin/scales.py. */
export function midiToHz(midi) {
  return 440 * 2 ** ((midi - 69) / 12);
}

/**
 * Per-waveform gain, so the four timbres land at comparable loudness.
 *
 * Sine was reported as "not sounding", and it was not broken -- it was much
 * quieter than the others at the same amplitude, which under eight sawtooth data
 * voices amounts to inaudible. A sine puts all its energy in the fundamental; a
 * square and a sawtooth spread it across a harmonic series that reads as far
 * louder to the ear, and lower fundamentals lose further ground to the
 * equal-loudness contours.
 *
 * Normalized *down* from sine rather than boosting sine up: scaling the bright
 * ones has no headroom problem, while multiplying an already-0.9 velocity would
 * clip. The overall keyboard therefore sits lower, which the bus gain below
 * compensates for once rather than per note.
 */
const WAVEFORM_GAIN = {
  sine: 1.0,
  triangle: 0.82,
  sawtooth: 0.5,
  square: 0.42,
};

/** How long a held note may sustain before it is released for you. */
const MAX_SUSTAIN_SECONDS = 30;

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
    // Set by the engine when journalling is on. Kept as a plain field rather
    // than a callback per event so the hot path costs one null check.
    this.journal = null;
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

    this.journal?.push({
      time,
      voice: this.index,
      name: this.name,
      waveform: this.osc.type,
      freq: safeFreq,
      amp,
      attack,
      hold,
      retrigger,
      // The literal calls, in order, so the reconstruction is a transcript
      // rather than an interpretation of one.
      calls: retrigger
        ? [`osc.frequency.setValueAtTime(${safeFreq.toFixed(3)}, t)`]
        : [
            `osc.frequency.setTargetAtTime(${safeFreq.toFixed(3)}, t, ` +
              `${Math.max(0.008, seconds * 0.35).toFixed(4)})`,
          ],
    });
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
    this.delayNote = reader.meta.delay_note ?? '1/8.';
    // Played notes bypass the crusher by default -- see the routing in start().
    this.keyboardCrushed = false;
    this.soloVoice = null;
    this.mutes = new Set();
    // "Audio-vs-visual mapping balance" (4.5): audio's share of the weight.
    this.balance = 0.5;

    // A ring of recently scheduled events, for the console. Off by default:
    // eight voices at twelve frames a second is a hundred objects a second, and
    // nobody watching a piece needs them.
    this.journal = [];
    this.journalLimit = 400;
    this.journalEnabled = false;
  }

  /**
   * Start or stop journalling.
   *
   * Threaded down into the voices rather than wrapped around `update()`, so what
   * gets recorded is what was actually scheduled -- including the gate/slide
   * decision, which `update()` does not know about.
   */
  setJournalling(on) {
    this.journalEnabled = Boolean(on);
    const sink = this.journalEnabled ? this.journal : null;
    for (const voice of this.voices) voice.journal = sink;
    if (!this.journalEnabled) this.journal.length = 0;
  }

  _trimJournal() {
    const overflow = this.journal.length - this.journalLimit;
    if (overflow > 0) this.journal.splice(0, overflow);
  }

  /** A description of the effect chain, for the console's graph view. */
  graph() {
    if (!this.started) return null;
    return {
      sampleRate: this.ctx.sampleRate,
      state: this.ctx.state,
      baseLatency: this.ctx.baseLatency ?? null,
      nodes: [
        { id: 'voice[n]', type: 'OscillatorNode', detail: 'one per data voice, persistent' },
        { id: 'voice[n].filter', type: 'BiquadFilterNode', detail: 'lowpass, opens with intensity' },
        { id: 'voice[n].gain', type: 'GainNode', detail: 'per-event envelope' },
        { id: 'bus', type: 'GainNode', detail: 'where the data voices land' },
        { id: 'crusher', type: 'WaveShaperNode', detail: `quantizing curve, ${this.crushBits} bits, oversample none` },
        { id: 'tone', type: 'BiquadFilterNode', detail: `lowpass ${this.filterCutoff.toFixed(0)} Hz` },
        { id: 'delaySend', type: 'GainNode', detail: `mix ${this.delayMix.toFixed(2)}` },
        { id: 'delay', type: 'DelayNode', detail: `${this.delayNote} = ${(this.delaySeconds() * 1000).toFixed(0)} ms` },
        { id: 'feedback', type: 'GainNode', detail: '0.34, loops into delay' },
        { id: 'keyboardBus', type: 'GainNode', detail: 'played notes' },
        { id: 'keyClean', type: 'GainNode', detail: `post-crusher path (${this.keyboardCrushed ? 'off' : 'on'})` },
        { id: 'keyCrushed', type: 'GainNode', detail: `pre-crusher path (${this.keyboardCrushed ? 'on' : 'off'})` },
        { id: 'master', type: 'GainNode', detail: this.masterGain.toFixed(2) },
      ],
      edges: [
        'voice[n].osc -> filter -> gain -> level -> bus',
        'bus -> crusher -> tone -> master -> destination',
        'tone -> delaySend -> delay -> master',
        'delay -> feedback -> delay',
        'keyboardBus -> keyClean -> tone',
        'keyboardBus -> keyCrushed -> bus',
      ],
    };
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

    // Simple feedback delay, timed in note values rather than seconds: a dotted
    // eighth stays a dotted eighth when the author moves the BPM slider, which
    // is the whole reason tempo exists as a named thing.
    this.delay = ctx.createDelay(4.0);
    this.delay.delayTime.value = this.delaySeconds();
    this.feedback = ctx.createGain();
    this.feedback.gain.value = 0.34;
    this.delaySend = ctx.createGain();
    this.delaySend.gain.value = this.delayMix;

    this.bus = ctx.createGain(); // where the data voices land

    this.bus.connect(this.crusher);
    this.crusher.connect(this.tone);
    this.tone.connect(this.master);
    this.tone.connect(this.delaySend);
    this.delaySend.connect(this.delay);
    this.delay.connect(this.feedback);
    this.feedback.connect(this.delay); // the feedback loop
    this.delay.connect(this.master);
    this.master.connect(ctx.destination);

    // -- the keyboard's own path ------------------------------------------
    //
    // Played notes default to *after* the crusher, which is the one place this
    // deliberately breaks the project's "everything gets the same dirt" rule.
    // The point of playing along is to sit over the noise, and the intensity
    // envelope drops the crusher to three bits at the climax -- exactly when a
    // melody most needs to be audible. It still gets the filter and the
    // tempo-synced delay, so it stays in the same room.
    //
    // Both paths are wired permanently and switched by gain rather than by
    // re-patching: toggling a connection mid-note clicks, a gain ramp does not.
    this.keyboardBus = ctx.createGain();
    // Above unity, because WAVEFORM_GAIN normalizes every timbre downward from
    // sine. Applied once here rather than per note, where it would clip.
    this.keyboardBus.gain.value = 1.6;
    this.keyClean = ctx.createGain();
    this.keyCrushed = ctx.createGain();
    this.keyClean.gain.value = this.keyboardCrushed ? 0 : 1;
    this.keyCrushed.gain.value = this.keyboardCrushed ? 1 : 0;

    this.keyboardBus.connect(this.keyClean).connect(this.tone);
    this.keyboardBus.connect(this.keyCrushed).connect(this.bus);

    const audioVoices = this.reader.audio.voices ?? [];
    this.voices = audioVoices.map(
      (voice, index) => new Voice(ctx, this.bus, index, voice.waveform, voice.name),
    );
    // The voices only exist once start() runs, so a journalling flag set before
    // then has to be re-applied here or it would silently do nothing.
    if (this.journalEnabled) this.setJournalling(true);

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

    if (this.journalEnabled) this._trimJournal();

    // Global grit tracks intensity too: the doc wants aggression to rise into
    // the climax and back off at the close (5.1-A).
    if (this.crushBits >= 8) {
      const implied = Math.round(8 - intensity * 4.2);
      this.setCrush(Math.max(3, implied), false);
    }
  }

  /**
   * Play one note, now. The keyboard's entry point.
   *
   * A node per note here, unlike the data voices -- and for the opposite
   * reason. Data voices are eight continuous streams, so persistent oscillators
   * avoid constant allocation; played notes are short, sparse and polyphonic,
   * so spawning one per press is both simpler and correct. Ten fingers cannot
   * out-allocate a garbage collector.
   *
   * @param {number} midi      MIDI note number
   * @param {number} velocity  0..1
   * @param {object} options   {waveform, attack, decay, when}
   */
  /**
   * Play one note, now. Returns a handle so it can be released later.
   *
   * A node per note here, unlike the data voices -- and for the opposite
   * reason. Data voices are eight continuous streams, so persistent oscillators
   * avoid constant allocation; played notes are short, sparse and polyphonic,
   * so spawning one per press is both simpler and correct. Ten fingers cannot
   * out-allocate a garbage collector.
   *
   * **Sustain.** With `sustain > 0` the envelope decays to that level and holds
   * there until `release()` is called, which is what makes holding a key hold a
   * note. With `sustain = 0` it decays to silence and stops itself -- the
   * original percussive bleep, still available because a bleep is a legitimate
   * choice rather than a limitation.
   *
   * A held note that is never released would run forever, so there are two
   * backstops: a hard ceiling on sustain length, and the caller releasing on
   * window blur (alt-tabbing mid-chord otherwise leaves a drone behind).
   *
   * @param {number} midi      MIDI note number
   * @param {number} velocity  0..1
   * @param {object} options   {waveform, attack, decay, sustain, release, when}
   * @returns {null|{midi, freq, release: Function, released: boolean}}
   */
  playNote(midi, velocity = 0.7, options = {}) {
    if (!this.started) return null;
    const ctx = this.ctx;
    const when = options.when ?? ctx.currentTime + 0.004;
    const attack = options.attack ?? 0.003;
    const decay = options.decay ?? 0.22;
    const sustain = Math.max(0, Math.min(1, options.sustain ?? 0));
    const releaseTime = Math.max(0.01, options.release ?? 0.12);
    const freq = midiToHz(midi);

    const osc = ctx.createOscillator();
    osc.type = options.waveform ?? 'square';
    osc.frequency.value = Math.max(18, Math.min(ctx.sampleRate / 2.2, freq));

    // Timbre compensation, so switching waveform changes the colour and not the
    // volume. See WAVEFORM_GAIN.
    const peak = Math.max(0, Math.min(1, velocity)) * (WAVEFORM_GAIN[osc.type] ?? 0.6);
    const sustainLevel = peak * sustain;

    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0, when);
    gain.gain.linearRampToValueAtTime(peak, when + attack);

    let end = null;
    if (sustainLevel > 0.0005) {
      // Decay toward the sustain level and stay. setTargetAtTime approaches
      // asymptotically, which is exactly right for a hold: it never schedules an
      // end, so there is nothing to cancel when the key comes up.
      gain.gain.setTargetAtTime(sustainLevel, when + attack, Math.max(0.01, decay * 0.4));
      end = when + attack + MAX_SUSTAIN_SECONDS;
    } else {
      // No sustain: decay to silence and stop. One linear ramp to a hard zero at
      // the end, because setTargetAtTime alone never reaches its target and the
      // node could not be stopped cleanly.
      gain.gain.setTargetAtTime(0.0001, when + attack, Math.max(0.01, decay * 0.4));
      end = when + attack + decay + 0.05;
      gain.gain.linearRampToValueAtTime(0, end);
    }

    osc.connect(gain).connect(this.keyboardBus);
    osc.start(when);
    osc.stop(end);

    const handle = {
      midi,
      freq,
      when,
      released: sustainLevel <= 0.0005,
      release: (at) => {
        if (handle.released) return;
        handle.released = true;
        const time = Math.max(at ?? ctx.currentTime, when + attack);
        try {
          gain.gain.cancelScheduledValues(time);
          // Anchored at the value it actually has: without this the ramp starts
          // from whatever the last scheduled event said, which after a
          // setTargetAtTime is not where the signal is.
          gain.gain.setValueAtTime(gain.gain.value, time);
          gain.gain.linearRampToValueAtTime(0, time + releaseTime);
          osc.stop(time + releaseTime + 0.02);
        } catch {
          // The context can be closed, or the note already stopped, mid-release.
        }
      },
    };

    if (this.journalEnabled) {
      this.journal.push({
        time: when,
        voice: -1, // not a data voice: this one came from a key
        name: 'keyboard',
        waveform: osc.type,
        freq,
        amp: peak,
        attack,
        hold: sustainLevel > 0 ? releaseTime : decay,
        sustain: sustainLevel,
        retrigger: true,
        midi,
        calls: [
          `osc.type = '${osc.type}'`,
          `osc.frequency.value = ${freq.toFixed(3)}`,
          `gain.gain.linearRampToValueAtTime(${peak.toFixed(3)}, t + ${attack})`,
          sustainLevel > 0
            ? `gain.gain.setTargetAtTime(${sustainLevel.toFixed(3)}, t + ${attack}, ...)  // holds`
            : `gain.gain.setTargetAtTime(0, t + ${attack}, ...)  // decays`,
        ],
      });
      this._trimJournal();
    }

    osc.onended = () => {
      try {
        gain.disconnect();
        osc.disconnect();
      } catch {
        // Already torn down -- a context close races with the last few notes.
      }
    };
    return handle;
  }

  /**
   * A MediaStream carrying the master output, for recording.
   *
   * Hung off `master` rather than off `destination`, because there is no way to
   * tap the destination -- and created lazily and kept, because connecting a
   * second MediaStreamAudioDestinationNode on every take would leave the old
   * ones attached to the graph, quietly summing.
   */
  recordingStream() {
    if (!this.started) return null;
    if (!this._tap) {
      this._tap = this.ctx.createMediaStreamDestination();
      this.master.connect(this._tap);
    }
    return this._tap.stream;
  }

  /** Route played notes through the bitcrusher, or around it. */
  setKeyboardCrushed(crushed) {
    this.keyboardCrushed = crushed;
    if (!this.keyClean) return;
    const now = this.currentTime;
    this.keyClean.gain.setTargetAtTime(crushed ? 0 : 1, now, 0.02);
    this.keyCrushed.gain.setTargetAtTime(crushed ? 1 : 0, now, 0.02);
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

  /** Delay time in seconds for the current note value and tempo. */
  delaySeconds() {
    return Math.min(3.9, this.reader.tempo.noteSeconds(this.delayNote));
  }

  /** Re-time the delay -- called when the note value or the tempo changes. */
  syncDelay() {
    if (!this.delay) return;
    // A ramp, not a jump: retiming a delay line instantaneously pitches whatever
    // is still echoing inside it, which sounds like a fault rather than a choice.
    this.delay.delayTime.setTargetAtTime(this.delaySeconds(), this.currentTime, 0.08);
  }

  setDelayNote(note) {
    this.delayNote = note;
    this.syncDelay();
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
