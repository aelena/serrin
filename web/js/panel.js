/**
 * The author panel (section 4.5).
 *
 * Explicitly *not* part of the piece: it starts hidden, it is reached with `p`
 * or `?panel=1`, and hiding it changes nothing about what is playing. It lives
 * in the same document as the stage rather than in a second window, because
 * live envelope drawing needs to be on the engine's clock -- section 5.1 calls
 * that out as an architectural consequence, and a second process would have to
 * fake it.
 *
 * One control from 4.5 is honestly incomplete: reordering and toggling
 * individual pedals *on the fly*. The chain is rendered offline by the Python
 * pipeline in phase 1, so the pedal list here is read-only and shows which
 * pedals the current intensity has notionally switched on. Live pedal
 * manipulation needs the chain ported to JS (roadmap step 5).
 */

import {
  Envelope,
  StrokeRecorder,
  envelopeFromArchetype,
  envelopeFromEquation,
} from './envelope.js';
import { MODES, REGISTERS, noteName } from './keyboard.js';
import {
  apply as applySession,
  autosave,
  clearAutosave,
  downloadPreset,
  downloadSession,
  downloadStreams,
  loadAutosave,
  readSessionFile,
} from './session.js';
import { describeRender, readTextFile, requestRender } from './source.js';
import { Tempo } from './tempo.js';

const $ = (id) => document.getElementById(id);

export class Panel {
  constructor(app) {
    this.app = app; // the live engine handle from main.js
    this.root = $('panel');
    // Re-appliers for every slider and checkbox, so a preset switch can push the
    // author's current settings into the freshly built engines.
    this._appliers = [];
    this._wire();
    this.refresh();
  }

  /**
   * Point the panel at a newly loaded piece.
   *
   * Constructed once and refreshed, never rebuilt: the controls live in the
   * document, so a second Panel would bind a second listener to every one of
   * them and a second StrokeRecorder to the envelope canvas. Handlers reach the
   * engines through `this.app`, whose properties main.js replaces on load, so
   * the existing wiring keeps working against the new objects.
   */
  refresh() {
    const app = this.app;
    $('ctl-mode').value = app.reader.mode;
    $('ctl-loop').value = app.reader.loopPolicy;
    $('ctl-entry').value = app.entryStrategy;
    // Speed goes the other way: the reader may already carry a ?speed= from the
    // URL, and re-applying a stale slider position would silently discard it.
    $('ctl-speed').value = String(app.reader.speed);
    this._syncTempoControls();
    // Everything else re-applies the slider positions the author is looking at;
    // a new AudioEngine starts on its own defaults and would otherwise ignore
    // them.
    for (const apply of this._appliers) apply();
    this._paintFacts();
    this._paintVoices();
    this._paintPedals();
    this._paintTempo();
    this._paintKeyboard();
    this._paintEnvelope();
    this._paintSession();
    this._paintRecord();
  }

  /** Push the loaded piece's tempo into the controls without firing handlers. */
  _syncTempoControls() {
    const tempo = this.app.reader.tempo;
    $('ctl-bpm').value = String(tempo.bpm);
    $('out-bpm').textContent = tempo.bpm.toFixed(1);
    $('ctl-swing').value = String(tempo.swing);
    $('out-swing').textContent = tempo.swing.toFixed(2);
    $('ctl-subdivision').value = String(tempo.subdivision);
    $('ctl-beats-per-bar').value = String(tempo.beatsPerBar);
    $('ctl-delay-note').value = this.app.audio.delayNote;
  }

  /** Scale, pool size and last note played -- everything the player needs. */
  _sourceMessage(text, warning = false) {
    const node = $('source-readout');
    node.textContent = text;
    node.style.color = warning ? '#ff9d5c' : '';
  }

