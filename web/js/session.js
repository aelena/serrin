/**
 * Sessions, runtime side: capture what you are hearing, and get it back.
 *
 * The panel was previously a place to discover settings you could not keep --
 * tempo, swing, a hand-drawn envelope, register, balance, mutes, crush, all
 * gone on reload. This captures every one of them into a file the Python side
 * also understands.
 *
 * The format has a deliberate seam, and it is worth being blunt about where it
 * falls:
 *
 *   `preset`  -- the render layer. Changing any of it means the exported JSON
 *                would be different, so applying it here can only *record* the
 *                intent; the pipeline has to run again to hear it.
 *   `runtime` -- everything that exists only while the piece plays. Applying it
 *                takes effect immediately, and it has no offline meaning at all.
 *
 * So `apply()` restores the runtime layer and reports the render layer as a
 * mismatch when it no longer matches the loaded streams, rather than pretending
 * a chain edit took hold. Anything else would be lying about what you can hear.
 */

import { Envelope } from './envelope.js';
import { Tempo } from './tempo.js';

export const FORMAT = 'serrin-session/1';

/** Read the whole live state of the app into a plain object. */
export function capture(app, notes = '') {
  const { reader, audio, visual, keyboard, transport, envelope } = app;
  const meta = reader.meta ?? {};

  return {
    format: FORMAT,
    label: meta.label ?? '',
    fingerprint: meta.fingerprint ?? '',
    saved_at: new Date().toISOString(),
    notes,

    // -- what was ingested, from the render's own record of itself ----------
    source: {
      path: meta.ingest?.source ?? meta.source ?? '',
      columns: meta.ingest?.columns ?? meta.columns ?? null,
      bit_depth: meta.bit_depth ?? 8,
      granularity: meta.ingest?.granularity ?? meta.granularity ?? 1,
      aggregation: meta.ingest?.aggregation ?? null,
      log_scale: meta.ingest?.log_scale ?? false,
      limit: null,
      // The *live* tempo, not the rendered one: if the author moved the BPM
      // slider, that is the grid they mean to keep.
      tempo: reader.tempo.toJSON(),
    },

    // -- the render layer, in the schema the CLI already speaks -------------
    preset: buildPreset(app),

    // -- live only ---------------------------------------------------------
    runtime: {
      transport: {
        speed: reader.speed,
        mode: reader.mode,
        loop_policy: reader.loopPolicy,
        target_frames: transport?.targetFrames ?? null,
        counter: transport?.counter ?? 0,
      },
      audio: {
        master: audio.masterGain,
        crush_bits: audio.crushBits,
        delay_mix: audio.delayMix,
        delay_note: audio.delayNote,
        cutoff: audio.filterCutoff,
        balance: audio.balance,
        keyboard_crushed: audio.keyboardCrushed,
        mutes: [...audio.mutes],
        solo: audio.soloVoice,
      },
      visual: {
        show_glyphs: visual.showGlyphs,
        show_bars: visual.showBars,
        show_banding: visual.showBanding,
        corruption: visual.corruption,
        invert: visual.invert,
        hidden: [...visual.hidden],
        show_keys: visual.showKeys,
      },
      keyboard: {
        enabled: keyboard?.enabled ?? false,
        mode: keyboard?.mode ?? 'random',
        register: keyboard?.register ?? 'mid',
        level: keyboard?.level ?? 0.7,
        waveform: keyboard?.waveform ?? 'square',
      },
      voices: { entry_strategy: app.entryStrategy },
      // The curve as points, so a stroke drawn by hand survives -- this is the
      // concrete answer to section 8's question about recording a live stroke.
      envelope: envelope ? envelope.toJSON() : null,
    },

    streams: {
      audio: app.presets.find((p) => p.id === app.presetId)?.audio ?? null,
      visual: app.presets.find((p) => p.id === app.presetId)?.visual ?? null,
      preset_id: app.presetId,
    },
  };
}

