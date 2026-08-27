/**
 * Entry point: wire the reader, the two engines, the transport and the panel.
 *
 * The whole of section 4.2's structural pseudocode ends up in `intensityAt` and
 * the Transport -- one clock, one frame, both engines fed from it. Everything
 * else here is loading and keyboard shortcuts.
 *
 * **The studio is the entry point.** Opening the app used to drop you straight
 * onto a playing stage, fed from a hardcoded list of rendered pairs -- which was
 * the old render-first flow with a piece format bolted beside it. A piece is the
 * document, and you do not open a document already playing.
 *
 * So: load, land in the studio, configure or pick a piece, press play. That last
 * press is also the user gesture browsers require before audio, which is why the
 * "begin" gate could be deleted rather than moved.
 *
 * Visibility is not set here. `ViewState` owns which surface is showing and
 * `applyView` is the single place that touches `hidden` -- see views.js for why.
 *
 * URL parameters, all optional:
 *   ?piece=01-decay        open this piece in the studio
 *   ?play=01-decay         go straight to the stage with this piece's render
 *   ?preset=gritty_01      go straight to the stage with a built-in pair
 *   ?audio=…&visual=…      explicit paths, for a render outside a piece
 *   ?panel=1               open the author panel once on the stage
 *   ?speed=1.5             initial tick speed
 */

import { AudioEngine } from './audio.js';
import { KeyboardEngine } from './keyboard.js';
import { Recorder } from './recorder.js';
import { Envelope, ReactiveIntensity, voiceGates } from './envelope.js';
import { loadStreams } from './reader.js';
import { Transport } from './transport.js';
import { VisualEngine } from './visual.js';
import { DebugConsole } from './console.js';
import { Panel } from './panel.js';
import { Studio } from './studio.js';
import { ViewState } from './views.js';

/**
 * Built-in rendered pairs, for going straight to the stage without a piece.
 *
 * No longer the app's index -- pieces are, and they come from the server. This
 * is the demo path: clone the repo, run one render, open the page with
 * `?preset=gritty_01` and hear something without configuring anything first.
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

/** One piece of state for what is on screen. Nothing else touches `hidden`. */
const views = new ViewState('studio');

/**
 * The studio and the console exist from the first frame, not from the first
 * render. They used to be built inside `adopt()`, which meant they did not
 * exist until something had been loaded -- fine when the app opened onto a
 * playing stage, impossible now that the studio *is* the opening screen.
 */

