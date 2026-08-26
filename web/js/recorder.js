/**
 * Recording: a file you can send to someone who does not have serrin.
 *
 * Two ways to get audio out of a Web Audio graph, and they answer different
 * questions.
 *
 * `MediaRecorder` — used here — taps the live output and captures **exactly what
 * you heard**, including anything played on the keyboard and any envelope drawn
 * mid-piece. It runs in real time (a five-minute piece takes five minutes) and
 * produces WebM/Opus, which is lossy. It can also record the canvas, so the
 * audiovisual loop comes out as one file with both tracks in sync.
 *
 * `OfflineAudioContext` would render faster than real time, deterministically,
 * to uncompressed samples — a clean master. But it renders a *graph*, not a
 * performance: it cannot capture live playing, because there is no live. It also
 * means rebuilding the whole node graph a second time, which is a second place
 * for the sound to drift from what the piece actually is.
 *
 * Given that the keyboard and the live stroke are the two features that make
 * serrin a performance tool rather than a batch renderer, capturing the
 * performance is the more honest default. The offline master is the upgrade
 * path, not the thing to build first.
 */

/** Codec preferences, best first. Browsers disagree about all of these. */
const AUDIO_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
];

const VIDEO_TYPES = [
  'video/webm;codecs=vp9,opus',
  'video/webm;codecs=vp8,opus',
  'video/webm',
  'video/mp4',
];

function pickType(candidates) {
  if (typeof MediaRecorder === 'undefined') return null;
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) ?? null;
}

export function support() {
  if (typeof MediaRecorder === 'undefined') {
    return { audio: null, video: null, available: false };
  }
  const audio = pickType(AUDIO_TYPES);
  const video = pickType(VIDEO_TYPES);
  return { audio, video, available: Boolean(audio) };
}

function extensionFor(mimeType) {
  if (!mimeType) return 'bin';
  if (mimeType.startsWith('audio/ogg')) return 'ogg';
  if (mimeType.includes('mp4')) return mimeType.startsWith('audio/') ? 'm4a' : 'mp4';
  return 'webm';
}

export class Recorder {
  /**
   * @param {import('./audio.js').AudioEngine} audio
   * @param {HTMLCanvasElement} canvas
   */
  constructor(audio, canvas) {
    this.audio = audio;
    this.canvas = canvas;
    this.recorder = null;
    this.chunks = [];
    this.startedAt = 0;
    this.mimeType = null;
    this.withVideo = false;
    this.fps = 30;
    this.onStateChange = null;
    this.lastError = null;
  }

  get recording() {
    return this.recorder?.state === 'recording';
  }

  get elapsed() {
    return this.recording ? (performance.now() - this.startedAt) / 1000 : 0;
  }

  /** Bytes captured so far. Approximate -- chunks arrive on a timer. */
  get size() {
    return this.chunks.reduce((total, chunk) => total + chunk.size, 0);
  }

  /**
   * Begin recording. The audio engine must already be started, because the tap
   * hangs off its master node and there is no master before then.
   */
  start({ video = false, fps = 30 } = {}) {
    if (this.recording) return false;
    this.lastError = null;

    if (!this.audio.started) {
      this.lastError = 'press play first — there is no audio graph to record yet';
      return false;
    }
    const available = support();
    if (!available.available) {
      this.lastError = 'this browser has no MediaRecorder';
      return false;
    }

    const stream = this.audio.recordingStream();
    if (!stream) {
      this.lastError = 'could not tap the audio output';
      return false;
    }

    this.withVideo = Boolean(video && available.video && this.canvas?.captureStream);
    this.fps = fps;

    let source = stream;
    if (this.withVideo) {
      // One MediaStream carrying both tracks, so the muxer keeps them in sync.
      // Built by hand rather than by recording the canvas stream and adding
      // audio: captureStream() gives a video-only stream and the audio tap gives
      // an audio-only one, and MediaRecorder wants a single stream.
      const canvasStream = this.canvas.captureStream(fps);
      source = new MediaStream([...canvasStream.getVideoTracks(), ...stream.getAudioTracks()]);
      this._canvasStream = canvasStream;
    }

    this.mimeType = this.withVideo ? available.video : available.audio;
    try {
      this.recorder = new MediaRecorder(source, {
        mimeType: this.mimeType,
        audioBitsPerSecond: 192000,
      });
    } catch (error) {
      this.lastError = `MediaRecorder refused ${this.mimeType}: ${error.message}`;
      return false;
    }

    this.chunks = [];
    this.recorder.ondataavailable = (event) => {
      if (event.data?.size) this.chunks.push(event.data);
    };
    this.recorder.onerror = (event) => {
      this.lastError = String(event.error?.message ?? 'recording failed');
      this.onStateChange?.();
    };
    this.recorder.onstop = () => this._finish();

    // A one-second timeslice, so `size` moves while recording and a crash costs
    // one second rather than the whole take.
    this.recorder.start(1000);
    this.startedAt = performance.now();
    this.onStateChange?.();
    return true;
  }

  stop() {
    if (!this.recording) return false;
    this.recorder.stop();
    return true;
  }

  _finish() {
    const seconds = this.elapsed || (performance.now() - this.startedAt) / 1000;
    const blob = new Blob(this.chunks, { type: this.mimeType ?? 'application/octet-stream' });
    this._canvasStream?.getTracks().forEach((track) => track.stop());
    this._canvasStream = null;

    this.lastTake = {
      blob,
      seconds,
      mimeType: this.mimeType,
      extension: extensionFor(this.mimeType),
      size: blob.size,
    };
    this.recorder = null;
    this.onStateChange?.();
  }

  /** Hand the last take to the browser as a download. */
  download(label = 'serrin') {
    if (!this.lastTake) return null;
    const safe = String(label).replace(/[^A-Za-z0-9._+-]+/g, '_').slice(0, 80);
    const url = URL.createObjectURL(this.lastTake.blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${safe}.${this.lastTake.extension}`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    return link.download;
  }

  describe() {
    if (this.lastError) return this.lastError;
    if (this.recording) {
      const kind = this.withVideo ? 'audio + video' : 'audio';
      return `recording ${kind} — ${this.elapsed.toFixed(1)}s, ${(this.size / 1048576).toFixed(2)} MB`;
    }
    if (this.lastTake) {
      return (
        `last take: ${this.lastTake.seconds.toFixed(1)}s, ` +
        `${(this.lastTake.size / 1048576).toFixed(2)} MB, ${this.lastTake.mimeType}`
      );
    }
    const available = support();
    if (!available.available) return 'this browser cannot record';
    return available.video
      ? 'ready — audio, or audio and video together'
      : 'ready — audio only (no video codec here)';
  }
}
