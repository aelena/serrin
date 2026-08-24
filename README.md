# serrin

**An opinionated, entropic noise generation tool.**

Any boring data stream — server monitoring logs, tide data, a UVB-76 byte dump —
is run through a chain of small, dumb transformations ("guitar pedals") and the
result drives a browser audiovisual loop: primitive waveforms, ASCII, corrupted
data as a visual language.

Data stops mattering for its meaning. What matters is its shape: entropy,
periodicity, bursts, silences.

```
CSV ──► ingest ──► pedal chain ──► ┬──► stream_audio.json  ──► Web Audio
        (Python, offline)          └──► stream_visual.json ──► Canvas2D
                                        (JS, one clock, one tick)
```

Status: **v0.1**, phase 1 of the roadmap. Python renders; the browser plays.
No dependencies on either side — Python stdlib only, no npm, no build step.

---

## Quick start

```bash
# 1. make some data with the right shapes in it (periodicity, a burst, a flatline)
python scripts/make_sample_data.py

# 2. render a piece
python -m serrin render -i data/monitoring.csv -c presets/gritty_01.json

# 3. play it
python scripts/serve.py            # opens http://localhost:8000/web/
```

Press <kbd>space</kbd> to begin, <kbd>p</kbd> for the author panel.

A browser will not make sound until you ask it to, which is why there is a
"begin" gate. Opening `web/index.html` directly from disk will *not* work —
ES modules and `fetch` both refuse `file://`. Hence the tiny server.

---

## The pipeline (Python)

One entry point, five subcommands:

```bash
python -m serrin render   -i data.csv -c presets/gritty_01.json   # the main event
python -m serrin inspect  -i data.csv                             # look before rendering
python -m serrin catalog                                          # pedals + parameters
python -m serrin scales                                           # the scale bank
python -m serrin curve --archetype climax                         # preview an envelope as ASCII
```

`inspect` is the one to reach for first — it shows which columns were chosen,
their variance, the derived seed, and the order voices would enter in.

### Ingestion

`raw CSV → rows → selected numeric columns → integer stream`, max **8 voices**.
That ceiling is a design constraint, not a technical one: it forces a decision
about which eight columns actually matter.

With no `--columns`, selection is automatic, and drops two kinds of column that
look useful and are not:

- **constant** ones — a flat line is not a voice;
- **monotonic** ones — a timestamp has enormous variance and no shape, so it
  quantizes to a rising ramp that says nothing. `--columns timestamp` still
  gets you the ramp if you want it.

Each channel is normalized against **its own** range before quantizing, so a
CPU-percent column and a bytes-sent column both use the full byte range. This
throws absolute magnitude away deliberately.

| flag | meaning |
|---|---|
| `--columns` | names or indices, in order (defines the voices) |
| `--bit-depth` | bits per value before the chain (8 by default, so XOR/Caesar are byte ops) |
| `--granularity` | rows per frame; `--aggregation mean\|max\|min\|sum\|first\|last\|range` |
| `--rate` | frames per second — this is what an LFO's `hz` is measured against |
| `--log-scale` | log-normalize on the way in, for bursty data |
| `--limit` | read at most N rows |

### The pedals

Nine, all from the v0.1 catalog. Each is a pure function
`(stream, params, rng) → stream`. The rule is that **no pedal is smart** — if one
starts needing a lot of logic, it is two pedals.

| pedal | what it does | key params |
|---|---|---|
| `caesar` | cyclic shift by N, N itself can oscillate | `shift`, `shift_lfo` |
| `xor_mask` | XOR against a constant, another column, or an LFSR | `mask_source`, `mask`, `column`, `taps` |
| `delta` | difference from previous — silence = stability, noise = change | `order`, `signed` |
| `mod_reduce` | reduce by modulo, or onto a scale's degrees | `modulus`, `scale`, `octaves` |
| `bit_reverse` | reverse the bits of each value | — |
| `interleave` | alternate blocks of two channels | `a`, `b`, `stride`, `into` |
| `cross_mix` | one column drives the transformation of the others | `driver_column`, `op`, `depth` |
| `bitcrush` | throw away low bits, 8→4→2 | `target_bits`, `bits_lfo` |
| `stutter_repeat` | repeat a block when the data goes flat | `threshold`, `repeats`, `block` |