const app = {
  views,
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
  console: null,
  studio: null,

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
    this.console?.setTrace(result.trace ?? null, result.label);
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
const failure = document.getElementById('failure');
const failureText = document.getElementById('failure-text');

/**
 * Paint the DOM from the view state. The only place `hidden` is assigned.
 *
 * Derived rather than toggled, so the contradictory combinations that used to be
 * reachable -- studio over a playing stage, panel over the studio, an error
 * behind a loaded view -- are now unrepresentable rather than merely avoided.
 */
function applyView(snapshot) {
  document.getElementById('studio').hidden = !snapshot.studio;
  document.getElementById('panel').hidden = !snapshot.panel;
  document.getElementById('console').hidden = !snapshot.console;
  failure.hidden = snapshot.error === null;
  if (snapshot.error !== null) failureText.textContent = snapshot.error;
  stage.style.cursor = snapshot.pointer ? 'default' : 'none';
  // The stage keeps rendering underneath the studio; it just has nothing to
  // render until a piece is loaded, and stops being covered when you play one.
  document.body.dataset.view = snapshot.view;
}

views.onChange = applyView;

function showError(error) {
  views.fail(String(error.message ?? error));
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
  // Built once and re-pointed, like the panel: it owns the log, and a preset
  // switch should not erase what happened before it.
  if (!app.console) app.console = new DebugConsole(app);
  app.console.log(
    `loaded ${reader.meta.label ?? 'stream'} — ${reader.voiceCount} voices, ` +
      `${reader.length} frames, ${reader.tempo.describe()}`,
    'render',
  );
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

  // Constructed once, refreshed thereafter -- see Panel.refresh().
  if (app.panel) {
    app.panel.refresh();
  } else {
    app.panel = new Panel(app);
  }
}

/**
 * Go to the stage with a rendered pair, starting the transport.
 *
 * The one path into playing. Called by the studio's "play it", by a `?play=` or
 * `?preset=` parameter, and by nothing else -- so "how did this start sounding"
 * has a single answer.
 */
async function playStreams(audioUrl, visualUrl, label = '') {
  const reader = await loadStreams(audioUrl, visualUrl);
  adopt(reader);
  views.clearError();
  views.go('stage');
  await app.transport.play();
  app.panel?.refresh();
  app.console?.log(`playing ${label || reader.meta.label || 'stream'}`, 'render');
}

app.playStreams = playStreams;

async function boot() {
  // Built before anything is loaded: the studio IS the opening screen, so it
  // cannot wait for a render the way it did when the stage came first.
  app.console = new DebugConsole(app);
  app.studio = new Studio(app);
  app.studioReady = () => app.studio.enter();

  // Painted before anything is loaded, so the studio is on screen while the
  // catalog and the piece list are still in flight rather than after.
  applyView(views.snapshot());

  const directAudio = params.get('audio');
  const directVisual = params.get('visual');
  const presetId = params.get('preset');
  const playPiece = params.get('play');

  try {
    if (directAudio && directVisual) {
      await playStreams(directAudio, directVisual, 'the given pair');
      return;
    }
    if (presetId) {
      const preset = PRESETS.find((entry) => entry.id === presetId);
      if (!preset) throw new Error(`no built-in preset called ${presetId}`);
      app.presetId = preset.id;
      await playStreams(preset.audio, preset.visual, preset.id);
      return;
    }

    // The default: the studio, with nothing sounding.
    await app.studioReady();
    if (playPiece) await app.studio.playPiece(playPiece);
    else if (params.get('piece')) await app.studio.openPiece(params.get('piece'));
  } catch (error) {
    showError(error);
  }

  if (params.get('panel') === '1') views.setOverlay('panel', true);

  // Panel and console refresh on their own loop so the engines' one stays clean.
  const chrome = () => {
    app.panel?.tick();
    app.console?.tick();
    requestAnimationFrame(chrome);
  };
  requestAnimationFrame(chrome);
}

// -- keyboard --------------------------------------------------------------
//
// Two rules, learned the hard way.
//
// **The stage does not repaint itself on a stray keypress.** `i` used to invert
// the whole piece to a light palette from a bare letter key -- undocumented,
// drastic, and only when the keyboard happened to be disarmed, so the same key
// did two unrelated things depending on hidden state. Inverting is an author
// choice and lives in the panel, where the author's choices live.
//
// **The way back in is always reachable.** `p` is reserved by the keyboard
// engine, so arming the keyboard cannot lock you out of the panel. It could
// before: `p` played a note, and the only way back was Escape, which disarmed
// the thing you were trying to configure.
window.addEventListener('keydown', async (event) => {
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;

  // The keyboard gets first refusal on everything except the keys the piece
  // reserves (see RESERVED in keyboard.js), so playing does not trip shortcuts.
  if (app.keyboard?.claims(event)) {
    event.preventDefault();
    app.keyboard.press(event);
    return;
  }

  // The arrows are never claimed (claims() takes single-character keys only),
  // which makes them the natural place for a live octave shift.
  if (app.keyboard?.enabled && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
    event.preventDefault();
    const offset = app.keyboard.shiftOctave(event.key === 'ArrowUp' ? 1 : -1);
    app.console?.log(`octave shift ${offset > 0 ? '+' : ''}${offset}`, 'info');
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
      // Nothing to toggle until a piece has been played into the stage.
      if (!app.transport) break;
      {
        const playing = await app.transport.toggle();
        document.getElementById('ctl-play').textContent = playing ? 'pause' : 'play';
      }
      break;
    case 'p':
    case 'P':
      // Only meaningful on the stage; the state machine refuses it elsewhere.
      views.toggleOverlay('panel');
      break;
    case 'F3':
      event.preventDefault();
      // Between the two views rather than an overlay on top of one. Entering the
      // studio pauses: design time is quiet, performance time sounds.
      if (views.inStudio) {
        if (app.reader) views.go('stage');
      } else {
        app.transport?.pause();
        views.go('studio');
        app.studio?.refreshFromServer();
      }
      break;
    case 'F2':
      // A function key, so the keyboard never claims it -- claims() only takes
      // single-character keys.
      event.preventDefault();
      views.toggleOverlay('console');
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

// A held note whose key-up lands in another window would sustain forever, since
// nothing on this page ever hears the release. Both events fire for alt-tab and
// for a click outside, and panic() is idempotent.
for (const event of ['blur', 'visibilitychange']) {
  window.addEventListener(event, () => {
    if (document.visibilityState !== 'visible' || event === 'blur') app.keyboard?.panic();
  });
}

document.getElementById('failure-dismiss').addEventListener('click', () => views.clearError());

boot();
