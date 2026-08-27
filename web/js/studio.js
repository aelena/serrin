/**
 * The studio: where a piece is configured, before it is generated.
 *
 * The overlay panel and this view answer different questions and deliberately do
 * not look alike. The panel is *performance time* -- things you touch while a
 * piece plays, reachable without covering the stage. The studio is *design time*
 * -- the source, the chain, the mapping, the grid, things you set and then leave
 * alone. Merging them would produce one cluttered surface that is bad at both.
 *
 * They share the state, not the layout.
 *
 * One thing this view can do that the panel cannot: **edit the pedal chain.**
 * That was on the deferred list, but the deferral was about toggling pedals
 * *live*, which needs the chain ported to JavaScript. Editing a chain and then
 * re-rendering through Python needs no port at all -- the browser is a front end
 * for the CLI here, and the pedals stay in one place.
 *
 * The studio holds a **working copy** of the manifest. Edits are local until
 * saved, which is what lets it say "unsaved" honestly. Rendering reads the file
 * from disk, so a dirty piece is saved first -- announced rather than silent,
 * because "render" quietly writing your edits would be a surprise.
 */

import {
  ALL_KEYS,
  KEY_ROWS,
  chromaticKeymap,
  defaultKeymap,
  describeBinding,
  fallbackLabel,
  layoutLabels,
  noteName,
} from './keymap.js';
import { requestRender } from './source.js';

const $ = (id) => document.getElementById(id);

