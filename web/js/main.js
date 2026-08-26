/**
 * Entry point: wire the reader, the two engines, the transport and the panel.
 *
 * The whole of section 4.2's structural pseudocode ends up in `intensityAt` and
 * the Transport -- one clock, one frame, both engines fed from it. Everything
 * else here is loading and keyboard shortcuts.
 *
 * URL parameters, all optional:
 *   ?preset=gritty_01      which rendered pair to load (default: gritty_01)
 *   ?audio=…&visual=…      explicit paths, for a render outside out/
 *   ?panel=1               open the author panel immediately
 *   ?autoplay=1            skip the gate (only works where autoplay is allowed)
 *   ?speed=1.5             initial tick speed
 */

import { AudioEngine } from './audio.js';
import { KeyboardEngine } from './keyboard.js';
import { Recorder } from './recorder.js';
import { Envelope, ReactiveIntensity, voiceGates } from './envelope.js';
import { loadStreams } from './reader.js';
import { Transport } from './transport.js';
import { VisualEngine } from './visual.js';
import { Panel } from './panel.js';

/**
 * The presets the panel can switch between. Each needs a rendered pair in out/.
 * Kept as a literal list because phase 1 renders offline -- there is no way to
 * discover them from a static file server.
 */
const PRESETS = [
  {
    id: 'gritty_01',
    audio: '../out/stream_audio.json',
    visual: '../out/stream_visual.json',
    note: "the design document's own chain: delta, LFSR mask, bitcrush, moving Caesar",
  },
  {
    id: 'ikeda_sparse',
    audio: '../out/ikeda_sparse_audio.json',
    visual: '../out/ikeda_sparse_visual.json',
    note: 'restrained: only acceleration speaks, pentatonic, no dirt',
  },
  {
    id: 'corrupted_dump',
    audio: '../out/corrupted_dump_audio.json',
    visual: '../out/corrupted_dump_visual.json',
    note: 'everything on: bit reverse, cross-mix, 2-bit crush at the climax',
  },
  {
    id: 'merkle_drift',
    audio: '../out/repo_audio.json',
    visual: '../out/repo_visual.json',
    note: 'a commit graph: branches are voices, hashes are the noise (section 6.3)',
  },
  {
    id: 'endless_drift',
    audio: '../out/endless_drift_audio.json',
    visual: '../out/endless_drift_visual.json',
    note: 'mode B: installation loop, no arc, intensity follows the data',
  },
];

const params = new URLSearchParams(location.search);

const app = {
  presets: PRESETS,
  presetId: params.get('preset') ?? PRESETS[0].id,
  reader: null,
  audio: null,
  visual: null,
  transport: null,
  panel: null,
  envelope: Envelope.constant(1),
  reactive: null,
  intensity: 1,
  gates: null,
  entryStrategy: 'variance',
  entryOrder: [],
  keyboard: null,
  recorder: null,

  /** Replace the intensity curve. Stroke, equation or archetype -- same call. */
  setEnvelope(envelope) {
    this.envelope = envelope;
  },

  /**
   * Change the grid live.
   *
   * The reader holds the tempo because it owns frame timing; the transport has
   * to be told, because every onset it has already computed was measured against
   * the old grid.
   */
  setTempo(tempo) {
    this.reader.tempo = tempo;
    this.transport?.retime();
  },

  setEntryStrategy(strategy) {
    this.entryStrategy = strategy;
    this.entryOrder = computeEntryOrder(this.reader, strategy);
  },

  /**
   * Take on a pair the server just rendered.
   *
   * Registered as a preset first, then loaded through the normal path -- so
   * there is one way to load a piece, and an uploaded render is switchable in
   * the preset dropdown like anything else.
   */
  async adoptRender(result) {
    const id = result.label || `render-${this.presets.length}`;
    const entry = {
      id,
      audio: result.audio,
      visual: result.visual,
      note: `rendered from ${result.kind} through ${result.chain}`,
    };
    const existing = this.presets.findIndex((preset) => preset.id === id);
    if (existing >= 0) this.presets[existing] = entry;
    else this.presets.push(entry);
    await this.loadPreset(id);
  },

  async loadPreset(id) {
    const preset = PRESETS.find((p) => p.id === id);
    if (!preset) return;
    const wasPlaying = this.transport?.playing ?? false;
    this.transport?.pause();
    try {
      const reader = await loadStreams(preset.audio, preset.visual);
      this.presetId = id;
      adopt(reader);
      if (wasPlaying) await this.transport.play();
    } catch (error) {
      showError(error);
    }
  },
};

// ---------------------------------------------------------------------------
// intensity: the one place mode A and mode B differ (5.1-A vs 5.1-B)
// ---------------------------------------------------------------------------
function intensityAt(counter, totalFrames, frame) {
  if (app.reader.mode === 'endless') {
    // No imposed arc. Intensity is reactive to the data itself.
    app.intensity = app.reactive.update(frame.visual);
  } else {
    const progress = totalFrames ? Math.min(1, counter / totalFrames) : 0;
    app.intensity = app.envelope.at(progress);
  }
  app.gates = voiceGates(app.intensity, app.entryOrder, 1);
  return app.intensity;
}

