/**
 * Visual engine: Canvas2D, monochrome, ASCII, deliberately unkind.
 *
 * Section 4.4's constraint is the one that shapes everything here: *the grit
 * comes from the data, not from a post-process*. So there is no CRT shader, no
 * film-grain overlay, no random flicker. Every displaced block, every dropped
 * scanline, every dense glyph is keyed to a value the pipeline actually produced
 * -- the `glitch` flag is a real spike in the transformed stream, and if the
 * data is calm the screen is calm.
 *
 * The one exception is admitted openly: the horizontal scan sweep is drawn on a
 * clock, not on the data. It is there as a reading position -- a playhead -- and
 * without it a still frame is unreadable as time.
 *
 * Layers, back to front:
 *   1. field       -- banding whose height comes from flatness (stuck data)
 *   2. waterfall   -- a scrolling history of glyph rows, one column per frame
 *   3. bars        -- per-voice level, hard-edged, no gradients
 *   4. corruption  -- block displacement on glitch frames
 *   5. scan        -- the sweep, plus the label
 */

const MONO = '"Cascadia Mono", "DejaVu Sans Mono", "Consolas", monospace';

export class VisualEngine {
  constructor(canvas, reader) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false });
    this.reader = reader;

    // Ring buffer of recent frames. The waterfall is the main way the piece
    // shows *history*, which a per-frame render cannot.
    this.historyLength = 240;
    this.history = [];

    // Live controls (4.5).
    this.balance = 0.5; // visual's share rises as this falls
    this.showGlyphs = true;
    this.showBars = true;
    this.showBanding = true;
    this.corruption = 1.0;
    this.invert = false;
    this.hidden = new Set();

    this.glyphs = reader.glyphs ?? ' .:-=+*#%@';
    this.cell = 12;
    this.frameCount = 0;
    this.lastGlitchAt = -1;

    this.resize();
    window.addEventListener('resize', () => this.resize());
  }

  resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const width = this.canvas.clientWidth || window.innerWidth;
    const height = this.canvas.clientHeight || window.innerHeight;
    this.canvas.width = Math.floor(width * dpr);
    this.canvas.height = Math.floor(height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.width = width;
    this.height = height;
    this.cols = Math.max(24, Math.floor(width / this.cell));
    this.historyLength = this.cols;
  }

  /** Called once per data frame (the same frames the audio engine schedules). */
  update(frame, state = {}) {
    if (!frame) return;
    this.history.push({
      visual: frame.visual,
      intensity: state.intensity ?? 1,
      gates: state.gates,
      index: frame.index,
      pass: frame.pass,
    });
    while (this.history.length > this.historyLength) this.history.shift();
    this.frameCount += 1;

    const glitching = frame.visual.some((v) => v.glitch);
    if (glitching) this.lastGlitchAt = this.frameCount;
  }

  /**
   * Called from requestAnimationFrame. Deliberately separate from update():
   * "decoupled in temporal resolution but reading from the same shared state"
   * (section 4.2) -- the data may advance at 8 Hz while this draws at 60.
   */
  render(now, transport) {
    const ctx = this.ctx;
    const w = this.width;
    const h = this.height;
    const latest = this.history[this.history.length - 1];
    const intensity = latest?.intensity ?? 1;
    const weight = 1.35 - this.balance; // inverse of the audio's share

    // Background. Not pure black: a near-black with a slight lift reads as a
    // surface rather than as a void, which makes the white glyphs sit on top of
    // something instead of floating.
    ctx.fillStyle = this.invert ? '#e8e8e4' : '#07070a';
    ctx.fillRect(0, 0, w, h);

    if (!latest) {
      this._drawIdle(ctx, w, h);
      return;
    }

    if (this.showBanding) this._drawField(ctx, w, h, latest, intensity);
    if (this.showGlyphs) this._drawWaterfall(ctx, w, h, weight);
    if (this.showBars) this._drawBars(ctx, w, h, latest, weight);
    if (this.corruption > 0) this._drawCorruption(ctx, w, h, latest, intensity);
    this._drawScan(ctx, w, h, now, transport, intensity);
  }

  // -- layer 1: banding from flatness ------------------------------------
  _drawField(ctx, w, h, latest, intensity) {
    // Stuck data bands the screen. This is the visual equivalent of the audio
    // side sliding into a drone: nothing is moving, so the image stratifies.
    let flatness = 0;
    for (const v of latest.visual) flatness = Math.max(flatness, v.flat ?? 0);
    if (flatness < 0.05) return;

    const bandHeight = Math.max(2, Math.round(6 + flatness * 26));
    const ink = this.invert ? 0 : 255;
    for (let y = 0; y < h; y += bandHeight * 2) {
      const alpha = 0.03 + flatness * 0.09 * (1 - intensity * 0.4);
      ctx.fillStyle = `rgba(${ink},${ink},${ink},${alpha.toFixed(3)})`;
      ctx.fillRect(0, y, w, bandHeight);
    }
  }

  // -- layer 2: the ASCII waterfall ---------------------------------------
  _drawWaterfall(ctx, w, h, weight) {
    const rows = this.reader.voiceCount || 1;
    const rowHeight = Math.min(this.cell * 2.2, h / (rows + 2));
    const fontSize = Math.max(8, Math.min(22, rowHeight * 0.82));
    ctx.font = `${fontSize}px ${MONO}`;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'center';

    const top = (h - rows * rowHeight) / 2;
    const columnWidth = w / this.historyLength;

    for (let col = 0; col < this.history.length; col += 1) {
      const entry = this.history[col];
      // Age fade: the present is bright, the past decays. One of the few places
      // where drawing is a function of the clock rather than the data.
      const age = (this.history.length - col) / this.history.length;
      const x = col * columnWidth + columnWidth / 2;

      for (const v of entry.visual) {
        if (this.hidden.has(v.voice)) continue;
        if (entry.gates && entry.gates[v.voice] === false) continue;

        const glyphIndex = Math.max(0, Math.min(this.glyphs.length - 1, v.glyph));
        const char = this.glyphs[glyphIndex];
        if (char === ' ') continue;

        const y = top + v.voice * rowHeight + rowHeight / 2;
        // Vertical jitter from the voice's own y channel -- the rotated one, so
        // a voice's position is not a readout of its own pitch (section 3.5).
        const wobble = (v.y - 0.5) * rowHeight * 0.55;
        const alpha = Math.min(1, (0.12 + v.density * 0.95) * (1 - age * 0.72) * weight);

        if (v.glitch) {
          // A real spike: draw it as a solid block, not a character. The image
          // corrupts where the data does.
          ctx.fillStyle = this.invert ? `rgba(0,0,0,${alpha})` : `rgba(255,255,255,${alpha})`;
          ctx.fillRect(x - columnWidth / 2, y - rowHeight * 0.36, columnWidth, rowHeight * 0.72);
          continue;
        }

        const grey = Math.round((this.invert ? 1 - v.gray : v.gray) * 255);
        ctx.fillStyle = `rgba(${grey},${grey},${grey},${alpha.toFixed(3)})`;
        ctx.fillText(char, x, y + wobble);
      }
    }
  }

  // -- layer 3: per-voice bars --------------------------------------------
  _drawBars(ctx, w, h, latest, weight) {
    const count = latest.visual.length || 1;
    const gutter = 2;
    const barWidth = (w - gutter * (count + 1)) / count;
    const maxHeight = h * 0.16;

    for (const v of latest.visual) {
      if (this.hidden.has(v.voice)) continue;
      const gated = latest.gates && latest.gates[v.voice] === false;
      const value = gated ? 0 : v.density * weight;
      const barHeight = Math.max(1, value * maxHeight);
      const x = gutter + v.voice * (barWidth + gutter);
      const ink = this.invert ? 0 : 255;
      ctx.fillStyle = `rgba(${ink},${ink},${ink},${gated ? 0.08 : 0.42})`;
      ctx.fillRect(x, h - barHeight, barWidth, barHeight);

      // Tick at full scale so the bar reads against something.
      ctx.fillStyle = `rgba(${ink},${ink},${ink},0.14)`;
      ctx.fillRect(x, h - maxHeight, barWidth, 1);
    }
  }

  // -- layer 4: corruption -------------------------------------------------
  _drawCorruption(ctx, w, h, latest, intensity) {
    const sinceGlitch = this.frameCount - this.lastGlitchAt;
    if (this.lastGlitchAt < 0 || sinceGlitch > 3) return;

    // Block displacement: lift a horizontal slab and put it back offset. The
    // slab's size and offset come from the spiking voice's own values, so the
    // corruption is the data, restated as a graphics failure.
    const spikes = latest.visual.filter((v) => v.glitch && !this.hidden.has(v.voice));
    for (const v of spikes) {
      const sliceHeight = Math.max(4, Math.round(v.density * h * 0.13 * this.corruption));
      const y = Math.round(v.y * (h - sliceHeight));
      const shift = Math.round((v.x - 0.5) * w * 0.22 * this.corruption * (0.4 + intensity));
      if (!sliceHeight || !shift) continue;
      try {
        ctx.drawImage(this.canvas, 0, y, w, sliceHeight, shift, y, w, sliceHeight);
      } catch {
        // drawImage from the same canvas can fail mid-resize; a dropped frame of
        // corruption is not worth an exception.
      }
    }
  }

  // -- layer 5: the sweep and the label ------------------------------------
  _drawScan(ctx, w, h, now, transport, intensity) {
    const ink = this.invert ? 0 : 255;
    const period = 4200; // ms per sweep
    const x = ((now % period) / period) * w;
    const gradient = ctx.createLinearGradient(x - 60, 0, x + 6, 0);
    gradient.addColorStop(0, `rgba(${ink},${ink},${ink},0)`);
    gradient.addColorStop(1, `rgba(${ink},${ink},${ink},${(0.05 + intensity * 0.09).toFixed(3)})`);
    ctx.fillStyle = gradient;
    ctx.fillRect(x - 60, 0, 66, h);

    ctx.font = `10px ${MONO}`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillStyle = `rgba(${ink},${ink},${ink},0.30)`;
    const meta = this.reader.meta;
    const bits = [
      meta.label ?? 'serrin',
      `${meta.mode ?? 'closed'}/${meta.loop_policy ?? 'vary'}`,
      `int ${intensity.toFixed(2)}`,
    ];
    if (transport?.pass > 0) bits.push(`pass ${transport.pass}`);
    ctx.fillText(bits.join('   '), 8, 8);
  }

  _drawIdle(ctx, w, h) {
    const ink = this.invert ? 0 : 255;
    ctx.font = `12px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = `rgba(${ink},${ink},${ink},0.35)`;
    ctx.fillText('press space to begin  ·  p for the panel', w / 2, h / 2);
  }

  toggleVoice(index) {
    if (this.hidden.has(index)) this.hidden.delete(index);
    else this.hidden.add(index);
    return !this.hidden.has(index);
  }

  clear() {
    this.history = [];
    this.lastGlitchAt = -1;
  }
}