/**
 * The render layer as a preset.
 *
 * Starts from the preset the render was actually made with -- carried in
 * `meta.chain` -- and overlays the parts the author may have changed since that
 * do have offline equivalents (tempo, envelope, piece settings). The pedal
 * chain itself is untouched, because nothing in the browser can edit it yet.
 */
function buildPreset(app) {
  const meta = app.reader.meta ?? {};
  const base = meta.chain ? structuredClone(meta.chain) : { name: 'session', chain: [] };

  base.ingest = { ...(base.ingest ?? {}) };
  base.ingest.tempo = app.reader.tempo.toJSON();
  delete base.ingest.rate; // tempo supersedes it; leaving both invites a conflict

  base.piece = {
    ...(base.piece ?? {}),
    mode: app.reader.mode,
    loop: app.reader.loopPolicy,
    voice_entry: app.entryStrategy,
    delay_note: app.audio.delayNote,
  };

  if (app.envelope) {
    // Baked to points rather than named: the author may have drawn over
    // whatever the preset originally specified, and the drawing is the truth.
    base.envelope = {
      kind: 'points',
      points: app.envelope.curve.map((value, index) => [
        index / Math.max(1, app.envelope.curve.length - 1),
        value,
      ]),
      origin: app.envelope.origin,
    };
  }
  return base;
}

/**
 * Apply a session to the running app.
 *
 * Returns a report rather than throwing on mismatch: a session saved against a
 * different render is still worth loading for its runtime half, and the author
 * should be told which parts did not take rather than left guessing.
 */
export function apply(app, session) {
  if (session.format && session.format !== FORMAT) {
    throw new Error(`session format ${session.format} is not ${FORMAT}`);
  }
  const report = { applied: [], skipped: [], warnings: [] };
  const runtime = session.runtime ?? {};

  // -- does the render layer still match what is loaded? -----------------
  const meta = app.reader.meta ?? {};
  if (session.fingerprint && meta.fingerprint && session.fingerprint !== meta.fingerprint) {
    report.warnings.push(
      `this session was saved against render ${session.fingerprint}, ` +
        `and ${meta.fingerprint} is loaded — the chain and mapping are not ` +
        're-applied here; re-render with: python -m serrin render --session <file>',
    );
    report.skipped.push('preset (chain, mapping, ingest)');
  }

  if (runtime.transport) {
    const t = runtime.transport;
    if (typeof t.speed === 'number') app.reader.speed = t.speed;
    if (t.mode) app.reader.mode = t.mode;
    if (t.loop_policy) app.reader.loopPolicy = t.loop_policy;
    if (typeof t.target_frames === 'number' && app.transport) {
      app.transport.targetFrames = t.target_frames;
    }
    report.applied.push('transport');
  }

  // Tempo comes from the source block: it is a render input *and* a live
  // control, so it is the one field that legitimately appears in both halves.
  const tempoSpec = session.source?.tempo;
  if (tempoSpec) {
    app.setTempo(
      new Tempo({
        bpm: tempoSpec.bpm,
        subdivision: tempoSpec.subdivision,
        swing: tempoSpec.swing,
        beatsPerBar: tempoSpec.beats_per_bar,
      }),
    );
    report.applied.push('tempo');
  }

  if (runtime.audio) {
    const a = runtime.audio;
    if (typeof a.master === 'number') app.audio.setMaster(a.master);
    if (typeof a.crush_bits === 'number') app.audio.setCrush(a.crush_bits);
    if (typeof a.delay_mix === 'number') app.audio.setDelayMix(a.delay_mix);
    if (a.delay_note) app.audio.setDelayNote(a.delay_note);
    if (typeof a.cutoff === 'number') app.audio.setCutoff(a.cutoff);
    if (typeof a.balance === 'number') {
      app.audio.balance = a.balance;
      app.visual.balance = a.balance;
    }
    if (typeof a.keyboard_crushed === 'boolean') {
      app.audio.setKeyboardCrushed(a.keyboard_crushed);
    }
    if (Array.isArray(a.mutes)) {
      app.audio.mutes = new Set(a.mutes.filter((i) => i < app.reader.voiceCount));
    }
    app.audio.soloVoice = typeof a.solo === 'number' ? a.solo : null;
    report.applied.push('audio');
  }

  if (runtime.visual) {
    const v = runtime.visual;
    if (typeof v.show_glyphs === 'boolean') app.visual.showGlyphs = v.show_glyphs;
    if (typeof v.show_bars === 'boolean') app.visual.showBars = v.show_bars;
    if (typeof v.show_banding === 'boolean') app.visual.showBanding = v.show_banding;
    if (typeof v.corruption === 'number') app.visual.corruption = v.corruption;
    if (typeof v.invert === 'boolean') app.visual.invert = v.invert;
    if (Array.isArray(v.hidden)) {
      app.visual.hidden = new Set(v.hidden.filter((i) => i < app.reader.voiceCount));
    }
    if (typeof v.show_keys === 'boolean') app.visual.showKeys = v.show_keys;
    report.applied.push('visual');
  }

  if (runtime.keyboard && app.keyboard) {
    const k = runtime.keyboard;
    app.keyboard.enabled = k.enabled === true;
    if (k.mode) app.keyboard.setMode(k.mode);
    if (k.register) app.keyboard.setRegister(k.register);
    if (typeof k.level === 'number') app.keyboard.level = k.level;
    if (k.waveform) app.keyboard.waveform = k.waveform;
    app.keyboard.showKeys = app.visual.showKeys;
    report.applied.push('keyboard');
  }

  if (runtime.voices?.entry_strategy) {
    app.setEntryStrategy(runtime.voices.entry_strategy);
    report.applied.push('voice entry');
  }

  if (runtime.envelope?.curve?.length) {
    app.setEnvelope(Envelope.fromExport(runtime.envelope));
    report.applied.push('envelope');
  }

  return report;
}