  _paintRecord() {
    const recorder = this.app.recorder;
    if (!recorder) return;
    $('record-readout').textContent = recorder.describe();
    const button = $('ctl-record');
    button.textContent = recorder.recording ? 'stop' : 'record';
    button.classList.toggle('on', recorder.recording);
    $('ctl-record-save').disabled = !recorder.lastTake || recorder.recording;
  }

  /** One line of feedback for the session buttons. Warnings read differently. */
  _sessionMessage(text, warning = false) {
    const node = $('session-readout');
    node.textContent = text;
    node.style.color = warning ? '#ff9d5c' : '';
    this._sessionMessageAt = performance.now();
  }

  _paintSession() {
    if (this._sessionMessageAt) return; // a real message outranks the summary
    const meta = this.app.reader.meta ?? {};
    $('session-readout').textContent =
      `render ${meta.fingerprint ?? '—'} · seed ${meta.seed ?? '—'}`;
  }

  _paintKeyboard() {
    const keyboard = this.app.keyboard;
    const state = keyboard.enabled ? (keyboard.ready ? 'live' : 'mode not implemented') : 'off';
    const last = keyboard.lastNote === null ? '—' : noteName(keyboard.lastNote);
    const octave = keyboard.octaveOffset
      ? ` · ↑↓ octave ${keyboard.octaveOffset > 0 ? '+' : ''}${keyboard.octaveOffset}`
      : '';
    $('keyboard-readout').textContent =
      `${state} · ${keyboard.describe()} · last ${last}${octave}`;
    $('ctl-keyboard-mode').value = keyboard.mode;
    $('ctl-keyboard-register').value = keyboard.register;
  }

  _paintTempo() {
    const app = this.app;
    const tempo = app.reader.tempo;
    const delaySeconds = tempo.noteSeconds(app.audio.delayNote);
    $('tempo-readout').textContent =
      `${tempo.describe()} · ${app.reader.bars.toFixed(1)} bars · ` +
      `delay ${app.audio.delayNote} = ${(delaySeconds * 1000).toFixed(0)} ms`;
  }

  get visible() {
    // Derived: the state machine decides, and it refuses the panel anywhere but
    // the stage. Reading it from there means the panel cannot believe itself
    // visible while hidden, which is how the old flag drifted.
    return this.app.views.snapshot().panel;
  }

  toggle(force) {
    const shown = this.app.views.setOverlay('panel', force ?? !this.visible);
    if (shown) this._paintEnvelope();
    return shown;
  }