Every pedal preserves voice count, channel length and bit depth. `stutter_repeat`
is the sole exception on length — making the stream stall is its whole point.

**LFOs.** Any parameter ending `_lfo` takes `shape:freqhz[:depth]`, e.g.
`sine:0.1hz`, `square:0.05hz:0.4`, `sample_hold:0.5hz`. Shapes: `sine`,
`triangle`, `square`, `saw`, `random`, `sample_hold`. Note that a fast LFO on a
slow stream aliases — one cycle per eight frames only ever samples eight phases.

**Scales**, two notations, one internal format:

```bash
python -m serrin scales                                   # the whole bank
python -m serrin scales "1, 1/2, 1, 1, 1/2, 1 1/2, 1/2"   # == harmonic_minor
```

Greek modes, harmonic/melodic minor, pentatonics, blues, altered, bebop
dominant, whole tone, diminished, chromatic. `mod_reduce` maps values onto scale
*degrees* rather than semitones mod 12 — unfiltered chromaticism tires quickly,
so `pentatonic_minor` is the default and full chromaticism is a deliberate
choice.

### Chains and presets

A chain is an ordered list of `(pedal, params)` plus a seed policy:

```json
{
  "name": "gritty_01",
  "seed_mode": "auto",
  "chain": [
    {"pedal": "delta", "params": {"order": 1}},
    {"pedal": "bitcrush", "params": {"target_bits": 4}, "at_intensity": 0.35}
  ],
  "ingest":   {"granularity": 4, "rate": 6.0},
  "envelope": {"kind": "archetype", "archetype": "full_arc"},
  "mapping":  {"quantize_to": "pentatonic_minor"},
  "piece":    {"mode": "closed", "loop": "vary", "voice_entry": "variance"}
}
```

`at_intensity` is the threshold at which a pedal switches on, so one preset can
render as "intro" or as "climax" instead of needing several near-identical files.
Precedence for everything: **CLI flag > preset > built-in default.**

Four shipped presets, spanning the range:

- **`gritty_01`** — the reference chain. Delta, LFSR mask, bitcrush, moving Caesar.
- **`ikeda_sparse`** — restrained. Only acceleration speaks; pentatonic; no dirt.
- **`corrupted_dump`** — everything on, fixed seed, 2-bit crush at the climax.
- **`endless_drift`** — mode B, installation loop, no arc.

### Seeds and reproducibility

Every piece is identifiable as `source + chain + seed` and regenerates
identically. `seed_mode: "auto"` hashes the first 64 rows of the input (including
the header — two files with the same numbers under different column names are
different sources). `seed_mode: "fixed"` pins it.

Each pedal draws from its own substream keyed by `"<index>/<pedal>"`, so
reordering a chain changes the result *deterministically* rather than by
accident, and disabling pedal 3 does not reshuffle what pedal 4 hears.

Randomness is SplitMix64, not `random.Random` — fully specified, so a render is
reproducible across Python versions and machines, not just within one.

### The forked export

Two files, sharing a chain but **not** a mapping — section 3.5's warning is that
audio and visual must not become "the same number disguised twice". The fork is
on three axes:

| | audio | visual |
|---|---|---|
| reads | absolute value (state) | delta + local spread (change, turbulence) |
| channels | voice *n* sings channel *n* | voice *n* is *positioned* by a rotated channel |
| flat data | sustains — becomes a drone | goes still and bands |

There is a test asserting the two stay decorrelated (`r < 0.6`). It caught a real
collapse: with a 3-voice stream the default rotation of 3 wrapped to 0 and the
visual side was reading the channel the audio was singing.

Output is columnar (`voice.freq[i]`, not `frames[i].freq`) — about a third of the
bytes, and it is what the JS reader wants anyway.

---

## The runtime (JS)

No framework, no bundler. ES modules straight from `web/js/`.

| module | job |
|---|---|
| `reader.js` | frame lookup, loop policies, per-pass variation |
| `transport.js` | the single clock; lookahead scheduler + rAF |
| `audio.js` | oscillators, filter, delay, bitcrush |
| `visual.js` | banding, ASCII waterfall, bars, block displacement, scan |
| `envelope.js` | intensity curves, stroke capture, voice gating |
| `panel.js` | the author panel |
| `main.js` | wiring, URL params, keyboard |

