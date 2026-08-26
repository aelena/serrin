/**
 * The transport: one clock, two consumers.
 *
 * This is the piece of section 4.2 that everything else hangs off. The structure
 * the doc sketches is a Web-Audio-style lookahead scheduler for sound and
 * requestAnimationFrame for pictures, both reading the same frame state. That is
 * exactly what this does, with one addition that turned out to matter: the frame
 * counter is owned here and *derived from the AudioContext clock*, never from
 * rAF timestamps.
 *
 * The reason is drift. rAF ticks stop when the tab is backgrounded and stutter
 * under load; the audio clock does neither. If visuals owned the counter, a
 * dropped frame would silently desynchronise the sound. This way the audio clock
 * is the metronome, the visual loop is a viewer onto it, and a janky frame costs
 * a picture rather than a beat.
 */

const LOOKAHEAD_SECONDS = 0.12;
const SCHEDULER_INTERVAL_MS = 25;
/** Frames allowed to wait for the visual loop before the oldest are dropped. */
const PENDING_LIMIT = 240;

export class Transport {
  /**
   * @param {import('./reader.js').Reader} reader
   * @param {import('./audio.js').AudioEngine} audioEngine
   * @param {import('./visual.js').VisualEngine} visualEngine
   * @param {object} hooks {intensityAt(counter), onFrame(frame,state), onEnd()}
   */
  constructor(reader, audioEngine, visualEngine, hooks = {}) {
    this.reader = reader;
    this.audio = audioEngine;
    this.visual = visualEngine;
    this.hooks = hooks;

    this.playing = false;
    this.counter = 0; // frames scheduled so far, monotonic across loops
    this.startTime = 0; // AudioContext time at which frame 0 sounds
    this.pass = 0;
    this.finished = false;

    // For mode A: how long the piece is in frames. Beyond the data's own length
    // when the loop policy extends it -- see targetFrames().
    this.targetFrames = reader.length;
    this.lastFrame = null;
    this.lastState = { intensity: 1, gates: null };

    this._schedulerId = null;
    this._rafId = null;
  }

  /** Total piece length in frames, honouring the duration policy (5.1.2). */
  setDurationSeconds(seconds) {
    if (!seconds || seconds <= 0) {
      this.targetFrames = this.reader.length; // "at the data's mercy"
      return;
    }
    this.targetFrames = Math.round(seconds * this.reader.rate * this.reader.speed);
  }

  async play() {
    if (this.playing) return;
    await this.audio.start();
    // Anchor the timeline so frame `counter` lands at a known clock time. On a
    // resume this re-anchors rather than resetting, so the counter continues.
    this.startTime = this.audio.currentTime + 0.08 - this.reader.frameOnset(this.counter);
    this.playing = true;
    this.finished = false;

    this._schedulerId = setInterval(() => this._schedule(), SCHEDULER_INTERVAL_MS);
    this._schedule();
    this._startRenderLoop();
  }

  pause() {
    this.playing = false;
    clearInterval(this._schedulerId);
    this._schedulerId = null;
    this.audio.silenceAll();
  }

  async toggle() {
    if (this.playing) this.pause();
    else await this.play();
    return this.playing;
  }

  stop() {
    this.pause();
    this.counter = 0;
    this.pass = 0;
    this.finished = false;
    this.visual.clear();
    this.lastFrame = null;
  }

  /**
   * Re-anchor the timeline after the grid changes underneath it.
   *
   * `startTime` fixes where frame 0 sounds, and every other onset is measured
   * from it -- so moving the BPM slider silently moves every future onset,
   * including ones already inside the lookahead window. Re-anchoring pins the
   * *current* frame where it is and lets the new grid apply from there, which is
   * what "change the tempo" is supposed to feel like. Without it the transport
   * either stalls (onsets jumped forwards) or dumps a burst of frames at once
   * (onsets jumped backwards).
   */
  retime() {
    if (!this.audio.started) return;
    this.startTime = this.audio.currentTime - this.reader.frameOnset(this.counter);
    this.audio.syncDelay();
  }

  seekNormalized(position) {
    const target = Math.max(0, Math.round(position * this.targetFrames));
    this.counter = target;
    this.startTime = this.audio.currentTime + 0.08 - this.reader.frameOnset(this.counter);
    this.visual.clear();
  }

  /** Where the piece is, 0..1, or null in endless mode (which has no "where"). */
  progress() {
    if (this.reader.mode === 'endless' || !this.targetFrames) return null;
    return Math.min(1, this.counter / this.targetFrames);
  }

  // -- the lookahead loop ---------------------------------------------------
  _schedule() {
    if (!this.playing || !this.audio.started) return;
    const horizon = this.audio.currentTime + LOOKAHEAD_SECONDS;

    while (this.startTime + this.reader.frameOnset(this.counter) < horizon) {
      const when = this.startTime + this.reader.frameOnset(this.counter);
      const frame = this.reader.frame(this.counter);

      if (!frame) {
        // A `once` stream has run out. Silence, then report it -- the doc leaves
        // this open (4.2); silence is the choice that does not pretend.
        this._finish();
        return;
      }

      // In a closed piece, the target duration ends the piece even if the loop
      // policy would happily keep going.
      if (this.reader.mode !== 'endless' && this.counter >= this.targetFrames) {
        this._finish();
        return;
      }

      const state = this._stateFor(this.counter, frame);
      this.audio.update(frame, when, state);
      // Visual state is handed over at schedule time but *rendered* on the rAF
      // loop; the queue below keeps it from appearing early.
      this._enqueueVisual(frame, state, when);

      this.pass = frame.pass;
      this.lastFrame = frame;
      this.lastState = state;
      this.counter += 1;
    }
  }

  _stateFor(counter, frame) {
    const intensity = this.hooks.intensityAt
      ? this.hooks.intensityAt(counter, this.targetFrames, frame)
      : 1;
    const gates = this.hooks.gatesFor ? this.hooks.gatesFor(intensity) : null;
    return { intensity, gates, counter };
  }

  _enqueueVisual(frame, state, when) {
    if (!this._pending) this._pending = [];
    this._pending.push({ frame, state, when });

    // Backgrounded tabs are the reason for this cap. Browsers throttle
    // requestAnimationFrame to a crawl -- and suspend it outright on a hidden
    // tab -- while the AudioContext clock keeps running, so the scheduler keeps
    // filling this queue with nobody draining it. An installation left running
    // behind another window would grow it without bound.
    //
    // Sound is authoritative and pictures are transient, so the oldest frames
    // are dropped rather than the newest: on coming back to the tab you want
    // the present, not a fast-forward through the backlog.
    const overflow = this._pending.length - PENDING_LIMIT;
    if (overflow > 0) this._pending.splice(0, overflow);
  }

  _startRenderLoop() {
    if (this._rafId !== null) return;
    const step = (now) => {
      this._rafId = requestAnimationFrame(step);
      // Release any frame whose audio onset has arrived. This is what keeps the
      // picture on the sound rather than ahead of it by the lookahead window.
      if (this._pending?.length && this.audio.started) {
        const clock = this.audio.currentTime;
        while (this._pending.length && this._pending[0].when <= clock) {
          const { frame, state } = this._pending.shift();
          this.visual.update(frame, state);
          this.hooks.onFrame?.(frame, state);
        }
      }
      this.visual.render(now, {
        pass: this.pass,
        progress: this.progress(),
        keyboard: this.hooks.keyboardArmed?.() ?? false,
      });
    };
    this._rafId = requestAnimationFrame(step);
  }

  _finish() {
    this.pause();
    this.finished = true;
    this.hooks.onEnd?.();
  }
}