  // -- wiring --------------------------------------------------------------
  _wire() {
    const app = this.app;

    $('panel-close').addEventListener('click', () => this.toggle(false));
    $('ctl-to-studio').addEventListener('click', () => {
      // Design time is quiet: going back to configure stops the piece rather
      // than leaving it playing under a screen full of settings.
      app.transport?.pause();
      $('ctl-play').textContent = 'play';
      app.studio?.enter();
    });

    $('ctl-play').addEventListener('click', async () => {
      const playing = await app.transport.toggle();
      $('ctl-play').textContent = playing ? 'pause' : 'play';
    });
    $('ctl-stop').addEventListener('click', () => {
      app.transport.stop();
      $('ctl-play').textContent = 'play';
    });

    // -- presets: re-fetch a different render. Switching preset in phase 1 means
    // loading a different pair of exported streams, not re-running the chain.
    const presetSelect = $('ctl-preset');
    presetSelect.replaceChildren();
    for (const preset of app.presets) {
      const option = document.createElement('option');
      option.value = preset.id;
      option.textContent = preset.id;
      presetSelect.append(option);
    }
    presetSelect.value = app.presetId ?? app.presets[0]?.id ?? '';
    presetSelect.addEventListener('change', () => app.loadPreset(presetSelect.value));

    // -- source. Renders happen in Python, so every button here is a request.
    const sourcePreset = $('ctl-source-preset');
    sourcePreset.replaceChildren();
    for (const preset of app.presets) {
      const option = document.createElement('option');
      option.value = preset.id;
      option.textContent = preset.id;
      sourcePreset.append(option);
    }
    sourcePreset.value = app.presetId ?? app.presets[0]?.id ?? '';

    const runRender = async (request, label) => {
      this._sourceMessage(`rendering ${label}…`);
      try {
        const result = await requestRender({
          preset: sourcePreset.value,
          trace: $('ctl-source-trace').checked,
          ...request,
        });
        this._sourceMessage(describeRender(result));
        // Adopted as a preset entry so it can be switched back to later, and so
        // the reload path is the one that already works.
        await app.adoptRender(result);
        this.refresh();
      } catch (error) {
        this._sourceMessage(String(error.message ?? error), true);
      }
    };

    $('ctl-console-open').addEventListener('click', () => app.console?.toggle(true));
    $('ctl-studio-open').addEventListener('click', () => {
      app.transport?.pause();
      $('ctl-play').textContent = 'play';
      app.studio?.enter();
    });

    $('ctl-source-csv').addEventListener('click', () => $('ctl-source-file').click());
    $('ctl-source-file').addEventListener('change', async (event) => {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (!file) return;
      try {
        const csv = await readTextFile(file);
        await runRender({ csv, name: file.name }, file.name);
      } catch (error) {
        this._sourceMessage(String(error.message ?? error), true);
      }
    });

    $('ctl-source-render').addEventListener('click', async () => {
      const repo = $('ctl-source-repo').value.trim();
      if (!repo) {
        this._sourceMessage('type a repository path first', true);
        return;
      }
      await runRender(
        {
          repo,
          metric: $('ctl-source-metric').value,
          traversal: $('ctl-source-traversal').value,
        },
        repo,
      );
    });

    // -- record
    $('ctl-record').addEventListener('click', () => {
      const recorder = app.recorder;
      if (recorder.recording) {
        recorder.stop();
        return;
      }
      const started = recorder.start({ video: $('ctl-record-video').checked });
      if (!started) this._paintRecord();
    });
    $('ctl-record-save').addEventListener('click', () => {
      const name = app.recorder.download(app.reader.meta?.label ?? 'serrin');
      if (name) this._sessionMessage(`saved ${name}`);
    });

    // -- session. The one place the render/runtime seam is visible to the
    // author, so the buttons are named for which half they keep.
    $('ctl-session-save').addEventListener('click', () => {
      const session = downloadSession(app, $('ctl-session-notes').value);
      this._sessionMessage(`saved ${session.label || 'session'}`);
      app.console?.log(`session saved: ${session.label}`, 'system');
    });

    $('ctl-session-load').addEventListener('click', () => $('ctl-session-file').click());
    $('ctl-session-file').addEventListener('change', async (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      try {
        const report = applySession(app, await readSessionFile(file));
        app.console?.log(
          `session loaded: ${file.name} — applied ${report.applied.join(', ') || 'nothing'}`,
          report.warnings.length ? 'warn' : 'system',
        );
        for (const warning of report.warnings) app.console?.log(warning, 'warn');
        this.refresh();
        this._sessionMessage(
          [
            `loaded ${file.name}: ${report.applied.join(', ') || 'nothing'}`,
            ...report.warnings,
          ].join(' — '),
          report.warnings.length > 0,
        );
      } catch (error) {
        this._sessionMessage(String(error.message ?? error), true);
      } finally {
        // Cleared so picking the same file twice fires the change event again.
        event.target.value = '';
      }
    });

    $('ctl-session-preset').addEventListener('click', () => {
      const preset = downloadPreset(app);
      this._sessionMessage(
        `froze ${preset.name} — levels, mutes, visuals and keyboard are NOT in a ` +
          'preset; keep the session for those',
      );
    });

    $('ctl-session-streams').addEventListener('click', () => {
      const label = downloadStreams(app);
      this._sessionMessage(`downloaded ${label}.audio.json + .visual.json`);
    });

    $('ctl-session-restore').addEventListener('click', () => {
      const saved = loadAutosave();
      if (!saved) {
        this._sessionMessage('no autosave in this browser', true);
        return;
      }
      const report = applySession(app, saved);
      this.refresh();
      this._sessionMessage(`restored autosave: ${report.applied.join(', ')}`);
    });

    this._check('ctl-session-autosave', (on) => {
      this.autosaveEnabled = on;
      if (!on) clearAutosave();
    });

    // -- keyboard. The one part of serrin that is performed rather than
    // generated, so it is off by default: a piece should not change behaviour
    // because someone leaned on the space bar.
    const modeSelect = $('ctl-keyboard-mode');
    modeSelect.replaceChildren();
    for (const [id, mode] of Object.entries(MODES)) {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = mode.label;
      // The planned modes are listed so the shape is visible, and disabled so
      // nothing pretends to work.
      option.disabled = !mode.ready;
      modeSelect.append(option);
    }
    modeSelect.value = app.keyboard.mode;
    modeSelect.addEventListener('change', (e) => {
      app.keyboard.setMode(e.target.value);
      this._paintKeyboard();
    });

    const registerSelect = $('ctl-keyboard-register');
    registerSelect.replaceChildren();
    for (const [id, register] of Object.entries(REGISTERS)) {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = register.label;
      registerSelect.append(option);
    }
    registerSelect.value = app.keyboard.register;
    registerSelect.addEventListener('change', (e) => {
      app.keyboard.setRegister(e.target.value);
      this._paintKeyboard();
    });

    this._check('ctl-keyboard', (on) => {
      app.keyboard.enabled = on;
      if (!on) app.keyboard.panic();
      this._paintKeyboard();
    });
    this._range('ctl-keyboard-level', 'out-keyboard-level', (v) => {
      app.keyboard.level = v;
      return v.toFixed(2);
    });
    this._range('ctl-keyboard-sustain', 'out-keyboard-sustain', (v) => {
      // 0 is the original percussive bleep, kept reachable: a bleep is a choice.
      app.keyboard.sustain = v;
      return v < 0.005 ? 'off (bleep)' : v.toFixed(2);
    });
    $('ctl-keyboard-wave').addEventListener('change', (e) => {
      app.keyboard.waveform = e.target.value;
    });
    this._check('ctl-keyboard-crush', (on) => app.audio.setKeyboardCrushed(on));
    this._check('ctl-keyboard-show', (on) => {
      app.keyboard.showKeys = on;
      app.visual.showKeys = on;
    });

    // -- tempo. Every control here goes through app.setTempo, which re-anchors
    // the transport; changing the grid without that makes the scheduler either
    // stall or dump a burst of frames.
    this._range('ctl-bpm', 'out-bpm', (v) => {
      app.setTempo(app.reader.tempo.with({ bpm: v }));
      this._paintTempo();
      return `${v.toFixed(1)}`;
    });
    this._range('ctl-swing', 'out-swing', (v) => {
      app.setTempo(app.reader.tempo.with({ swing: v }));
      this._paintTempo();
      return v.toFixed(2);
    });
    $('ctl-subdivision').addEventListener('change', (e) => {
      app.setTempo(app.reader.tempo.with({ subdivision: Number(e.target.value) }));
      this._paintTempo();
    });
    $('ctl-beats-per-bar').addEventListener('change', (e) => {
      app.setTempo(app.reader.tempo.with({ beatsPerBar: Number(e.target.value) }));
      this._paintTempo();
    });
    $('ctl-delay-note').addEventListener('change', (e) => {
      app.audio.setDelayNote(e.target.value);
      this._paintTempo();
    });
    $('ctl-tempo-reset').addEventListener('click', () => {
      app.setTempo(Tempo.fromMeta(app.reader.meta));
      this._syncTempoControls();
      this._paintTempo();
    });

    // -- clock
    this._range('ctl-speed', 'out-speed', (v) => {
      app.reader.speed = v;
      return `${v.toFixed(2)}×`;
    });

    const position = $('ctl-position');
    position.addEventListener('input', () => {
      app.transport.seekNormalized(Number(position.value));
    });

    $('ctl-mode').addEventListener('change', (e) => {
      app.reader.mode = e.target.value;
      this._paintEnvelope();
    });

    $('ctl-loop').addEventListener('change', (e) => {
      app.reader.loopPolicy = e.target.value;
    });

    $('ctl-duration').addEventListener('change', (e) => {
      app.transport.setDurationSeconds(Number(e.target.value));
      this._paintFacts();
    });

    // -- envelope
    this.strokeRecorder = new StrokeRecorder($('envelope-canvas'), (envelope) => {
      app.setEnvelope(envelope);
      this._paintEnvelope();
    });
    $('envelope-canvas').addEventListener('stroke:move', () => this._paintEnvelope());
    $('ctl-pressure').addEventListener('change', (e) => {
      this.strokeRecorder.usePressure = e.target.checked;
    });
    $('ctl-curve-apply').addEventListener('click', () => {
      const [kind, name] = $('ctl-curve').value.split(':');
      app.setEnvelope(
        kind === 'ar' ? envelopeFromArchetype(name) : envelopeFromEquation(name),
      );
      this.strokeRecorder.clear();
      this._paintEnvelope();
    });
    $('ctl-curve-baked').addEventListener('click', () => {
      app.setEnvelope(Envelope.fromExport(app.reader.audio.envelope));
      this.strokeRecorder.clear();
      this._paintEnvelope();
    });
    $('ctl-curve-export').addEventListener('click', () => this._exportEnvelope());

    // -- voices
    $('ctl-entry').addEventListener('change', (e) => {
      app.setEntryStrategy(e.target.value);
      this._paintVoices();
    });

    // -- balance and grit
    this._range('ctl-balance', 'out-balance', (v) => {
      app.audio.balance = v;
      app.visual.balance = v;
      return v.toFixed(2);
    });
    this._range('ctl-master', 'out-master', (v) => {
      app.audio.setMaster(v);
      return v.toFixed(2);
    });
    this._range('ctl-crush', 'out-crush', (v) => {
      // 8 hands control back to the intensity envelope; anything else pins it.
      app.audio.setCrush(v);
      return v >= 8 ? 'auto' : `${v} bit`;
    });
    this._range('ctl-cutoff', 'out-cutoff', (v) => {
      app.audio.setCutoff(v);
      return `${v.toFixed(0)} Hz`;
    });
    this._range('ctl-delay', 'out-delay', (v) => {
      app.audio.setDelayMix(v);
      return v.toFixed(2);
    });
    this._range('ctl-corruption', 'out-corruption', (v) => {
      app.visual.corruption = v;
      return v.toFixed(2);
    });

    this._check('ctl-glyphs', (on) => { app.visual.showGlyphs = on; });
    this._check('ctl-bars', (on) => { app.visual.showBars = on; });
    this._check('ctl-banding', (on) => { app.visual.showBanding = on; });
    this._check('ctl-invert', (on) => { app.visual.invert = on; });
  }