// ---------------------------------------------------------------------------
// files
// ---------------------------------------------------------------------------
function slug(text) {
  return (text || 'serrin').replace(/[^A-Za-z0-9._+-]+/g, '_').slice(0, 80);
}

/** Hand the browser a file. Used for sessions, presets and raw streams alike. */
export function download(filename, payload, type = 'application/json') {
  const text = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  // Revoked on a delay: Safari has historically cancelled the download when the
  // URL disappears in the same tick as the click.
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export function downloadSession(app, notes = '') {
  const session = capture(app, notes);
  download(`${slug(session.label)}.session.json`, session);
  return session;
}

export function downloadPreset(app) {
  const preset = buildPreset(app);
  preset.notes = [preset.notes, 'frozen from the panel'].filter(Boolean).join('\n');
  download(`${slug(preset.name)}.preset.json`, preset);
  return preset;
}

/**
 * Download the rendered streams themselves.
 *
 * The pair plus a session file is a complete, portable, re-playable piece --
 * which is the cheapest useful meaning of "save the piece". Audio you can send
 * to someone who does not have serrin is a different feature.
 */
export function downloadStreams(app) {
  const label = slug(app.reader.meta?.label ?? 'serrin');
  download(`${label}.audio.json`, app.reader.audio);
  download(`${label}.visual.json`, app.reader.visual);
  return label;
}

/** Read a session file the author picked. */
export function readSessionFile(file) {
  return new Promise((resolve, reject) => {
    const fileReader = new FileReader();
    fileReader.onerror = () => reject(new Error(`cannot read ${file.name}`));
    fileReader.onload = () => {
      try {
        resolve(JSON.parse(String(fileReader.result)));
      } catch (error) {
        reject(new Error(`${file.name} is not valid JSON: ${error.message}`));
      }
    };
    fileReader.readAsText(file);
  });
}

/** Per-browser autosave, so a reload does not cost the settings. */
const STORAGE_KEY = 'serrin.session.autosave';

export function autosave(app) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(capture(app, 'autosave')));
    return true;
  } catch {
    // Private windows and blocked site data both throw here. Losing an
    // autosave is not worth an error path.
    return false;
  }
}

export function loadAutosave() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function clearAutosave() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* nothing to do */
  }
}
