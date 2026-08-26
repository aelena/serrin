/**
 * The console: a devtools drawer for the piece.
 *
 * The panel answers "what should this do"; this answers "what did it just do".
 * They are different questions and they wanted different surfaces -- a knob and
 * a readout do not belong in the same column.
 *
 * Five tabs, each earning its place by answering a question that previously had
 * no answer at all:
 *
 *   log       what happened, in order, including warnings the page swallowed
 *   pipeline  what each pedal did to the numbers -- the trace from Python
 *   meta      everything the render says about itself
 *   frame     the values going past right now, per voice, both forks
 *   audio     the node graph, and the calls actually being scheduled
 *
 * The `audio` tab is where "inspect the code that produces the sounds" lands,
 * and it needs one honest caveat: **serrin does not generate JavaScript**. The
 * sound comes from a fixed graph driven by data, so there is no generated source
 * to dump. What the tab offers instead is a *reconstruction* -- a runnable
 * snippet that rebuilds the graph and replays the events actually scheduled.
 * That is the real answer to the question, and calling it a reconstruction
 * rather than a dump is the difference between a tool and a lie.
 */

const MAX_LOG = 500;

/** Escape for insertion as text. Log lines can contain anything. */
function esc(text) {
  return String(text).replace(
    /[&<>"']/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch],
  );
}

function num(value, places = 3) {
  if (value === null || value === undefined) return '—';
  if (typeof value !== 'number') return esc(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(places);
}

export class DebugConsole {
  constructor(app) {
    this.app = app;
    this.root = document.getElementById('console');
    this.body = document.getElementById('console-body');
    this.visible = false;
    this.tab = 'log';
    this.entries = [];
    this.trace = null;
    this.paused = false;
    this._lastPaint = 0;

    this._wireTabs();
    this._captureConsole();
    this.log('console ready', 'system');
  }

  // -- logging -------------------------------------------------------------
  /**
   * Record an event. `level` is one of system/info/warn/error/audio/render.
   *
   * Kept as structured entries rather than formatted strings so the filter can
   * work on them and so a future export has something to export.
   */
  log(message, level = 'info', detail = null) {
    this.entries.push({
      at: performance.now(),
      stamp: new Date().toISOString().slice(11, 23),
      level,
      message: String(message),
      detail,
    });
    if (this.entries.length > MAX_LOG) this.entries.splice(0, this.entries.length - MAX_LOG);
    if (this.visible && this.tab === 'log') this._render();
  }

  /**
   * Route the page's own console.warn/error here too.
   *
   * The reader already warns when two exports disagree about frame count, and
   * that warning went to a devtools window nobody had open. Anything worth
   * printing is worth showing in the tool.
   */
  _captureConsole() {
    for (const level of ['warn', 'error']) {
      const original = console[level].bind(console);
      console[level] = (...args) => {
        original(...args);
        this.log(args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' '), level);
      };
    }
    window.addEventListener('error', (event) => this.log(event.message, 'error'));
    window.addEventListener('unhandledrejection', (event) =>
      this.log(`unhandled rejection: ${event.reason}`, 'error'),
    );
  }

  // -- visibility ----------------------------------------------------------
  toggle(force) {
    this.visible = force ?? !this.visible;
    this.root.hidden = !this.visible;
    // Journalling is expensive enough to be worth switching off when nobody is
    // looking: a hundred objects a second, retained.
    this.app.audio?.setJournalling?.(this.visible && this.tab === 'audio');
    if (this.visible) this._render();
  }

  _wireTabs() {
    for (const button of this.root.querySelectorAll('[data-tab]')) {
      button.addEventListener('click', () => {
        this.tab = button.dataset.tab;
        for (const other of this.root.querySelectorAll('[data-tab]')) {
          other.classList.toggle('on', other === button);
        }
        this.app.audio?.setJournalling?.(this.tab === 'audio');
        this._render();
      });
    }
    document.getElementById('console-close').addEventListener('click', () => this.toggle(false));
    document.getElementById('console-clear').addEventListener('click', () => {
      this.entries = [];
      this.app.audio?.journal?.splice(0);
      this._render();
    });
    document.getElementById('console-pause').addEventListener('click', (event) => {
      this.paused = !this.paused;
      event.target.classList.toggle('on', this.paused);
      event.target.textContent = this.paused ? 'resume' : 'pause';
    });
    document.getElementById('console-copy').addEventListener('click', () => this._copy());
  }

  /** Called from the rAF loop. Throttled: this is a readout, not an animation. */
  tick() {
    if (!this.visible || this.paused) return;
    const live = this.tab === 'frame' || this.tab === 'audio';
    if (!live) return;
    const now = performance.now();
    if (now - this._lastPaint < 120) return;
    this._lastPaint = now;
    this._render();
  }

  setTrace(trace, label = '') {
    this.trace = trace;
    if (trace) {
      this.log(
        `trace received: ${trace.stages.length} stages, window ${trace.window} frames` +
          (label ? ` (${label})` : ''),
        'render',
      );
    }
    if (this.visible && this.tab === 'pipeline') this._render();
  }

  // -- rendering -----------------------------------------------------------
  _render() {
    const painters = {
      log: () => this._log(),
      pipeline: () => this._pipeline(),
      meta: () => this._meta(),
      frame: () => this._frame(),
      audio: () => this._audio(),
    };
    this.body.innerHTML = (painters[this.tab] ?? painters.log)();
  }

  _log() {
    if (!this.entries.length) return '<p class="dim">nothing logged yet</p>';
    const rows = this.entries
      .slice()
      .reverse()
      .map(
        (entry) =>
          `<tr class="lvl-${esc(entry.level)}"><td class="stamp">${esc(entry.stamp)}</td>` +
          `<td class="lvl">${esc(entry.level)}</td><td>${esc(entry.message)}</td></tr>`,
      )
      .join('');
    return `<table class="ctable log">${rows}</table>`;
  }

  _pipeline() {
    if (!this.trace) {
      return (
        '<p class="dim">no trace loaded. render something from the <b>source</b> ' +
        'section with tracing on, or run:<br>' +
        '<code>python -m serrin render -i data/monitoring.csv --trace out/trace.json</code></p>'
      );
    }

    const blocks = this.trace.stages.map((stage) => {
      const changed = stage.detail?.changed_fraction;
      const entropy = stage.detail?.entropy_delta;
      const badge =
        changed === undefined
          ? ''
          : `<span class="badge">${(changed * 100).toFixed(0)}% moved</span>` +
            `<span class="badge ${entropy >= 0 ? 'up' : 'down'}">` +
            `entropy ${entropy >= 0 ? '+' : ''}${entropy.toFixed(2)} bits</span>`;

      const params = stage.params
        ? Object.entries(stage.params)
            .filter(([, v]) => v !== null && v !== undefined)
            .map(([k, v]) => `${esc(k)}=<b>${esc(JSON.stringify(v))}</b>`)
            .join('  ')
        : '';

      const channels = (stage.channels ?? [])
        .map((channel) => {
          const s = channel.stats;
          const values = channel.values
            .slice(0, 48)
            .map((v) => String(v).padStart(3, ' '))
            .join(' ');
          return (
            `<tr><td class="name">${esc(channel.name)}</td>` +
            `<td>${s.min}–${s.max}</td><td>${s.unique}</td>` +
            `<td>${s.entropy.toFixed(2)}</td>` +
            `<td>${(s.change_rate * 100).toFixed(0)}%</td>` +
            `<td>${s.flat_longest}</td>` +
            `<td class="values">${esc(values)}${channel.truncated ? ' …' : ''}</td></tr>`
          );
        })
        .join('');

      const table = channels
        ? `<table class="ctable"><thead><tr><th>voice</th><th>range</th><th>uniq</th>` +
          `<th>H bits</th><th>change</th><th>flat run</th><th>values (window)</th>` +
          `</tr></thead><tbody>${channels}</tbody></table>`
        : '';

      return (
        `<section class="stage"><h3>[${stage.index}] <span class="kind">${esc(stage.kind)}</span> ` +
        `${esc(stage.name)} ${badge}</h3>` +
        (params ? `<p class="params">${params}</p>` : '') +
        (stage.note ? `<p class="dim">${esc(stage.note)}</p>` : '') +
        table +
        this._stageDetail(stage) +
        '</section>'
      );
    });

    return (
      `<p class="dim">values shown are the first ${this.trace.window} frames; ` +
      'statistics are over the whole stream.</p>' + blocks.join('')
    );
  }

  /** Stage-specific extras: the conversion table, the fork, branch ownership. */
  _stageDetail(stage) {
    const detail = stage.detail ?? {};
    let out = '';

    for (const conversion of detail.conversions ?? []) {
      const rows = conversion.cells
        .slice(0, 16)
        .map(
          (cell, i) =>
            `<tr><td class="values">${esc(cell)}</td>` +
            `<td>${num(conversion.parsed[i], 4)}</td>` +
            `<td>${num(conversion.aggregated[i], 4)}</td>` +
            `<td><b>${conversion.bytes[i]}</b></td></tr>`,
        )
        .join('');
      out +=
        `<details open><summary>${esc(conversion.name)}: cell → number → byte</summary>` +
        `<p class="dim">normalized against its own range ` +
        `${num(conversion.range.low, 4)} … ${num(conversion.range.high, 4)}` +
        (conversion.range.log_scale ? ' (log)' : '') +
        (conversion.unparseable_cells
          ? ` · ${conversion.unparseable_cells} unparseable cells held the previous value`
          : '') +
        `</p><table class="ctable"><thead><tr><th>cell</th><th>parsed</th>` +
        `<th>aggregated</th><th>byte</th></tr></thead><tbody>${rows}</tbody></table></details>`;
    }

    if (detail.owned) {
      const rows = Object.entries(detail.owned)
        .map(([ref, count]) => `<tr><td class="name">${esc(ref)}</td><td>${count} commits</td></tr>`)
        .join('');
      out +=
        `<details open><summary>branch ownership</summary>` +
        `<p class="dim">trunk <b>${esc(detail.trunk ?? '?')}</b> · ${detail.merges} merges · ` +
        `${detail.authors} authors` +
        (detail.dropped_branches?.length
          ? ` · dropped (no commits of their own): ${esc(detail.dropped_branches.join(', '))}`
          : '') +
        `</p><table class="ctable"><tbody>${rows}</tbody></table></details>`;
    }

    if (detail.examples) {
      const rows = detail.examples
        .flatMap((row) =>
          row.voices.map(
            (v, i) =>
              `<tr><td>${i === 0 ? row.frame : ''}</td><td class="name">${esc(v.voice)}</td>` +
              `<td><b>${v.byte}</b></td><td>${num(v.freq, 1)} Hz</td>` +
              `<td>${num(v.amp)}</td><td>${v.gate}</td>` +
              `<td>${num(v.x)}</td><td>${num(v.y)}</td><td>${num(v.density)}</td>` +
              `<td class="values">${esc(v.glyph)}</td><td>${v.glitch ? '●' : ''}</td></tr>`,
          ),
        )
        .join('');
      out +=
        `<details open><summary>one byte, two readings</summary>` +
        `<p class="dim">audio: ${esc(detail.audio_reads)}<br>visual: ${esc(detail.visual_reads)}</p>` +
        `<table class="ctable"><thead><tr><th>frame</th><th>voice</th><th>byte</th>` +
        `<th>freq</th><th>amp</th><th>gate</th><th>x</th><th>y</th><th>density</th>` +
        `<th>glyph</th><th>glitch</th></tr></thead><tbody>${rows}</tbody></table></details>`;
    }
    return out;
  }

  _meta() {
    const meta = this.app.reader?.meta ?? {};
    return (
      '<p class="dim">everything the render says about itself. this is the ' +
      '<code>meta</code> block of both exported documents.</p>' +
      `<pre class="json">${esc(JSON.stringify(meta, null, 2))}</pre>`
    );
  }

  _frame() {
    const frame = this.app.transport?.lastFrame;
    if (!frame) return '<p class="dim">press play</p>';
    const state = this.app.transport.lastState ?? {};
    const tempo = this.app.reader.tempo;

    const rows = frame.audio
      .map((a, index) => {
        const v = frame.visual[index] ?? {};
        const gated = state.gates ? state.gates[index] === false : false;
        const muted = this.app.audio?.mutes?.has(index);
        return (
          `<tr class="${gated ? 'gated' : ''}"><td class="name">${esc(a.name)}</td>` +
          `<td>${esc(a.waveform)}</td><td>${num(a.freq, 1)}</td><td>${num(a.amp)}</td>` +
          `<td>${num(a.dur)}</td><td>${a.gate}</td>` +
          `<td>${num(v.x)}</td><td>${num(v.y)}</td><td>${num(v.density)}</td>` +
          `<td>${num(v.gray)}</td><td>${v.glitch ? '●' : ''}</td>` +
          `<td>${gated ? 'gated' : muted ? 'muted' : ''}</td></tr>`
        );
      })
      .join('');

    return (
      `<p class="dim">frame <b>${frame.counter}</b> → index ${frame.index} · ` +
      `bar ${tempo.formatPosition(frame.counter)} · pass ${frame.pass} · ` +
      `intensity ${num(state.intensity)} · ` +
      `${this.app.reader.voiceCount} voices @ ${num(tempo.rate)} fps</p>` +
      '<table class="ctable"><thead><tr><th>voice</th><th>wave</th><th>freq</th>' +
      '<th>amp</th><th>dur</th><th>gate</th><th>x</th><th>y</th><th>density</th>' +
      '<th>gray</th><th>glitch</th><th></th></tr></thead>' +
      `<tbody>${rows}</tbody></table>`
    );
  }

  _audio() {
    const audio = this.app.audio;
    if (!audio?.started) return '<p class="dim">press play — there is no graph yet</p>';
    const graph = audio.graph();

    const nodes = graph.nodes
      .map(
        (node) =>
          `<tr><td class="name">${esc(node.id)}</td><td>${esc(node.type)}</td>` +
          `<td class="dim">${esc(node.detail)}</td></tr>`,
      )
      .join('');

    const journal = audio.journal.slice(-40).reverse();
    const events = journal.length
      ? journal
          .map(
            (event) =>
              `<tr><td>${event.time.toFixed(3)}</td>` +
              `<td class="name">${esc(event.name)}${event.midi ? ` (${event.midi})` : ''}</td>` +
              `<td>${esc(event.waveform)}</td><td>${num(event.freq, 1)}</td>` +
              `<td>${num(event.amp)}</td><td>${event.retrigger ? 'trigger' : 'slide'}</td>` +
              `<td class="values">${esc(event.calls.join('; '))}</td></tr>`,
          )
          .join('')
      : '<tr><td colspan="7" class="dim">nothing scheduled yet</td></tr>';

    return (
      `<p class="dim">${graph.sampleRate} Hz · context ${esc(graph.state)}` +
      (graph.baseLatency ? ` · base latency ${(graph.baseLatency * 1000).toFixed(1)} ms` : '') +
      '</p>' +
      '<details open><summary>the graph</summary>' +
      `<table class="ctable"><tbody>${nodes}</tbody></table>` +
      `<pre class="json">${esc(graph.edges.join('\n'))}</pre></details>` +
      '<details open><summary>scheduled events (most recent first)</summary>' +
      '<table class="ctable"><thead><tr><th>ctx time</th><th>voice</th><th>wave</th>' +
      '<th>Hz</th><th>amp</th><th>mode</th><th>calls</th></tr></thead>' +
      `<tbody>${events}</tbody></table></details>` +
      '<p class="dim">serrin does not generate JavaScript — the sound comes from a ' +
      'fixed graph driven by data. <b>copy</b> emits a <i>reconstruction</i>: a ' +
      'runnable snippet that rebuilds this graph and replays the events above.</p>'
    );
  }

  // -- copy ----------------------------------------------------------------
  _copy() {
    let payload;
    if (this.tab === 'audio') payload = reconstructJs(this.app.audio, this.app.reader?.meta);
    else if (this.tab === 'pipeline') payload = JSON.stringify(this.trace ?? {}, null, 2);
    else if (this.tab === 'meta') payload = JSON.stringify(this.app.reader?.meta ?? {}, null, 2);
    else if (this.tab === 'frame') payload = JSON.stringify(this.app.transport?.lastFrame ?? {}, null, 2);
    else payload = this.entries.map((e) => `${e.stamp} ${e.level} ${e.message}`).join('\n');

    navigator.clipboard?.writeText(payload).then(
      () => this.log(`copied ${this.tab} (${payload.length} chars)`, 'system'),
      () => this.log('the clipboard refused; the payload is in the console', 'warn'),
    );
    return payload;
  }

}

/**
 * Emit runnable JavaScript that reproduces journalled audio events.
 *
 * A reconstruction, not a dump: **serrin does not generate JavaScript.** The
 * sound comes from a fixed graph driven by data, so there is no generated source
 * to show. What this produces instead is genuinely runnable -- paste it into any
 * browser console and it rebuilds the effect chain and replays what was
 * scheduled -- which makes it useful for asking "why did that sound like that"
 * outside serrin entirely.
 *
 * A free function rather than a method, so it can be tested without a DOM. The
 * code generator has nothing to do with the drawer that displays it.
 *
 * @param {import('./audio.js').AudioEngine} audio
 * @param {object} meta the render's metadata, for the provenance comment
 * @param {number} limit how many of the most recent events to include
 */
export function reconstructJs(audio, meta = {}, limit = 40) {
  if (!audio?.started) return '// nothing to reconstruct: the audio graph is not running';
  const graph = audio.graph();
  const events = audio.journal.slice(-limit);
  if (!events.length) return '// nothing scheduled yet -- play a few frames first';

  const base = events[0].time;
  const lines = [
    '// serrin -- reconstruction of scheduled audio, not generated source.',
    '// The piece has no generated JavaScript: the sound comes from a fixed',
    '// graph driven by data. This rebuilds that graph and replays the last',
    `// ${events.length} events, with times made relative to the first.`,
    `// render: ${meta.label ?? 'unknown'}`,
    '',
    'const ctx = new AudioContext();',
    'const t0 = ctx.currentTime + 0.1;',
    '',
    '// -- the effect chain --------------------------------------------------',
    'const master = ctx.createGain();',
    `master.gain.value = ${audio.masterGain.toFixed(3)};`,
    'const crusher = ctx.createWaveShaper();',
    'crusher.curve = (bits => {',
    '  const steps = 2 ** bits, curve = new Float32Array(4096);',
    '  for (let i = 0; i < 4096; i += 1) {',
    '    const x = (i / 4095) * 2 - 1;',
    '    curve[i] = Math.round(x * steps) / steps;',
    '  }',
    '  return curve;',
    `})(${audio.crushBits});`,
    "crusher.oversample = 'none';  // aliasing is the point",
    'const tone = ctx.createBiquadFilter();',
    "tone.type = 'lowpass';",
    `tone.frequency.value = ${audio.filterCutoff.toFixed(1)};`,
    'const delay = ctx.createDelay(4);',
    `delay.delayTime.value = ${audio.delaySeconds().toFixed(4)};  // ${audio.delayNote}`,
    'const feedback = ctx.createGain();',
    'feedback.gain.value = 0.34;',
    'const delaySend = ctx.createGain();',
    `delaySend.gain.value = ${audio.delayMix.toFixed(3)};`,
    'const bus = ctx.createGain();',
    '',
    'bus.connect(crusher).connect(tone).connect(master);',
    'tone.connect(delaySend).connect(delay).connect(master);',
    'delay.connect(feedback).connect(delay);',
    'master.connect(ctx.destination);',
    '',
    '// -- the events --------------------------------------------------------',
  ];

  for (const event of events) {
    const at = (event.time - base).toFixed(4);
    lines.push(
      `// ${event.name}${event.midi ? ` -- MIDI ${event.midi}` : ''}` +
        ` -- ${event.retrigger ? 'new note' : 'slide, the data barely moved'}`,
      '{',
      '  const osc = ctx.createOscillator(), gain = ctx.createGain();',
      `  osc.type = '${event.waveform}';`,
      `  osc.frequency.value = ${event.freq.toFixed(3)};`,
      `  const t = t0 + ${at};`,
      '  gain.gain.setValueAtTime(0, t);',
      `  gain.gain.linearRampToValueAtTime(${event.amp.toFixed(4)}, t + ${event.attack.toFixed(4)});`,
      `  gain.gain.setTargetAtTime(0.0001, t + ${event.attack.toFixed(4)}, ${(event.hold * 0.4).toFixed(4)});`,
      '  osc.connect(gain).connect(bus);',
      `  osc.start(t); osc.stop(t + ${(event.attack + event.hold + 0.1).toFixed(4)});`,
      '}',
    );
  }

  lines.push('', `// graph as built: ${graph.edges.join(' | ')}`);
  return lines.join('\n');
}