  _range(inputId, outputId, apply) {
    const input = $(inputId);
    const output = $(outputId);
    const handler = () => {
      output.textContent = apply(Number(input.value));
    };
    input.addEventListener('input', handler);
    this._appliers.push(handler);
  }

  _check(id, apply) {
    const input = $(id);
    const handler = () => apply(input.checked);
    input.addEventListener('change', handler);
    this._appliers.push(handler);
  }

  // -- painting ------------------------------------------------------------
  _paintFacts() {
    const meta = this.app.reader.meta;
    // A commit-graph piece has facts a CSV one does not, and they explain what
    // you are hearing better than the column list would.
    if (meta.git) {
      const git = meta.git;
      $('preset-note').title =
        `${git.commits} commits, ${git.merges} merges, trunk ${git.trunk}, ` +
        `metric ${git.metric}`;
    }
    $('fact-label').textContent = meta.label ?? '—';
    $('fact-seed').textContent = String(meta.seed ?? '—');
    $('fact-voices').textContent = `${this.app.reader.voiceCount} / 8`;
    $('fact-frames').textContent =
      `${this.app.reader.length} (${this.app.reader.duration.toFixed(1)}s, ` +
      `${this.app.reader.bars.toFixed(1)} bars)`;
    $('fact-fingerprint').textContent = meta.fingerprint ?? '—';
    $('preset-note').textContent =
      this.app.presets.find((p) => p.id === this.app.presetId)?.note ?? '';
  }