function computeEntryOrder(reader, strategy) {
  const count = reader.voiceCount;
  if (strategy === 'columns') return Array.from({ length: count }, (_, i) => i);
  // The export already carries the order Python computed by variance; reversing
  // it gives "sparsest first" without re-deriving statistics in the browser.
  const byVariance = reader.entryOrder?.length
    ? [...reader.entryOrder]
    : Array.from({ length: count }, (_, i) => i);
  return strategy === 'sparse' ? byVariance.reverse() : byVariance;
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
const stage = document.getElementById('stage');
const gate = document.getElementById('gate');
const gateStart = document.getElementById('gate-start');
const gateLabel = document.getElementById('gate-label');
const gateError = document.getElementById('gate-error');

function showError(error) {
  gate.hidden = false;
  gateError.hidden = false;
  gateError.textContent = String(error.message ?? error);
  gateStart.disabled = true;
  console.error(error);
}

/** Point the whole app at a (new) reader, keeping the panel in sync. */
function adopt(reader) {
  app.reader = reader;
  if (params.has('speed')) reader.speed = Number(params.get('speed')) || 1;

  app.audio = new AudioEngine(reader);
  app.visual = new VisualEngine(stage, reader);
  if (app.keyboard) {
    // Kept across a preset switch: it holds the player's settings, and the new
    // piece only changes which scale it draws from.
    app.keyboard.audio = app.audio;
    app.keyboard.adopt(reader);
  } else {
    app.keyboard = new KeyboardEngine(app.audio, reader);
  }
  app.keyboard.onNote = (played) => app.visual.flashKey(played);
  // The recorder holds a tap on the audio graph, so it is rebuilt with the
  // engine rather than kept across a preset switch.
  app.recorder = new Recorder(app.audio, stage);
  app.recorder.onStateChange = () => app.panel?._paintRecord();
  app.visual.showKeys = app.keyboard.showKeys;
  app.envelope = Envelope.fromExport(reader.audio.envelope);
  app.reactive = new ReactiveIntensity(reader.seed);
  app.setEntryStrategy(reader.meta.voice_entry_strategy ?? 'variance');

  app.transport = new Transport(reader, app.audio, app.visual, {
    intensityAt,
    gatesFor: () => app.gates,
    keyboardArmed: () => app.keyboard?.enabled === true,
    onEnd: () => {
      document.getElementById('ctl-play').textContent = 'play';
    },
  });

  gateLabel.textContent = `${reader.meta.label ?? 'stream'} · ${reader.voiceCount} voices · ${reader.duration.toFixed(0)}s`;

  // Constructed once, refreshed thereafter -- see Panel.refresh().
  if (app.panel) {
    app.panel.refresh();
  } else {
    app.panel = new Panel(app);
    app.panel.toggle(params.get('panel') === '1');
  }
}

async function boot() {
  const preset = PRESETS.find((p) => p.id === app.presetId) ?? PRESETS[0];
  const audioUrl = params.get('audio') ?? preset.audio;
  const visualUrl = params.get('visual') ?? preset.visual;

  try {
    adopt(await loadStreams(audioUrl, visualUrl));
  } catch (error) {
    showError(error);
    return;
  }

  gateStart.disabled = false;
  gateStart.focus();

  const begin = async () => {
    gate.hidden = true;
    await app.transport.play();
    document.getElementById('ctl-play').textContent = 'pause';
  };

  gateStart.addEventListener('click', begin);
  if (params.get('autoplay') === '1') begin().catch(showError);

  // Panel refresh runs on its own rAF so the engines' loop stays clean.
  const panelLoop = () => {
    app.panel?.tick();
    requestAnimationFrame(panelLoop);
  };
  requestAnimationFrame(panelLoop);
}

// -- keyboard: the piece needs no pointer, the author needs no menus ---------
window.addEventListener('keydown', async (event) => {
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;

  // The keyboard gets first refusal on everything except the keys the piece
  // reserves (see RESERVED in keyboard.js), so playing does not trip shortcuts.
  if (app.keyboard?.claims(event)) {
    event.preventDefault();
    app.keyboard.press(event);
    return;
  }

  if (event.key === 'Escape' && app.keyboard?.enabled) {
    app.keyboard.enabled = false;
    app.keyboard.panic();
    const box = document.getElementById('ctl-keyboard');
    if (box) box.checked = false;
    return;
  }

  switch (event.key) {
    case ' ':
      event.preventDefault();
      if (!gate.hidden) {
        gateStart.click();
      } else {
        const playing = await app.transport.toggle();
        document.getElementById('ctl-play').textContent = playing ? 'pause' : 'play';
      }
      break;
    case 'p':
    case 'P':
      app.panel?.toggle();
      break;
    case 'i':
    case 'I':
      app.visual.invert = !app.visual.invert;
      break;
    case 'f':
    case 'F':
      if (document.fullscreenElement) document.exitFullscreen();
      else document.body.requestFullscreen?.();
      break;
    default:
      // 1..8 mute a voice, matching the panel's numbering.
      if (/^[1-8]$/.test(event.key)) {
        const index = Number(event.key) - 1;
        if (index < app.reader.voiceCount) app.audio.toggleMute(index);
      }
  }
});

window.addEventListener('keyup', (event) => {
  app.keyboard?.release(event);
});

boot();