function esc(text) {
  return String(text ?? '').replace(
    /[&<>"']/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch],
  );
}

/**
 * Parameters that read better as a menu than a text box.
 *
 * A convenience, not a schema: anything not listed falls back to a type guessed
 * from its default. The pedal catalog's `params` dict is the real description of
 * what a pedal accepts, and it comes from Python.
 */
const PARAM_CHOICES = {
  mask_source: ['const', 'column', 'lfsr'],
  op: ['xor', 'add', 'sub', 'mod', 'mul', 'min', 'max'],
  freq_curve: ['log', 'direct'],
  aggregation: null, // filled from the catalog
};

const LFO_HINT = 'sine:4beats · saw:1bar · square:0.1hz:0.4 · sample_hold:1/2beat';

/**
 * Endpoints this page cannot work without.
 *
 * Checked against what the catalog says the server serves, because the failure
 * otherwise is invisible: a server started before an endpoint existed answers
 * 404, the browser logs one line, and the UI shows nothing at all. Restarting
 * serve.py is the fix, and nothing was telling anyone to do it.
 */
const REQUIRED_ENDPOINTS = [
  '/api/source',
  '/api/pieces',
  '/api/piece',
  '/api/piece/data',
  '/api/render',
];

export class Studio {
  constructor(app) {
    this.app = app;
    this.root = $('studio');
    this.catalog = null;
    this.pieces = [];
    this.folder = null;
    this.manifest = null;
    this.source = null;
    this.dirty = false;
    this.busy = false;
    //: The author's own key labels, so the editor shows their keyboard while
    //: still storing positions. Filled once, asynchronously.
    this.labels = Object.fromEntries(ALL_KEYS.map((code) => [code, fallbackLabel(code)]));
    this.selectedKey = null;

    layoutLabels().then((labels) => {
      this.labels = labels;
      if (this.visible) this.paint();
    });

    // "to the stage" rather than a close box: the studio is not a dialog over
    // the piece, it is the other half of the app.
    $('studio-close').addEventListener('click', () => this.leave());
    $('studio-new').addEventListener('click', () => this.createPiece());
    $('studio-save').addEventListener('click', () => this.save());
    $('studio-render').addEventListener('click', () => this.render());
    $('studio-play').addEventListener('click', () => this.playCurrent());
  }

  // -- coming and going ----------------------------------------------------
  get visible() {
    return this.app.views.inStudio;
  }

  /** Enter the studio: load what it needs, then show it. */
  async enter() {
    this.app.views.go('studio');
    await this.refreshFromServer();
  }

  /** Reload the catalog and the piece list. Cheap enough to do on every entry. */
  async refreshFromServer() {
    if (!this.catalog) await this.loadCatalog();
    await this.loadPieces();
    this.paint();
  }

  /**
   * Leave for the stage.
   *
   * Only possible once something is loaded: the stage with no reader is a black
   * rectangle, and sending the author to it would look like a crash.
   */
  leave() {
    if (!this.app.reader) {
      this.message('nothing rendered yet — press render, then play it', true);
      return false;
    }
    this.app.views.go('stage');
    return true;
  }

  message(text, warning = false) {
    const node = $('studio-message');
    node.textContent = text;
    node.style.color = warning ? '#ff9d5c' : '';
    this.app.console?.log(text, warning ? 'warn' : 'system');
  }

  // -- loading -------------------------------------------------------------
  async _get(path) {
    const response = await fetch(path);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) {
      throw new Error(payload.error ?? `${path} failed (${response.status})`);
    }
    return payload;
  }

  async _post(path, body) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) {
      throw new Error(payload.error ?? `${path} failed (${response.status})`);
    }
    return payload;
  }

  async loadCatalog() {
    try {
      this.catalog = await this._get('/api/catalog');
      PARAM_CHOICES.aggregation = this.catalog.aggregations;

      // An older server answers this endpoint but not the newer ones. Saying so
      // once, loudly, beats a 404 per action and no explanation.
      const served = this.catalog.endpoints ?? [];
      const missing = REQUIRED_ENDPOINTS.filter((route) => !served.includes(route));
      if (missing.length) {
        this.stale = missing;
        this.message(
          `the server is older than this page — it does not serve ${missing.join(', ')}. ` +
            'Stop it and run scripts/serve.py again.',
          true,
        );
      } else {
        this.stale = null;
      }
    } catch (error) {
      this.message(`cannot load the catalog: ${error.message} — is serve.py running?`, true);
      // A minimal catalog, so the studio degrades to something usable rather
      // than a blank screen.
      this.catalog = {
        pedals: [], scales: {}, archetypes: [], equations: [],
        git: { metrics: {}, traversals: [] },
        tempo: { subdivisions: [4, 8, 16], note_fractions: {} },
        mapping_defaults: {}, max_voices: 8, aggregations: ['mean'],
        loop_policies: ['vary'], voice_entry: ['variance'], modes: ['closed'],
        waveforms: ['sawtooth'], lfo_shapes: [],
      };
    }
  }

  async loadPieces() {
    try {
      const payload = await this._get('/api/pieces');
      this.pieces = payload.pieces ?? [];
    } catch (error) {
      this.pieces = [];
      this.message(error.message, true);
    }
  }

  async openPiece(folder) {
    if (this.dirty && !confirm('This piece has unsaved changes. Discard them?')) return;
    try {
      const payload = await this._get(`/api/piece?folder=${encodeURIComponent(folder)}`);
      this.folder = payload.folder;
      this.manifest = payload.manifest;
      this.detail = payload;
      this.dirty = false;
      this.source = null;
      this.paint();
      await this.loadSource();
    } catch (error) {
      this.message(error.message, true);
    }
  }

  /**
   * Ask about the source *currently in the form*, not the one on disk.
   *
   * The old version passed only the piece folder, so the server loaded the saved
   * manifest and answered about its old path -- which is why typing a new path
   * never refreshed the column list. A working copy is unsaved by definition.
   */
  async loadSource() {
    if (!this.manifest) return;
    const path = this.get('source.path');
    if (!path) {
      this.source = null;
      this.paint();
      return;
    }
    const query = new URLSearchParams({ path, kind: this.get('source.kind', 'csv') });
    if (this.folder) query.set('piece', this.folder);
    this.source = { loading: true };
    try {
      this.source = await this._get(`/api/source?${query}`);
    } catch (error) {
      this.source = { error: error.message };
    }
    this.paint();
  }

  /** Upload a file into the piece folder and point the source at it. */
  async putData(file) {
    if (!this.folder) {
      // Silence here was the actual reported symptom: choosing a file with no
      // piece open did nothing, said nothing, and logged nothing.
      this.message('open or create a piece first — a file has to go somewhere', true);
      return;
    }
    this.message(`copying ${file.name} into the piece…`);
    try {
      const text = await file.text();
      const result = await this._post('/api/piece/data', {
        piece: this.folder,
        filename: file.name,
        text,
      });
      this.set('source.kind', result.kind);
      this.set('source.path', result.path);
      this.source = result.source ?? null;
      this.paint();
      this.message(
        `${file.name} copied into the piece as ${result.path} ` +
          `(${(result.bytes / 1024).toFixed(1)} KiB)`,
      );
    } catch (error) {
      // Rejected before it was written: a piece never ends up pointing at a file
      // that cannot be read.
      this.message(`not accepted — ${error.message}`, true);
    }
  }

  /** Read a repository once and keep a portable copy of its history. */
  async exportHistory() {
    if (!this.folder) {
      this.message('open or create a piece first — the history has to go somewhere', true);
      return;
    }
    const repo = prompt('Path to the repository to export:', this.get('source.path') || '');
    if (!repo) return;
    this.message(`reading ${repo}…`);
    try {
      const result = await this._post('/api/piece/graph', {
        piece: this.folder,
        repo,
        stamp: new Date().toISOString(),
      });
      this.set('source.kind', 'graph');
      this.set('source.path', result.path);
      await this.loadSource();
      this.message(
        `exported ${result.graph.commits} commits into ${result.path} — ` +
          'the history now travels with the piece',
      );
    } catch (error) {
      this.message(error.message, true);
    }
  }

  async createPiece() {
    const name = prompt('Name for the new piece (it becomes the folder name):');
    if (!name) return;
    try {
      const payload = await this._post('/api/piece/new', {
        name,
        source: { kind: 'csv', path: '' },
        stamp: new Date().toISOString(),
      });
      await this.loadPieces();
      await this.openPiece(payload.folder);
      this.message(`created ${payload.folder} — set its source path next`);
    } catch (error) {
      this.message(error.message, true);
    }
  }

  // -- saving and rendering ------------------------------------------------
  async save() {
    if (!this.manifest) return null;
    try {
      const payload = await this._post('/api/piece', {
        folder: this.folder,
        manifest: this.manifest,
        stamp: new Date().toISOString(),
      });
      this.manifest = payload.manifest;
      this.detail = payload;
      this.dirty = false;
      await this.loadPieces();
      this.paint();
      this.message(`saved ${this.folder}`);
      return payload;
    } catch (error) {
      // Refused rather than written: an invalid manifest on disk would make the
      // piece unopenable, which is worse than a rejected save.
      this.message(`not saved — ${error.message}`, true);
      return null;
    }
  }

  async render() {
    if (!this.manifest || this.busy) return;
    if (!this.manifest.source?.path) {
      this.message('this piece has no source path yet', true);
      return;
    }
    // Python renders from the file on disk, so a dirty piece is saved first.
    // Said out loud, because "render" silently writing your edits is a surprise.
    if (this.dirty) {
      this.message('saving first — the pipeline renders from the file on disk…');
      if (!(await this.save())) return;
    }

    this.busy = true;
    this.paint();
    this.message('rendering…');
    try {
      const result = await requestRender({
        piece: this.folder,
        trace: true,
        stamp: new Date().toISOString(),
      });
      this.manifest = result.manifest ?? this.manifest;
      this.detail = result;
      await this.loadPieces();
      this.message(
        `rendered ${result.fingerprint} — ${result.frames} frames, ` +
          `${Number(result.duration).toFixed(1)}s, ${result.voices.length} voices`,
      );
      this.app.console?.setTrace(result.trace ?? null, result.label);
      this.lastRender = result;
    } catch (error) {
      this.message(error.message, true);
    } finally {
      this.busy = false;
      this.paint();
    }
  }

  /**
   * Play the piece that is open: load its render, go to the stage, start.
   *
   * Goes through `app.playStreams`, which is the only path into playing -- so
   * "how did this start sounding" has one answer, and the click that gets here
   * is the user gesture the browser wants before audio.
   */
  async playCurrent() {
    const render = this.detail?.render ?? {};
    const audio = render.audio_url ?? this.lastRender?.audio;
    const visual = render.visual_url ?? this.lastRender?.visual;
    if (!audio || !visual) {
      this.message('nothing rendered yet — press render first', true);
      return;
    }
    try {
      await this.app.playStreams(audio, visual, this.manifest?.name ?? 'piece');
      // After the streams, because the key map is resolved against the render's
      // scale and there is no scale until the render is loaded.
      this.applyPerformance();
    } catch (error) {
      this.app.views.go('studio');
      this.message(error.message, true);
    }
  }

  /** Open a piece and play it in one step. Used by `?play=…`. */
  async playPiece(folder) {
    await this.openPiece(folder);
    if (this.get('render.fingerprint')) await this.playCurrent();
    else this.message(`${folder} has not been rendered yet — press render`, true);
  }

  /**
   * Push the piece's performance layer into the live engines.
   *
   * Samples and patterns are still inert -- their modes come next -- so only the
   * key map and the keyboard settings cross over. Wiring the rest now would be
   * scaffolding pretending to be a feature.
   */
  applyPerformance() {
    const performance = this.manifest?.performance ?? {};
    const keyboard = performance.keyboard ?? {};
    if (!this.app.keyboard) return;

    const bound = this.app.keyboard.setKeymap(performance.keymap);
    if (keyboard.mode) this.app.keyboard.setMode(keyboard.mode);
    if (keyboard.register) this.app.keyboard.setRegister(keyboard.register);
    if (typeof keyboard.level === 'number') this.app.keyboard.level = keyboard.level;
    if (keyboard.waveform) this.app.keyboard.waveform = keyboard.waveform;

    if (bound) {
      this.app.console?.log(
        `key map loaded: ${bound} positions${keyboard.mode === 'notes' ? '' : ' (switch the keyboard to notes mode to play it)'}`,
        'system',
      );
    }
    this.app.panel?._paintKeyboard?.();
  }

  // -- editing -------------------------------------------------------------
  /** Mutate the working copy at a dotted path, and mark the piece dirty. */
  set(path, value) {
    if (!this.manifest) return;
    const parts = path.split('.');
    let target = this.manifest;
    for (const part of parts.slice(0, -1)) {
      if (target[part] === undefined || target[part] === null) target[part] = {};
      target = target[part];
    }
    const last = parts[parts.length - 1];
    if (value === '' || value === null || Number.isNaN(value)) delete target[last];
    else target[last] = value;
    this.dirty = true;
    this._paintHeader();
  }

  get(path, fallback = '') {
    let target = this.manifest;
    for (const part of path.split('.')) {
      if (target === null || target === undefined) return fallback;
      target = target[part];
    }
    return target === undefined || target === null ? fallback : target;
  }

  get chainSlots() {
    if (!this.manifest.preset) this.manifest.preset = { name: this.manifest.name, chain: [] };
    if (!Array.isArray(this.manifest.preset.chain)) this.manifest.preset.chain = [];
    return this.manifest.preset.chain;
  }

  // -- painting ------------------------------------------------------------
  paint() {
    this._paintPieceList();
    this._paintHeader();
    $('studio-body').innerHTML = this.manifest
      ? [
          this._source(),
          this._tempo(),
          this._chain(),
          this._mapping(),
          this._envelope(),
          this._pieceSettings(),
          this._performance(),
          this._keymapEditor(),
        ].join('')
      : '<p class="dim">pick a piece on the left, or create one.</p>';
    if (this.manifest) this._wireBody();
  }

  _paintPieceList() {
    const list = $('studio-pieces');
    if (!this.pieces.length) {
      list.innerHTML =
        '<p class="dim">no pieces yet. start the server with ' +
        '<code>--pieces &lt;your album folder&gt;</code>.</p>';
      return;
    }
    list.innerHTML = this.pieces
      .map((entry) => {
        if (!entry.ok) {
          return `<button class="piece broken" disabled title="${esc(entry.error)}">
            ${esc(entry.name)}<span>broken</span></button>`;
        }
        const state = entry.rendered ? entry.fingerprint.slice(0, 10) : 'not rendered';
        const warn = entry.missing_samples?.length ? ' · missing samples' : '';
        return `<button class="piece${entry.name === this.folder ? ' on' : ''}"
          data-open="${esc(entry.name)}">
          ${esc(entry.title || entry.name)}
          <span>${esc(state)}${esc(warn)}</span></button>`;
      })
      .join('');
    for (const button of list.querySelectorAll('[data-open]')) {
      button.addEventListener('click', () => this.openPiece(button.dataset.open));
    }
  }

  _paintHeader() {
    $('studio-title').textContent = this.manifest
      ? this.manifest.title || this.manifest.name
      : 'no piece open';
    $('studio-state').textContent = !this.manifest
      ? ''
      : this.dirty
        ? 'unsaved changes'
        : this.get('render.fingerprint')
          ? `rendered ${this.get('render.fingerprint').slice(0, 12)}`
          : 'not rendered';
    $('studio-state').classList.toggle('dirty', this.dirty);
    $('studio-save').disabled = !this.manifest || !this.dirty;
    $('studio-render').disabled = !this.manifest || this.busy;
    $('studio-render').textContent = this.busy ? 'rendering…' : 'render';
    $('studio-play').disabled = !this.get('render.fingerprint');
  }

  // -- sections ------------------------------------------------------------
  _field(label, path, type = 'text', extra = '') {
    const value = this.get(path);
    return `<label>${esc(label)}
      <input data-path="${esc(path)}" type="${type}" value="${esc(value)}" ${extra} />
    </label>`;
  }

  _select(label, path, options, current = null) {
    const value = current ?? this.get(path);
    const items = options
      .map(
        (option) =>
          `<option value="${esc(option)}"${String(option) === String(value) ? ' selected' : ''}>${esc(option)}</option>`,
      )
      .join('');
    return `<label>${esc(label)}
      <select data-path="${esc(path)}">${items}</select></label>`;
  }

  _source() {
    const kind = this.get('source.kind', 'csv');
    const rows = [this._select('kind', 'source.kind', ['csv', 'git', 'graph'])];

    if (kind === 'csv') {
      rows.push(
        `<div class="row">
           <button data-action="pick-csv">choose a CSV…</button>
           <input id="studio-file" type="file" accept=".csv,.tsv,.json,text/csv" hidden />
         </div>
         <p class="dim">choosing a file copies it into the piece folder, because a
         browser hands over contents and not a path — and because data that
         travels with the piece is the better default. For a file too large to
         want two copies of, type its path instead.</p>`,
        this._field('path (relative to the piece, or absolute)', 'source.path'),
        this._field('rows per frame (granularity)', 'source.granularity', 'number', 'min="1"'),
        this._select('aggregation', 'source.aggregation', this.catalog.aggregations),
        this._field('bit depth', 'source.bit_depth', 'number', 'min="1" max="16"'),
        this._field('read at most N rows (blank = all)', 'source.limit', 'number', 'min="1"'),
      );
    } else if (kind === 'git') {
      rows.push(
        this._field('repository path on this machine', 'source.path'),
        `<p class="dim">a live clone: read every time the piece renders, and only
         on a machine that has it. <b>export its history</b> keeps a portable copy
         in the piece instead.</p>
         <div class="row">
           <button data-action="export-history">export its history…</button>
         </div>`,
      );
    } else {
      rows.push(
        `<div class="row">
           <button data-action="pick-graph">choose a history JSON…</button>
           <button data-action="export-history">export from a repository…</button>
           <input id="studio-file" type="file" accept=".json,application/json" hidden />
         </div>
         <p class="dim">an exported commit history. Travels with the piece, so an
         album opens on a machine that has never cloned the repository. Make one
         with <code>serrin graph --repo …</code> or the button above.</p>`,
        this._field('path (relative to the piece, or absolute)', 'source.path'),
      );
    }

    if (kind !== 'csv') {
      rows.push(
        this._select('metric', 'source.metric', Object.keys(this.catalog.git.metrics)),
        this._select('traversal', 'source.traversal', ['', ...this.catalog.git.traversals]),
        `<p class="dim">${esc(
          this.catalog.git.metrics[this.get('source.metric', 'hash')] ?? '',
        )}</p>`,
      );
    }

    rows.push(this._sourceReport(kind));

    return this._section(
      'source',
      rows.join(''),
      'Serrin does not clean data. Whatever is wrong with a source is named ' +
      'here and has to be fixed upstream.',
    );
  }

  /** What the server found, and what it objected to. */
  _sourceReport(kind) {
    const source = this.source;
    if (!source) return '<p class="dim">no source set yet.</p>';
    if (source.loading) return '<p class="dim">reading the source…</p>';
    if (source.error) {
      return `<p class="warn">cannot read the source: ${esc(source.error)}</p>`;
    }

    const problems = (source.problems ?? [])
      .map((problem) => `<li>${esc(problem)}</li>`)
      .join('');
    const complaints = problems
      ? `<ul class="problems">${problems}</ul>`
      : '<p class="dim">no problems.</p>';

    if (kind === 'csv') return complaints + this._tableReport() + this._columnPicker();
    if (kind === 'graph') return complaints + this._graphReport();

    const branches = (source.branches ?? [])
      .map((branch) => esc(branch.name))
      .join(', ');
    return (
      complaints +
      (branches
        ? `<p class="dim">branches: ${branches} — each becomes a voice, up to
           ${this.catalog.max_voices}.</p>`
        : '')
    );
  }

  /** How the file was parsed. Shown always, not only when something was odd. */
  _tableReport() {
    const table = this.source?.table;
    if (!table?.columns) return '';
    const bits = [
      `delimiter <b>${esc(table.delimiter === '\t' ? 'tab' : table.delimiter)}</b>`,
      `header on line <b>${table.header_line}</b>`,
      `${table.columns} columns`,
      `${table.data_rows} rows`,
    ];
    if (table.preamble_lines) bits.push(`${table.preamble_lines} preamble lines skipped`);
    if (table.dropped_rows) bits.push(`${table.dropped_rows} lines dropped`);
    return `<p class="dim">${bits.join(' · ')}</p>`;
  }

  _graphReport() {
    const facts = this.source?.graph;
    if (!facts) return '';
    const owned = Object.entries(facts.owned ?? {})
      .map(([ref, count]) => `<tr><td class="name">${esc(ref)}</td><td>${count} commits</td></tr>`)
      .join('');
    return (
      `<p class="dim">${facts.commits} commits · ${facts.merges} merges ·
        ${facts.authors} authors · traversal ${esc(facts.traversal)} ·
        ${facts.has_stats ? 'with' : 'without'} per-commit stats` +
      (facts.span_seconds
        ? ` · spans ${(facts.span_seconds / 86400).toFixed(1)} days`
        : '') +
      `</p><table class="stable"><tbody>${owned}</tbody></table>`
    );
  }

  _tempo() {
    const tempo = this.get('source.tempo', {}) || {};
    const bpm = tempo.bpm ?? 120;
    const subdivision = tempo.subdivision ?? 16;
    const rate = (bpm / 60) * (subdivision / 4);
    return this._section(
      'tempo',
      [
        this._field('bpm', 'source.tempo.bpm', 'number', 'min="20" max="300" step="0.5"'),
        this._select(
          'one frame is a 1/N note',
          'source.tempo.subdivision',
          this.catalog.tempo.subdivisions,
          subdivision,
        ),
        this._field('swing (0 straight, 1 triplet)', 'source.tempo.swing', 'number',
          'min="0" max="1" step="0.01"'),
        this._field('beats per bar', 'source.tempo.beats_per_bar', 'number', 'min="1" max="16"'),
        `<p class="dim">= ${rate.toFixed(3)} frames per second. One row of data is one
          frame; the tempo only names the spacing.</p>`,
      ].join(''),
    );
  }

  _chain() {
    const slots = this.chainSlots;
    const names = this.catalog.pedals.map((pedal) => pedal.name);

    const rows = slots
      .map((slot, index) => {
        const pedal = this.catalog.pedals.find((p) => p.name === slot.pedal);
        const params = Object.entries(pedal?.params ?? slot.params ?? {})
          .map(([key, fallback]) => this._pedalParam(index, key, fallback, slot.params ?? {}))
          .join('');
        return `<li class="slot${slot.enabled === false ? ' off' : ''}">
          <div class="slot-head">
            <select data-slot="${index}" data-slot-field="pedal">
              ${names.map((name) => `<option value="${esc(name)}"${name === slot.pedal ? ' selected' : ''}>${esc(name)}</option>`).join('')}
            </select>
            <label class="inline"><input type="checkbox" data-slot="${index}"
              data-slot-field="enabled"${slot.enabled === false ? '' : ' checked'} /> on</label>
            <label class="inline">from intensity
              <input type="number" data-slot="${index}" data-slot-field="at_intensity"
                value="${slot.at_intensity ?? 0}" min="0" max="1" step="0.05" /></label>
            <span class="spacer"></span>
            <button data-move="${index}" data-dir="-1" title="earlier"${index === 0 ? ' disabled' : ''}>↑</button>
            <button data-move="${index}" data-dir="1" title="later"${index === slots.length - 1 ? ' disabled' : ''}>↓</button>
            <button data-remove="${index}" title="remove">×</button>
          </div>
          <p class="dim">${esc(pedal?.summary ?? '')}</p>
          <div class="params">${params}</div>
        </li>`;
      })
      .join('');

    return this._section(
      'pedal chain',
      `<ol class="slots">${rows || '<li class="dim">no pedals — the data goes through untouched</li>'}</ol>
       <div class="row">
         <select id="studio-add-pedal">${names.map((n) => `<option>${esc(n)}</option>`).join('')}</select>
         <button data-action="add-pedal">add</button>
       </div>`,
      'Order matters, and each pedal draws its randomness from its position — ' +
      'moving one changes every pedal after it, deterministically. Editing here ' +
      'is fine because Python does the rendering; live toggling while a piece ' +
      'plays is a separate thing and still to come.',
    );
  }

  /** One parameter input, typed from the catalog's default value. */
  _pedalParam(index, key, fallback, params) {
    const current = params[key];
    const value = current === undefined ? '' : current;
    const label = `<span>${esc(key)}</span>`;
    const attrs = `data-slot="${index}" data-param="${esc(key)}"`;

    if (PARAM_CHOICES[key]) {
      const options = ['', ...PARAM_CHOICES[key]];
      return `<label class="param">${label}<select ${attrs}>
        ${options.map((o) => `<option value="${esc(o)}"${String(o) === String(value) ? ' selected' : ''}>${o || '(default)'}</option>`).join('')}
      </select></label>`;
    }
    if (key === 'scale') {
      const options = ['', ...Object.keys(this.catalog.scales)];
      return `<label class="param">${label}<select ${attrs}>
        ${options.map((o) => `<option value="${esc(o)}"${String(o) === String(value) ? ' selected' : ''}>${o || '(none — plain modulo)'}</option>`).join('')}
      </select></label>`;
    }
    if (key.endsWith('_lfo')) {
      return `<label class="param">${label}<input ${attrs} type="text"
        value="${esc(value)}" placeholder="${esc(LFO_HINT)}" /></label>`;
    }
    if (typeof fallback === 'boolean') {
      return `<label class="param inline"><input ${attrs} type="checkbox"
        ${value === true ? 'checked' : ''} /> ${esc(key)}</label>`;
    }
    if (typeof fallback === 'number') {
      return `<label class="param">${label}<input ${attrs} type="number"
        value="${esc(value)}" placeholder="${esc(fallback)}" step="any" /></label>`;
    }
    // Lists and nulls: JSON text. Honest about what it wants rather than
    // pretending a list is a string.
    const shown = Array.isArray(value) ? JSON.stringify(value) : value;
    return `<label class="param">${label}<input ${attrs} type="text"
      value="${esc(shown)}" placeholder="${esc(fallback === null ? '(default)' : JSON.stringify(fallback))}" /></label>`;
  }

  _mapping() {
    const defaults = this.catalog.mapping_defaults ?? {};
    const fields = [
      this._field('lowest note (MIDI)', 'preset.mapping.note_low', 'number', 'min="0" max="127"'),
      this._field('highest note (MIDI)', 'preset.mapping.note_high', 'number', 'min="0" max="127"'),
      this._select('frequency curve', 'preset.mapping.freq_curve', ['', 'log', 'direct']),
      this._select('quantize to scale', 'preset.mapping.quantize_to', [
        '',
        ...Object.keys(this.catalog.scales),
      ]),
      this._field('gate threshold', 'preset.mapping.gate_threshold', 'number', 'min="0" max="255"'),
      this._field('amplitude floor', 'preset.mapping.amp_floor', 'number',
        'min="0" max="1" step="0.01"'),
      this._field('visual density from delta', 'preset.mapping.density_from_delta', 'number',
        'min="0" max="1" step="0.05"'),
      this._field('glitch threshold', 'preset.mapping.glitch_threshold', 'number',
        'min="0" max="1" step="0.01"'),
      this._field('channel rotation', 'preset.mapping.channel_rotation', 'number', 'min="0" max="7"'),
    ].join('');

    return this._section(
      'mapping',
      fields +
        `<p class="dim">blank means the built-in default (note range ${defaults.note_low}–${defaults.note_high},
        ${esc(defaults.freq_curve)} curve).</p>`,
      'The subjective layer. Section 5 of the design document leaves it open on ' +
      'purpose — these are the numbers to argue with once you can hear the piece.',
    );
  }

  _envelope() {
    const envelope = this.get('preset.envelope', {}) || {};
    const kind = envelope.kind ?? (envelope.archetype ? 'archetype' : 'equation');
    return this._section(
      'intensity envelope',
      [
        this._select('kind', 'preset.envelope.kind', ['archetype', 'equation', 'constant', 'points']),
        kind === 'archetype'
          ? this._select('archetype', 'preset.envelope.archetype', this.catalog.archetypes) +
            this._field('curvature', 'preset.envelope.curvature', 'number', 'step="0.1" min="0.2" max="4"')
          : '',
        kind === 'equation'
          ? this._select('equation', 'preset.envelope.equation', this.catalog.equations)
          : '',
        kind === 'constant'
          ? this._field('level', 'preset.envelope.level', 'number', 'min="0" max="1" step="0.05"')
          : '',
        kind === 'points'
          ? '<p class="dim">a hand-drawn curve. draw it in the panel while the piece ' +
            'plays, then <b>freeze as preset</b> — or edit the points in the manifest.</p>'
          : '',
      ].join(''),
      'Archetypes are editable starting points, not a replacement for the free ' +
      'curve — both produce the same thing, a function from time to intensity.',
    );
  }

  _pieceSettings() {
    return this._section(
      'piece',
      [
        this._field('title', 'title'),
        `<label>notes<textarea data-path="notes" rows="3">${esc(this.get('notes'))}</textarea></label>`,
        this._select('mode', 'preset.piece.mode', this.catalog.modes),
        this._select('when the stream runs out', 'preset.piece.loop', this.catalog.loop_policies),
        this._select('voice entry order', 'preset.piece.voice_entry', this.catalog.voice_entry),
        this._select('delay time', 'preset.piece.delay_note', [
          '',
          ...Object.keys(this.catalog.tempo.note_fractions ?? {}),
        ]),
        this._field('seed override (blank = from the data)', 'preset.seed_override', 'number'),
      ].join(''),
    );
  }

  _performance() {
    const performance = this.get('performance', {}) || {};
    const samples = (performance.samples ?? []).length;
    const patterns = (performance.patterns ?? []).length;
    return this._section(
      'performance',
      `<div class="row">
         ${this._select('mode', 'performance.keyboard.mode', ['', 'random', 'notes'])}
         ${this._select('register', 'performance.keyboard.register',
           ['', 'bass', 'mid', 'treble', 'full', 'piece'])}
         ${this._select('timbre', 'performance.keyboard.waveform', ['', ...this.catalog.waveforms])}
       </div>
       ${this._field('level', 'performance.keyboard.level', 'number', 'min="0" max="1" step="0.05"')}
       <p class="dim">${samples} samples · ${patterns} patterns — the sample list and
       beat grid land here next.</p>`,
      'What you play, as opposed to what the data produces. Samples live only ' +
      'here — the eight data voices stay oscillators, because the generated ' +
      'sound is meant to be primitive.',
    );
  }

  /**
   * The key map editor.
   *
   * A picture of a keyboard, because a map *is* a layout and a table of
   * `KeyA → degree 0` rows does not read as one. Click a position to select it,
   * then bind it below.
   *
   * The labels come from the browser's layout map where it will say, so someone
   * on a Spanish keyboard sees their own keys -- while what gets stored is still
   * the physical position, which is what makes the map shareable.
   */
  _keymapEditor() {
    const keymap = this.get('performance.keymap', {}) || {};
    const scale = this._pieceScale();
    const bound = Object.keys(keymap).length;

    const rows = KEY_ROWS.map(
      (row) =>
        `<div class="keyrow">${row
          .map((code) => {
            const binding = keymap[code];
            const selected = code === this.selectedKey ? ' on' : '';
            const filled = binding ? ' bound' : '';
            const label = this.labels[code] ?? fallbackLabel(code);
            const detail = binding ? describeBinding(binding, scale).split(' · ')[0] : '';
            return `<button class="key${selected}${filled}" data-key="${esc(code)}"
              title="${esc(code)}${binding ? ` — ${esc(describeBinding(binding, scale))}` : ''}">
              <b>${esc(label)}</b><span>${esc(detail)}</span></button>`;
          })
          .join('')}</div>`,
    ).join('');

    return this._section(
      'key map',
      `<p class="dim">${bound} of ${ALL_KEYS.length} positions bound ·
        scale <b>${esc(scale.name)}</b> rooted at ${esc(noteName(scale.root))}</p>
       <div class="keyboard">${rows}</div>
       ${this._bindingEditor(keymap, scale)}
       <div class="row">
         <button data-action="keymap-default">two home rows</button>
         <button data-action="keymap-chromatic">every key</button>
         <button data-action="keymap-clear">clear</button>
       </div>`,
      'Bound by physical position, not by character — the map survives a change ' +
      'of keyboard layout, and a piece is meant to be shareable. Bindings are ' +
      'scale degrees rather than fixed pitches, so they stay in key when the ' +
      "chain changes the piece's scale.",
    );
  }

  /** The panel for whichever position is selected. */
  _bindingEditor(keymap, scale) {
    if (!this.selectedKey) {
      return '<p class="dim">click a key to bind it.</p>';
    }
    const code = this.selectedKey;
    const binding = keymap[code] ?? {};
    const kind = binding.kind ?? 'degree';
    const label = this.labels[code] ?? fallbackLabel(code);
    const samples = (this.get('performance.samples', []) || []).map((s) => s.id);
    const patterns = (this.get('performance.patterns', []) || []).map((p) => p.id);

    const kinds = ['degree', 'note'];
    // Only offered when the piece actually has something to point at: a binding
    // to a sample that does not exist is refused on save anyway.
    if (samples.length) kinds.push('sample');
    if (patterns.length) kinds.push('pattern');

    let fields = '';
    if (kind === 'degree') {
      fields =
        `<label>degree<input type="number" data-bind="degree"
          value="${binding.degree ?? 0}" step="1" /></label>
         <label>octave<input type="number" data-bind="octave"
          value="${binding.octave ?? 0}" step="1" min="-3" max="3" /></label>
         <p class="dim">plays <b>${esc(describeBinding({ kind: 'degree', degree: binding.degree ?? 0, octave: binding.octave ?? 0 }, scale))}</b></p>`;
    } else if (kind === 'note') {
      const midi = binding.midi ?? 60;
      fields =
        `<label>MIDI note<input type="number" data-bind="midi" value="${midi}"
          min="21" max="108" step="1" /></label>
         <p class="dim">plays <b>${esc(noteName(midi))}</b> — a fixed pitch, so it
         will not follow a change of scale.</p>`;
    } else if (kind === 'sample') {
      fields = `<label>sample<select data-bind="sample">
        ${samples.map((id) => `<option value="${esc(id)}"${id === binding.sample ? ' selected' : ''}>${esc(id)}</option>`).join('')}
      </select></label><p class="dim">not playable yet — the samples mode is next.</p>`;
    } else {
      fields = `<label>pattern<select data-bind="pattern">
        ${patterns.map((id) => `<option value="${esc(id)}"${id === binding.pattern ? ' selected' : ''}>${esc(id)}</option>`).join('')}
      </select></label><p class="dim">not playable yet — the beats mode is next.</p>`;
    }

    return `<div class="binding">
      <h4>${esc(label)} <span class="dim">(${esc(code)})</span></h4>
      <label>kind<select data-bind="kind">
        ${kinds.map((k) => `<option value="${esc(k)}"${k === kind ? ' selected' : ''}>${esc(k)}</option>`).join('')}
      </select></label>
      ${fields}
      <div class="row">
        <button data-action="binding-clear">unbind this key</button>
      </div>
    </div>`;
  }

  /**
   * The scale the map is written against.
   *
   * From the render when there is one, because that is what will actually sound.
   * Otherwise from the mapping the author has set, so the editor still shows
   * real note names on a piece that has never been generated.
   */
  _pieceScale() {
    const live = this.app.keyboard?.scale;
    if (this.get('render.fingerprint') && live) return live;
    const name = this.get('preset.mapping.quantize_to') || this.catalog.default_scale || 'pentatonic_minor';
    const entry = this.catalog.scales?.[name];
    return {
      name,
      offsets: entry?.offsets ?? [0, 3, 5, 7, 10],
      span: 12,
      root: Number(this.get('preset.mapping.note_low')) || this.catalog.mapping_defaults?.note_low || 33,
    };
  }

  _section(title, body, note = '') {
    return `<section class="studio-section">
      <h3>${esc(title)}</h3>
      ${note ? `<p class="dim note">${esc(note)}</p>` : ''}
      ${body}
    </section>`;
  }

  // -- wiring --------------------------------------------------------------
  _wireBody() {
    const body = $('studio-body');

    for (const input of body.querySelectorAll('[data-path]')) {
      const path = input.dataset.path;
      const handler = () => {
        let value;
        if (input.type === 'checkbox') value = input.checked;
        else if (input.type === 'number') value = input.value === '' ? '' : Number(input.value);
        else value = input.value;
        this.set(path, value);
        // Structural fields change which inputs exist at all.
        if (path === 'source.kind') {
          this.paint();
          this.loadSource();
        } else if (path === 'preset.envelope.kind') {
          this.paint();
        }
        if (path === 'source.path') this.loadSource();
      };
      input.addEventListener('change', handler);
    }

    for (const box of body.querySelectorAll('[data-column]')) {
      box.addEventListener('change', () => {
        const picked = [...body.querySelectorAll('[data-column]')]
          .filter((other) => other.checked)
          .map((other) => other.dataset.column);
        if (picked.length > this.catalog.max_voices) {
          box.checked = false;
          this.message(
            `at most ${this.catalog.max_voices} voices — that ceiling is a design ` +
              'constraint, not a technical one',
            true,
          );
          return;
        }
        this.set('source.columns', picked);
        this.paint();
      });
    }

    for (const select of body.querySelectorAll('[data-slot-field]')) {
      select.addEventListener('change', () => {
        const slot = this.chainSlots[Number(select.dataset.slot)];
        const field = select.dataset.slotField;
        if (field === 'enabled') slot.enabled = select.checked;
        else if (field === 'at_intensity') slot.at_intensity = Number(select.value) || 0;
        else {
          // Changing the pedal invalidates its parameters, so they are cleared
          // rather than carried over into a pedal that does not accept them.
          slot.pedal = select.value;
          slot.params = {};
        }
        this.dirty = true;
        this.paint();
      });
    }

    for (const input of body.querySelectorAll('[data-param]')) {
      input.addEventListener('change', () => {
        const slot = this.chainSlots[Number(input.dataset.slot)];
        const key = input.dataset.param;
        slot.params = slot.params ?? {};
        if (input.type === 'checkbox') slot.params[key] = input.checked;
        else if (input.value === '') delete slot.params[key];
        else if (input.type === 'number') slot.params[key] = Number(input.value);
        else slot.params[key] = this._parseParam(input.value);
        this.dirty = true;
        this._paintHeader();
      });
    }

    for (const button of body.querySelectorAll('[data-move]')) {
      button.addEventListener('click', () => {
        const from = Number(button.dataset.move);
        const to = from + Number(button.dataset.dir);
        const slots = this.chainSlots;
        if (to < 0 || to >= slots.length) return;
        [slots[from], slots[to]] = [slots[to], slots[from]];
        this.dirty = true;
        this.paint();
      });
    }

    for (const button of body.querySelectorAll('[data-remove]')) {
      button.addEventListener('click', () => {
        this.chainSlots.splice(Number(button.dataset.remove), 1);
        this.dirty = true;
        this.paint();
      });
    }

    for (const [action, accept] of [['pick-csv', '.csv,.tsv'], ['pick-graph', '.json']]) {
      body.querySelector(`[data-action="${action}"]`)?.addEventListener('click', () => {
        const input = $('studio-file');
        input.accept = accept;
        input.click();
      });
    }

    $('studio-file')?.addEventListener('change', async (event) => {
      const files = [...(event.target.files ?? [])];
      event.target.value = '';
      if (!files.length) return;
      if (files.length > 1) {
        // A piece has one source. Taking the first quietly is how two uploads
        // become one mystery.
        this.message(
          `a piece has one source: taking ${files[0].name}, ignoring ` +
            `${files.slice(1).map((f) => f.name).join(', ')}`,
          true,
        );
      }
      await this.putData(files[0]);
    });

    body.querySelector('[data-action="export-history"]')?.addEventListener('click', () =>
      this.exportHistory(),
    );

    body.querySelector('[data-action="add-pedal"]')?.addEventListener('click', () => {
      this.chainSlots.push({ pedal: $('studio-add-pedal').value, params: {} });
      this.dirty = true;
      this.paint();
    });

    for (const button of body.querySelectorAll('[data-key]')) {
      button.addEventListener('click', () => {
        // Toggling the selection off on a second click means the binding panel
        // does not stay open over a key you have finished with.
        this.selectedKey = this.selectedKey === button.dataset.key ? null : button.dataset.key;
        this.paint();
      });
    }

    for (const input of body.querySelectorAll('[data-bind]')) {
      input.addEventListener('change', () => {
        const field = input.dataset.bind;
        const keymap = { ...(this.get('performance.keymap', {}) || {}) };
        const current = { ...(keymap[this.selectedKey] ?? { kind: 'degree', degree: 0 }) };

        if (field === 'kind') {
          // Changing the kind invalidates the other fields, so it starts clean
          // rather than carrying a degree into a sample binding.
          const fresh = { kind: input.value };
          if (input.value === 'degree') Object.assign(fresh, { degree: 0, octave: 0 });
          if (input.value === 'note') fresh.midi = 60;
          keymap[this.selectedKey] = fresh;
        } else {
          current[field] = input.type === 'number' ? Number(input.value) : input.value;
          keymap[this.selectedKey] = current;
        }
        this.set('performance.keymap', keymap);
        this.paint();
      });
    }

    body.querySelector('[data-action="binding-clear"]')?.addEventListener('click', () => {
      const keymap = { ...(this.get('performance.keymap', {}) || {}) };
      delete keymap[this.selectedKey];
      this.set('performance.keymap', keymap);
      this.selectedKey = null;
      this.paint();
    });

    body.querySelector('[data-action="keymap-default"]')?.addEventListener('click', () => {
      this.set('performance.keymap', defaultKeymap());
      this.paint();
    });

    body.querySelector('[data-action="keymap-chromatic"]')?.addEventListener('click', () => {
      this.set('performance.keymap', chromaticKeymap());
      this.paint();
    });

    body.querySelector('[data-action="keymap-clear"]')?.addEventListener('click', () => {
      this.set('performance.keymap', {});
      this.selectedKey = null;
      this.paint();
    });

    body.querySelector('[data-action="columns-auto"]')?.addEventListener('click', () => {
      // Deleting the key is not the same as an empty list: absent means "let the
      // pipeline choose", empty would mean "no voices at all".
      delete this.manifest.source.columns;
      this.dirty = true;
      this.paint();
    });
  }

  /** A text parameter that might be a list, a number or a string. */
  _parseParam(text) {
    const trimmed = text.trim();
    if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
      try {
        return JSON.parse(trimmed);
      } catch {
        return trimmed; // let Python complain with a better message than this
      }
    }
    if (trimmed !== '' && !Number.isNaN(Number(trimmed))) return Number(trimmed);
    return trimmed;
  }
}