  _paintVoices() {
    const list = $('voice-list');
    list.replaceChildren();
    this.voiceRows = [];

    this.app.reader.names.forEach((name, index) => {
      const row = document.createElement('div');
      row.className = 'voice-row';

      const label = document.createElement('span');
      label.className = 'name';
      const entryPosition = this.app.entryOrder.indexOf(index);
      label.textContent = `${index + 1}. ${name}`;
      label.title = `enters at position ${entryPosition + 1} of ${this.app.entryOrder.length}`;

      const mute = document.createElement('button');
      mute.textContent = 'm';
      mute.title = 'mute audio';
      mute.addEventListener('click', () => {
        mute.classList.toggle('on', this.app.audio.toggleMute(index));
      });

      const solo = document.createElement('button');
      solo.textContent = 's';
      solo.title = 'solo audio';
      solo.addEventListener('click', () => {
        const soloed = this.app.audio.setSolo(index);
        for (const other of this.voiceRows) other.solo.classList.remove('on');
        if (soloed === index) solo.classList.add('on');
      });

      const hide = document.createElement('button');
      hide.textContent = 'v';
      hide.title = 'hide from the visuals';
      hide.addEventListener('click', () => {
        hide.classList.toggle('on', !this.app.visual.toggleVoice(index));
      });

      const meter = document.createElement('span');
      meter.className = 'meter';
      const fill = document.createElement('i');
      meter.append(fill);

      row.append(label, mute, solo, hide, meter);
      list.append(row);
      this.voiceRows.push({ row, fill, solo, index });
    });
  }