### One clock

The frame index is a *pure function of transport time*, derived from the
`AudioContext` clock — there is no shared mutable cursor for the two engines to
get out of step over. The audio scheduler runs ahead of the clock with a 120 ms
lookahead; the visual loop runs at the clock and releases frames as their audio
onsets arrive. A janky animation frame therefore costs a picture, never a beat.

Two questions the design doc left open, answered here:

- **Tick resolution:** data-driven. One row (or aggregated window) is one frame;
  `rate` sets frames per second. A musical grid would impose a metre the data
  does not have.
- **When the stream runs out:** author's choice — `vary` (default), `loop`,
  `pingpong`, `once`. `vary` shifts the read phase and transposes slightly on
  each pass, both derived from the seed, so it repeats without repeating
  identically.

### Modes

- **Closed piece (A)** — the intensity envelope drives which pedals are notionally
  on, how many voices are audible, and how aggressive the sound gets. Voices
  enter one at a time; activation is stretched at the low end so a piece really
  does spend time as a single bleep rather than racing to full density.
- **Endless stream (B)** — no arc. Intensity reacts to the data itself: fast
  attack on a spike, slow release, so tension accumulates without a plan.

### The envelope

Hand-drawn stroke, parametric equation, or named archetype — all three become the
same artifact (a sampled curve plus interpolation), so nothing downstream knows
which it got. Equations: `arc`, `sigmoid`, `plateau`, `ramp`, `pulse`, `flat`.
Archetypes: `full_arc`, `build_up`, `crescendo`, `climax`, `fade_out`,
`dismantling`.

Drawing is Pointer Events with optional stylus pressure, and a live stroke is
captured against the transport clock — so, on the open question in section 8:
**yes, a live stroke is recordable and replays identically.** "Export stroke" in
the panel writes the points out; there is a test asserting the round-trip.

### The panel

Not part of the piece. It starts hidden, opens with <kbd>p</kbd> or `?panel=1`,
and hiding it changes nothing about what is playing. It lives in the same
document as the stage rather than a second window because live envelope drawing
has to be on the engine's clock.

**Keyboard:** <kbd>space</kbd> play/pause · <kbd>p</kbd> panel · <kbd>i</kbd>
invert · <kbd>f</kbd> fullscreen · <kbd>1</kbd>–<kbd>8</kbd> mute a voice.

**URL params:** `?preset=` · `?audio=&visual=` · `?panel=1` · `?autoplay=1` ·
`?speed=`.

---

## Tests

```bash
python tests/run_all.py          # both suites, and they cross-check each other
```

71 Python tests, 17 Node tests. Weighted toward the two properties the aesthetic
depends on: **determinism** (a promise that is not tested is a wish) and
**invariants** (a pedal that breaks one fails hundreds of frames later, in the
browser, which is a miserable way to find out).

The runner hands Python's voice-activation numbers to the Node suite to assert
against, because both sides implement that curve independently and a drift
between them would not fail anywhere — the piece would just be subtly different
in the preview and in the browser.

---

## Layout

```
serrin/           the pipeline: rng, scales, ingest, pedals, chain, envelope, export, cli
presets/          chain definitions
scripts/          sample data generator, dev server
web/              the runtime (index.html, style.css, js/)
tests/            run_all.py, test_pipeline.py, test_runtime.mjs
data/  out/       inputs and renders (out/ is gitignored)
```

---

## Known gaps

Honest accounting of what is specified but not yet real:

- **Live pedal reorder/toggle** (§4.5). The chain is rendered offline in phase 1,
  so the panel's pedal list is read-only and shows which pedals the current
  intensity has *notionally* switched on. Real live manipulation needs the chain
  ported to JS — roadmap step 5.
- **Preset switching** re-fetches a different rendered pair rather than re-running
  the chain, for the same reason.
- **Audio bitcrush** is a WaveShaper quantizing curve, which gets the stepped,
  aliased character but not true sample-rate reduction. That needs an
  AudioWorklet.
- **WebGL** is untouched — Canvas2D is holding up fine at eight voices and a
  ~240-column waterfall, as §4.1 predicted it might.
- **Alternative data sources** (commit graph, tides, UVB-76) — the ingestion layer
  is where they plug in, but only CSV exists so far.