  _paintPedals() {
    const list = $('pedal-list');
    list.replaceChildren();
    const slots = this.app.reader.meta.chain?.chain ?? [];
    this.pedalRows = [];

    if (!slots.length) {
      const empty = document.createElement('li');
      empty.className = 'micro';
      empty.textContent = 'no chain metadata in this render';
      list.append(empty);
      return;
    }

    slots.forEach((slot, index) => {
      const row = document.createElement('li');
      row.className = 'pedal-row';

      const dot = document.createElement('span');
      dot.className = 'dot';
      dot.textContent = '●';

      const name = document.createElement('span');
      name.textContent = `${index}. ${slot.pedal}`;

      const params = document.createElement('span');
      params.className = 'params';
      const at = slot.at_intensity ? `@${slot.at_intensity}` : '';
      params.textContent = `${Object.entries(slot.params ?? {})
        .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
        .join(' ')} ${at}`.trim();

      row.append(dot, name, params);
      list.append(row);
      this.pedalRows.push({ row, threshold: slot.at_intensity ?? 0 });
    });
  }

  /** Draw the envelope, the live stroke, and the playhead. */
  _paintEnvelope() {
    const canvas = $('envelope-canvas');
    const ctx = canvas.getContext('2d');
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = canvas.clientWidth || 600;
    const h = canvas.clientHeight || 150;
    if (canvas.width !== Math.floor(w * dpr)) {
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    ctx.fillStyle = '#0c0c10';
    ctx.fillRect(0, 0, w, h);

    // Grid at the quartiles: an intensity curve is read by proportion.
    ctx.strokeStyle = '#1c1c22';
    ctx.lineWidth = 1;
    for (let i = 1; i < 4; i += 1) {
      ctx.beginPath();
      ctx.moveTo(0, (h * i) / 4);
      ctx.lineTo(w, (h * i) / 4);
      ctx.moveTo((w * i) / 4, 0);
      ctx.lineTo((w * i) / 4, h);
      ctx.stroke();
    }

    const envelope = this.app.envelope;
    if (this.app.reader.mode === 'endless') {
      ctx.fillStyle = '#4a4a48';
      ctx.font = '11px "Cascadia Mono", monospace';
      ctx.fillText('endless mode — intensity reacts to the data, no arc', 10, 20);
    }

    // The curve.
    ctx.strokeStyle = '#d8ff3a';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    const steps = Math.max(64, Math.floor(w));
    for (let i = 0; i <= steps; i += 1) {
      const t = i / steps;
      const x = t * w;
      const y = h - envelope.at(t) * h;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Pedal activation thresholds, so the author can see where each one arrives.
    const slots = this.app.reader.meta.chain?.chain ?? [];
    ctx.font = '9px "Cascadia Mono", monospace';
    for (const slot of slots) {
      const threshold = slot.at_intensity ?? 0;
      if (!threshold) continue;
      const y = h - threshold * h;
      ctx.strokeStyle = 'rgba(216,255,58,0.22)';
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(216,255,58,0.5)';
      ctx.fillText(slot.pedal, 4, y - 3);
    }

    // Playhead and current intensity.
    const progress = this.app.transport.progress();
    if (progress !== null) {
      const x = progress * w;
      ctx.strokeStyle = 'rgba(233,233,230,0.6)';
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
      ctx.fillStyle = '#e9e9e6';
      ctx.beginPath();
      ctx.arc(x, h - this.app.intensity * h, 3, 0, Math.PI * 2);
      ctx.fill();
    }

    $('envelope-origin').textContent = `origin: ${JSON.stringify(envelope.origin)}`;
  }

  /** Dump the current curve so a live stroke can be replayed later, or baked. */
  _exportEnvelope() {
    const payload = JSON.stringify(this.app.envelope.toJSON(), null, 2);
    const blob = new Blob([payload], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${this.app.reader.meta.label ?? 'serrin'}.envelope.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  /** Cheap per-frame refresh of the things that move. Called from the rAF loop. */
  tick() {
    // Autosave runs whether or not the panel is open: the settings are worth
    // keeping even when the author is watching the piece rather than the knobs.
    // Every 20 seconds, because serialising the whole state per frame would be
    // absurd and localStorage writes are synchronous.
    if (this.autosaveEnabled !== false) {
      const now = performance.now();
      if (!this._lastAutosave || now - this._lastAutosave > 20000) {
        this._lastAutosave = now;
        autosave(this.app);
      }
    }

    if (!this.visible) return;

    const levels = this.app.audio.voiceLevels();
    const gates = this.app.gates;
    for (const entry of this.voiceRows ?? []) {
      const level = levels[entry.index] ?? 0;
      entry.fill.style.width = `${Math.min(100, level * 140).toFixed(1)}%`;
      entry.row.classList.toggle('gated', gates ? gates[entry.index] === false : false);
    }

    for (const entry of this.pedalRows ?? []) {
      entry.row.classList.toggle('on', this.app.intensity >= entry.threshold);
    }

    if (this.app.keyboard.enabled) this._paintKeyboard();
    if (this.app.recorder?.recording) this._paintRecord();
    this._paintSession();

    const counter = this.app.transport.counter;
    $('fact-position').textContent =
      `${this.app.reader.tempo.formatPosition(counter)}  (frame ${counter})`;

    const progress = this.app.transport.progress();
    if (progress !== null && document.activeElement !== $('ctl-position')) {
      $('ctl-position').value = String(progress);
      $('out-position').textContent = `${(progress * 100).toFixed(0)}%`;
    }

    this._paintEnvelope();
  }
}
