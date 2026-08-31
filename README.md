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

**The app opens in the studio.** You pick or configure a piece, press render,
then press play — and that last press is also the user gesture browsers require
before audio, which is why there is no "begin" ceremony.

Two views, one at a time:

- **studio** — where you start. Configuring a piece; nothing sounds.
- **stage** — the piece playing, with the panel (<kbd>p</kbd>) for
  performance-time controls.

<kbd>F3</kbd> moves between them. Going back to the studio pauses: design time
is quiet, performance time sounds.

`?preset=gritty_01` or `?play=01-decay` skip the studio and go straight to the
stage, for a demo or a link.

Opening `web/index.html` directly from disk will *not* work — ES modules and
`fetch` both refuse `file://`. Hence the tiny server.

---

### Running more than one at a time

Each browser tab is an independent instance: its own `AudioContext`, its own
transport, its own clock. Nothing is exclusive at the audio-device level — the OS
mixes them — so several pieces can play at once and you do not need a second
server to do it. Just open more tabs.

Separate ports are only useful for serving a *different working copy*:

```bash
python scripts/serve.py --port 8000
python scripts/serve.py --port 8010 --no-open
```

Two things to know about long runs:

- A hidden tab keeps making sound but stops drawing — browsers suspend
  `requestAnimationFrame` while leaving the audio clock running. The transport
  caps its pending-visual queue and drops the oldest frames, so a piece left
  behind another window does not accumulate a backlog.
- The dev server is threaded. It began as a single-threaded one, which stalls
  every other request while a browser holds a connection open.

## The pipeline (Python)

One entry point, five subcommands:

```bash
python -m serrin new      my-album/01-decay -i data.csv           # start a piece
python -m serrin render   --piece my-album/01-decay              # render it
python -m serrin pieces   my-album                               # the album
python -m serrin render   -i data.csv -c presets/gritty_01.json   # or one-off
python -m serrin render   --repo . -c presets/merkle_drift.json    # a commit graph instead
python -m serrin graph    --repo . -o history.json                # or its history as a file
python -m serrin render   --session out/my.session.json           # re-render what you saved
python -m serrin session  out/my.session.json --to-preset p.json  # freeze it as a preset
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
| `--tempo` | the grid, musically: `120`, `96/8` (BPM/subdivision), `128/16+0.3` (with swing) |
| `--subdivision` | note value of one frame: 4=quarter, 8=eighth, 16=sixteenth |
| `--swing` | 0 straight, 1 triplet feel |
| `--rate` | frames per second, if you would rather not think in BPM |
| `--log-scale` | log-normalize on the way in, for bursty data |
| `--limit` | read at most N rows |

### A repository as the source

Section 6.3's parked idea, now real. It is an *ingestion adapter* and nothing
more: it produces the same `Stream` a CSV does, so pedals, forked export and the
whole runtime are untouched.

```bash
python -m serrin inspect --repo .                        # look at the graph first
python -m serrin render  --repo . -c presets/merkle_drift.json
python -m serrin render  --repo . --metric interval --traversal topo
```

**Branches are voices.** Each branch takes a new value at each of *its own*
commits and holds in between — so a branch with three commits becomes a voice
that speaks three times and is still the rest of the time. `delta` reads a held
value as zero, so silence falls out of the data rather than being imposed on it.
Density becomes a property of the repo's real activity, exactly as the doc
predicted.

**Which commits are whose** is a question git cannot answer directly — a commit
belongs to every branch that can reach it. So a branch's *exclusive* commits
(`rev-list ref --not <every other ref>`) are unambiguously its own, and the
shared trunk goes to the trunk branch (`main`/`master`/`trunk`/`develop` if
present, otherwise whichever reaches the most). A fully merged branch owns
nothing and is dropped — and *named*, because "my branch is missing" is otherwise
a mystery.

**Metrics** — what a commit contributes:

| metric | reads |
|---|---|
| `hash` | bytes of the commit id — real noise, needs no pedals to be chaotic |
| `interval` | seconds since that branch's last commit — the repo's rhythm |
| `churn` / `insertions` / `deletions` / `files` | size of the change |
| `parents` | merges spike, and a merge *is* `interleave` happening in the data |
| `hour` | hour of day — diurnal periodicity |
| `author` | hashed identity — the voice changes when the person does |

`hash` is not renormalized (already uniform), `parents` and `hour` use fixed
scales (0–8 parents, 0–23 hours) so they mean the same thing in every repo, and
the heavy-tailed ones are log-scaled by default.

**Traversal** — the question section 8 left open. The default is **chronological**,
because the piece exists in time and a repository's rhythm (bursts, nights, dead
weekends) is the most musical property it has. `--traversal topo` throws the real
timestamps away for an even cadence: correct as a graph traversal, inert as
music. `reverse` runs history backwards.

`presets/merkle_drift.json` is built for this and is deliberately gentle on the
pedals: hash bytes have no structure left for `xor_mask` or `bitcrush` to break,
so those would only relabel one kind of randomness as another. It uses `delta`
to read the blips, `mod_reduce` to pull them onto a scale, and `cross_mix` so the
trunk perturbs the branches the way a merge perturbs a repo. Graphs are finite,
so it is a closed piece with `loop: once` — the doc's own conclusion.

### A repository's history as a file

The live adapter needs the clone on this machine and a working `git`. Fine for
your own work, useless for three things people want: rendering from a repo you
have not cloned, sharing a piece whose source travels with it, and putting a
graph piece in an album someone else can open.

So a history can be exported and ingested, and **both produce the same stream** —
guaranteed by going through the same code, not by carefulness. There is a test
asserting identical fingerprints across every metric.

```bash
python -m serrin graph --repo . -o history.json      # export
python -m serrin graph --check history.json          # inspect and validate
python -m serrin render --graph history.json -c presets/merkle_drift.json
```

In the studio, `kind: graph` takes a `.json` you choose, or **export from a
repository…** reads a clone once and leaves a portable copy in the piece folder.

**Two things are baked in at export time**, because the file cannot recompute
them:

- *Ownership.* Deciding which branch owns a commit needs
  `rev-list ref --not <others>` against a real repository; a flat commit list
  would mean reimplementing reachability, badly.
- *The seed.* A repository seeds from HEAD's own first commits in git's order,
  and a timeline merged across branches is a different list. Without recording
  it, a piece would change seed the moment its source switched from the clone to
  the export.

A hand-written history still loads. Without `owner` it collapses to one voice and
`validate` says so, because a file that quietly becomes one voice looks like a
bug in the pipeline rather than a gap in the file. Same for missing per-commit
stats: the churn, insertions, deletions and files metrics would read as flat.
Missing timestamps are fatal — the chronological order and the `interval` metric
both need them. And topological order is *refused* rather than faked: it needs
the DAG walked, and a flat list cannot.

### Reading a file that was not written for you

A real export does not begin with a header row. PVGIS writes eight lines of
tab-separated metadata and then `time,G(i),H_sun,T2m,WS10m,Int`, with prose at
the end. Solargis writes forty-one `#` comment lines and then a semicolon table.
Taking line one as the header turned the first into a two-column file called
*Latitude (decimal degrees)* and the second into one column of English, and both
then failed for a reason that pointed nowhere near the cause.

So `find_table` looks for the table instead of assuming where it is: **the header
is the line that starts the longest run of rows that all split into the same
number of fields.** That definition is what makes it robust — a preamble line
splits into one or two fields and a footer line into one, so neither can outvote
a thousand rows of six. Ties go to the wider table, because a long preamble of
tab-separated key/value pairs is otherwise a strong candidate.

This is parsing, not cleaning. Serrin still does not repair values — a constant
column stays constant and gets dropped, a bad number stays bad. But a metadata
preamble is part of the file *format*, and refusing to find the table is failing
to read the file rather than declining to fix it.

**And it says what it decided**, everywhere the decision is visible: in the
studio, in `/api/source`'s problem list, and in `inspect`. Skipping forty-one
lines is a judgement, and a judgement made silently is indistinguishable from a
bug — the report is what lets you see at a glance that the guess was right.

```
$ python -m serrin inspect -i "Solargis_Prospect_Medina del Campo.csv"
read as 13 columns x 13 rows, delimiter ';', header on line 42
  skipped 41 line(s) above it (metadata preamble)
  columns: Month, GHIm, Diffm, DNIm, T24, ALBm, WSm, RHm, PWATm, PRECm, ...
```

Rows that do not match the header's width are dropped rather than padded: a
short row padded with blanks becomes held-forward values that look like real,
very flat data. A file with a header and nothing under it is refused with the
header, the column count and the delimiter in the message, because that is the
one case where the guess probably *was* wrong.

The other half of reading these files was the comma. `_parse_number` used to
strip every comma as a thousands separator, which turns the European `12,5` into
`125` — a tenfold error, silent, in a column that still looks perfectly
plausible afterwards. The two conventions are now told apart by shape: commas
every three digits group, a single comma between digits is a decimal point.

### Whose fault is it

Two failures reported as problems with a CSV turned out to be Serrin's own, and
the misattribution cost more than the bugs did.

`_columnPicker` went missing: a refactor deleted the body and left the call site,
so `_sourceReport` threw for every CSV. Because the throw happened inside the
upload's `try`, it surfaced as **`not accepted — this._columnPicker is not a
function`** — on a file that had been read perfectly, 140,256 rows of it. That
sentence sends the next hour into the data.

Three things changed, and only one of them is the missing method:

- **The upload's error handling stops at the upload.** A failure from the request
  is the file's fault and says `not accepted`. A failure *after* the file is on
  disk and parsed is Serrin failing to draw it, and says so — the copy still
  happened, and the message says that too.
- **Each report section renders inside a guard.** One section throwing used to
  take the panel; now it prints which section broke and that it is a bug in
  Serrin, not a problem with your file, and the rest of the report survives.
- **A test asserts every method `_sourceReport` calls exists.** The specific bug
  is covered, but the general shape — a call site outliving its method — is what
  actually happened, twice. Reintroducing the deletion fails six tests.

The upload validation also grew the half its docstring already claimed. It said
files were checked before being written; that was only true of histories, so an
unparseable CSV was written first and complained about afterwards. Note what is
*not* checked: a preamble, a semicolon, a footer, a decimal comma are all read
fine and none of the uploader's business. The bar is "can the table be found at
all" — anything stricter would reject files that work.

### Tempo

The frame grid has a musical name. This does **not** change the tick model —
one row is still one frame, decided by the data — it only names the spacing,
which is the one thing a CSV row has no opinion about.

The two framings were never opposed: 8 frames per second *is* sixteenth notes at
120 BPM. That was always serrin's default; it just had no name.

```bash
python -m serrin render -i data.csv --tempo 96/8        # 96 BPM in eighths
python -m serrin render -i data.csv --tempo 128/16+0.3  # with swing
python -m serrin render -i data.csv --rate 6            # still fine; infers 90 BPM
```

Naming it buys three things:

- **LFOs in beats and bars.** `sine:4beats`, `saw:1bar`, `triangle:1/2beat`
  stay locked to the grid at any tempo; `sine:0.1hz` still means hertz and
  drifts across it. Both are useful — the first is a synced tremolo, the second
  is a slow sweep that does not care what the music is doing.
- **A delay in note values.** Dotted eighth stays a dotted eighth when the BPM
  slider moves, instead of being "about three frames".
- **Swing.** Offbeat frames are pushed later; `swing: 1` is a triplet feel
  (the pair splits 2:1). Applied in exactly one place —
  `Reader.frameOnset` — so the feel lands on the *picture* as well as the
  sound, with no second implementation to keep in step.

Positions are reported as `bar.beat.step`, and the panel edits BPM,
subdivision, swing, metre and delay time live. Changing the grid mid-playback
re-anchors the transport so the current frame stays where it is.

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

**LFSR taps.** `xor_mask` writes taps as polynomial exponents with the `+ 1`
implicit, so `[3, 1]` means `x^8 + x^3 + x + 1` and normalizes to `[3, 1, 0]`.
Bit 0 is always included, which is what makes the state map invertible and the
register mathematically unable to reach the all-zero dead state. Omit `taps`
entirely to get a *primitive* polynomial — a maximal-length run of 2ⁿ−1 before
it repeats. Non-primitive taps are shorter and buzzier on purpose; `render`
reports the period it actually got, because the period is audible.

**LFOs.** Any parameter ending `_lfo` takes `shape:period[:depth]`. The period
carries a unit — `0.1hz` (absolute), `4beats` or `1bar` (locked to the grid);
fractions like `1/2beat` are allowed. Shapes: `sine`, `triangle`, `square`,
`saw`, `random`, `sample_hold`. A fast LFO on a slow stream aliases — one cycle
per eight frames only ever samples eight phases.

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

### The key map editor

In the studio. A picture of a keyboard, because a map *is* a layout and a table
of `KeyA → degree 0` rows does not read as one. Click a position, bind it below;
buttons fill the two home rows, every key, or clear it.

**Positions, not characters.** A binding is stored against `KeyboardEvent.code`
— `KeyA`, not `a`. `event.key` depends on the layout, so a map authored on a
Spanish keyboard would land on different physical keys on a US one, and a piece
is meant to be shareable.

That leaves the opposite problem — the editor has to show *you* your own keys —
which `navigator.keyboard.getLayoutMap()` answers where it exists, with a US
table as the fallback. So the labels are yours and the storage is portable.

**Degrees, not pitches.** A binding is a scale degree by default, so the map
stays in key when the chain or the mapping moves the piece into another scale.
Absolute MIDI notes are available and say so in the editor: they will not follow.

Four binding kinds: `degree`, `note`, `sample`, `pattern`. The last two are
stored and validated but not playable yet — and they resolve to something that
is *not* a note rather than quietly falling back to a pitch, so a half-built map
does not sound finished. A binding pointing at a sample the piece does not have
is refused when the manifest is saved.

The browser's default map and the CLI's `default_keymap()` are cross-checked by a
test, since both exist and a divergence would mean "the same piece" plays two
different layouts.

### Two views, and one place that decides

The app grew four independently hidden elements — gate, studio, panel, console —
each toggled by whoever needed it. Nothing stopped them contradicting each other:
the studio over a playing stage, the panel over the studio, an error banner
behind a view that had already loaded. Every one of those was reachable, none was
intended, and none could be tested without a browser.

`views.js` holds one piece of state and derives visibility from it. `snapshot()`
is a pure function from state to what should be on screen; `main.js` applies it in
one place and nothing else touches `hidden`. The illegal combinations are now
unrepresentable rather than merely avoided, and the whole thing runs in node —
there is a test that walks the entire state space and asserts every reachable
combination is one somebody meant.

The rules it encodes: exactly one view at a time; the panel is a stage overlay
and refuses to open anywhere else; the console shows over either; the stage has
no cursor unless something is layered over it; an error is state, so a view
change cannot quietly navigate away from it.

### The studio

<kbd>F3</kbd>, or `?view=studio`. A full-viewport view for configuring a piece
*before* generating it.

The panel and the studio deliberately do not look alike. The panel is
**performance time** — things you touch while a piece plays, reachable without
covering the stage. The studio is **design time** — the source, the chain, the
mapping, the grid. They share the state, not the layout; merging them would
produce one surface that is bad at both.

What it edits:

- **source** — one of three kinds, with a file dialog and a problem list:
  - `csv` — **choose a CSV…** copies it into the piece folder, because a browser
    hands over contents and not a path, and because data that travels with the
    piece is the better default. A path field stays for a file too large to want
    two copies of. The column picker shows *why* each column would be dropped:
    constant, monotonic, or unparseable — and a line above it says how the file
    was read: delimiter, header line, columns, rows, and anything skipped. An
    upload is parsed *before* it is written, so a piece never ends up pointing at
    a file nobody can read.
  - `git` — a clone on this machine, read on every render.
  - `graph` — an exported history, which travels with the piece.

  Serrin does not clean data, so whatever is wrong with a source is named here
  and has to be fixed upstream. The endpoint reads the path *in the form*, not
  the one saved on disk — a working copy is unsaved by definition, and the old
  version answered about the saved path, which is why typing a new one never
  refreshed anything.
- **tempo** — bpm, subdivision, swing, metre, with the resulting frame rate shown.
- **pedal chain** — add, remove, reorder by dragging the cards (the arrows stay:
  they work from the keyboard and are unambiguous about one step), edit every
  parameter, set the intensity each pedal switches on at. Reordering is a real
  edit of the sound, not a tidy — each pedal draws its randomness from its
  position, so moving one changes every pedal after it, deterministically.
- **mapping** — the note range, frequency curve, gate threshold, glitch
  threshold, channel rotation. The subjective layer §5 leaves open on purpose.
- **envelope** — archetype, equation or a hand-drawn curve.
- **piece** — title, notes, mode, loop policy, voice entry, seed override.
- **performance** — keyboard settings now; the key map editor, sample list and
  beat grid land here next.

**The chain editor works, and that is not a contradiction.** Live pedal
reorder/toggle is still pending because it needs the chain ported to JavaScript.
Editing a chain here and *re-rendering through Python* needs no port at all — the
browser is a front end for the pipeline, and the pedals stay in one place.

The studio holds a **working copy** of the manifest, so edits are local until
saved and it can say "unsaved" honestly. Rendering reads the file from disk, so a
dirty piece is saved first — announced rather than silent, because "render"
quietly writing your edits would be a surprise.

Every list in the UI — pedals, their parameters, scales, archetypes, git metrics
— comes from `GET /api/catalog`. Hardcoding them in JavaScript would mean a pedal
added in Python is silently unreachable from the studio.

### The console

<kbd>F2</kbd> opens a devtools drawer. The panel answers *what should this do*;
the console answers *what did it just do* — different questions, so they get
different surfaces.

| tab | shows |
|---|---|
| **log** | what happened, in order — including `console.warn`/`error` the page used to swallow |
| **pipeline** | the trace: what each stage did to the numbers |
| **meta** | everything the render says about itself |
| **frame** | the values going past right now, per voice, both forks |
| **audio** | the node graph, and the calls actually being scheduled |

<kbd>F2</kbd> rather than a letter because the keyboard claims every
single-character key when it is armed.

#### The trace

Every stage of a render used to destroy its input. A CSV cell became a float
became a byte became nine transformed bytes became a frequency, and only the last
survived — fine for playing a piece, useless for understanding one. When a chain
sounds wrong the question is always *which stage did that*, and there was no way
to ask.

```bash
python -m serrin render -i data.csv --trace out/trace.json   # write one
python -m serrin inspect -i data.csv --trace                 # or just look
```

Tracing is opt-in per render (`trace: true` on the endpoint, a checkbox in the
**source** section) because it roughly doubles the work and the reply size.

What each stage carries:

- **ingest** — the conversion nobody could see: raw cell text → parsed number →
  aggregated → byte, plus the range each column was normalized against, which is
  the lossy step where magnitude is discarded and only shape survives. Also which
  columns were dropped and why. For a git source, branch ownership instead.
- **pedal**, one per applied pedal — the values after it, what fraction of them
  moved, and the change in entropy.
- **mapping** — worked examples of the fork: `byte 197 → 196.0 Hz / y 0.84 /
  glyph '!'`. Statistics would not show the interesting claim, which is that one
  value becomes a frequency *and*, via a rotated channel, a position.

**Entropy is the headline number**, because it is what this project is actually
about. Read down a chain it tells the story directly:

```
[0] ingest   read monitoring.csv
[1] pedal    delta      87% moved, H -1.89   <- strips absolute level
[2] pedal    xor_mask  100% moved, H +3.18   <- shreds what structure was left
[3] pedal    bitcrush   97% moved, H -3.29   <- collapses to 16 values
[4] pedal    caesar      88% moved, H +2.79   <- the moving shift re-spreads it
[5] mapping  forked export
```

Values shown are a window (first 96 frames by default); statistics are over the
whole stream. The format says which is which, because a trace quietly reporting
window statistics would look authoritative and be wrong.

#### Inspecting the sound

The **audio** tab shows the live node graph and a journal of what was scheduled —
frequency, amplitude, and whether each frame retriggered or *slid* (the decision
that turns held data into a drone). **copy** emits runnable JavaScript.

One caveat stated plainly, in the tab as well as here: **serrin does not generate
JavaScript.** The sound comes from a fixed graph driven by data, so there is no
generated source to dump. What **copy** produces is a *reconstruction* — a
self-contained snippet that rebuilds the effect chain and replays the events
actually scheduled, with times made relative so it runs immediately. A test parses
it with `vm.Script` to make sure it is real code rather than something that looks
like it.

### Recording a take

**record** in the panel captures what you actually hear — keyboard playing and a
live-drawn envelope included — in real time, to WebM/Opus. Tick *with video* and
the canvas comes along in the same file, tracks in sync.

It is deliberately not a lossless master. The other option,
`OfflineAudioContext`, renders faster than real time to uncompressed samples, but
it renders a *graph* rather than a performance: it cannot capture live playing,
because there is no live. Since the keyboard and the live stroke are what make
serrin a performance tool rather than a batch renderer, capturing the performance
is the more honest default. The offline master is the upgrade path.

### Rendering something new from the browser

The **source** section takes a CSV you upload or a path to a git repository, and
hands it to the pipeline. Needs `scripts/serve.py` running, because the pedals
live in Python.

That is the honest middle option. Porting the chain to JavaScript (roadmap step
5) would duplicate nine pedals, ingestion and the export mapping, and create a
*second source of truth for the aesthetic* — two implementations that drift, with
the sound as the thing that drifts. Posting the file to the one real pipeline
keeps a single implementation and costs one endpoint.

`POST /api/render` takes JSON: `{csv, name}` or `{repo}`, plus optional `preset`,
`preset_json`, `tempo`, `columns`, `seed`, `metric`, `traversal`. It writes the
pair *and a session* under `out/uploads/`, and returns the URLs — so the page
loads the result exactly like a preset, with no second code path in the runtime.

**The server binds to localhost.** It writes files it is given and runs renders
on request; that is fine on your own machine and not fine on a shared network.
`--host 0.0.0.0` exists and says what it is doing when you use it.

## Pieces

The thing you work on is a **piece**, and a piece is a folder.

Until now the central object was a *render*: the pipeline produced two JSON files
and the browser played them, and a session was a note taken afterwards about what
you had found. That is the wrong way round for making an album, so the flow is
inverted — the piece holds everything needed to generate itself, and the render
becomes an output of it, like a mixdown.

```
my-album/
  01-decay/
    piece.json      the manifest
    data.csv        or a path to somewhere else
    samples/        audio the performance layer triggers
    out/            renders, disposable
  02-static/
  kit/              samples shared across the series
```

```bash
python -m serrin new my-album/01-decay -i data.csv --title Decay --tempo 96/16+0.2
python -m serrin render --piece my-album/01-decay   # into its own out/
python -m serrin piece  my-album/01-decay --keymap   # inspect one
python -m serrin pieces my-album                     # the album view
python -m serrin piece  my-album/03-old --from-session out/old.session.json
```

**A folder rather than a file**, for two practical reasons. Samples cannot live
in the JSON — base64 audio bloats the file and destroys the diff, which is
exactly what breaks the version control you are meant to be doing yourself. And
relative paths make the folder portable: copy it, zip it, move it into an album,
and it still resolves. A piece can point at `../kit/kick.wav` to share a sample
with the rest of a series without duplicating it.

### The four blocks

| block | holds | render input? |
|---|---|---|
| `source` | what to ingest and how | yes |
| `preset` | the pedal chain and mapping — the schema the CLI already speaks | yes |
| `performance` | keymap, samples, patterns, keyboard settings | **no** |
| `runtime` | levels, mutes, visual toggles | no |

The first two decide what the pipeline produces; the last two decide what happens
on top of it. That boundary is what stops "open a piece" from implying "and
re-render it".

`performance` is where §4.3 is honoured rather than broken. **Samples live there
and only there** — the eight data voices stay oscillators, because the generated
sound is meant to be primitive. What *you* play over the top is interpretation
rather than translation, and that is a different rule.

`render` is a fifth, optional block: present once the piece has been produced at
least once. Its absence is normal — a piece exists before it has been rendered,
which is the entire point of the inversion.

### Keys are bound by position, not by character

A keymap stores `KeyA`, not `a`. `event.key` depends on the layout, so a map
authored on a Spanish keyboard would land on different physical keys on a US one
— and a piece is meant to be shareable. Position is also the right model
musically: a mapping is a *layout*, like a piano.

Bindings are scale **degrees** by default rather than fixed MIDI notes, so a map
stays in key when the chain or the mapping changes the piece's scale. Absolute
pitches would go quietly out of tune.

### A session is a piece that has been rendered

The old session format loads without translation — one reader, two shapes — so
anything you saved before still opens, and `piece --from-session` imports it into
a folder.

### Serving an album

```bash
python scripts/serve.py --pieces ~/music/my-album
```

The endpoints the studio view is built on: `GET /api/pieces`, `GET /api/piece`,
`POST /api/piece`, `POST /api/piece/new`, and `POST /api/render` with
`{"piece": "01-decay"}`. Rendered files are served from `/pieces/…`, so an album
can live anywhere on disk rather than only inside the repo.

**Writes are confined to the pieces root.** The resolved path is checked, so `..`
and absolute paths both fail — an endpoint that writes JSON to a path supplied
over HTTP wants a boundary, and "the folder you pointed me at" is one you already
understand.

### Sessions: keeping what you found

The panel used to be a place to discover settings you could not keep. **save
session** writes everything you have tuned to a file that both sides understand.

The format has one seam, and it is the whole design:

| block | what it holds | applying it |
|---|---|---|
| `source` | what was ingested and how | needs a re-render |
| `preset` | chain, seed policy, mapping, envelope - *exactly* the preset schema | needs a re-render |
| `runtime` | levels, cutoff, mutes, visual toggles, keyboard, speed, drawn envelope | takes effect at once |

Loading a session in the browser restores what you were **hearing**;
re-rendering from it restores what the **pipeline** would produce. When the
loaded streams come from a different render, the panel says so and names the
command that would fix it, rather than pretending a chain edit took hold.

```bash
python -m serrin render --session out/my.session.json      # reproduces it exactly
python -m serrin session out/my.session.json               # look at one
python -m serrin session out/my.session.json --to-preset presets/mine.json
```

**freeze as preset** is the useful direction of travel: tune by ear, then lock
the result as a CLI preset - section 3.4's "freeze the chains that work". A
preset is the half of a session with an offline meaning, so levels, mutes,
visuals and keyboard are *not* in it. Both the panel and the CLI say so out loud.

**download streams** gives you the rendered JSON pair. Pair plus session is a
complete, portable, replayable piece. Audio you can send to someone who does not
have serrin is a separate feature, still to come.

Sessions also autosave into the browser every 20 seconds (per-origin
`localStorage`), so a reload does not cost the settings.

### The keyboard

Playing along with the piece, live. Off by default — a piece must not change
behaviour because someone leaned on the space bar.

Turn on **use keyboard** in the panel, and letter/number keys play notes. The
piece keeps only <kbd>space</kbd> (play/pause) and <kbd>esc</kbd> (switch the
keyboard off); modifier combinations pass through to the browser.

Modes arrive in stages. Only the first exists:

| mode | what it does |
|---|---|
| `random` | a fresh note every press — scattershot — **implemented** |
| `fixed` | every key keeps one note, chosen for you — **implemented** |
| `notes` | the piece's own key map, for playing melodies — **implemented** |
| `samples` | key → sample — pending |
| `beats` | step sequencing and live recording over the stream — pending |

The pending modes are listed in the dropdown and disabled, so the shape is
visible and nothing pretends to work.

**`notes` claims only the keys the map binds.** An unbound position falls through
to the piece's own shortcuts, so a nine-key map does not make thirty other keys
dead. `random` has no map and claims everything.

<kbd>↑</kbd>/<kbd>↓</kbd> shift the whole map by octaves while playing — the
arrows are never claimed, which makes them the natural place for it.

**`fixed` is `random` keyed differently.** `random` draws from the press number,
so a key gives a new note every time; `fixed` draws from the *key position*, so
one key is always the same note. That makes it playable — you can learn it, find
intervals, come back to a phrase — without anyone having authored a map. Still
seed-derived, so a different piece scatters differently while any one piece keeps
its layout across reloads.

**Holding a key holds the note.** The envelope decays to a sustain level and
stays there until you let go. Sustain 0 is the original percussive bleep, still
reachable from the panel, because a bleep is a choice rather than a limitation.
Losing window focus releases everything held — a key-up landing in another window
would otherwise leave a drone nothing on the page can reach.

**The four timbres are gain-matched.** Sine was reported as "not sounding", and
it was not broken: it puts all its energy in the fundamental, while a square or a
sawtooth spreads it across a harmonic series that reads as far louder — which
under eight sawtooth data voices amounts to inaudible. Each waveform now carries
a compensating gain, normalized down from sine so there is no headroom problem.

**Which notes.** The pipeline now exports the key the piece is in
(`meta.scale`), so the keyboard plays *inside* it rather than over the top of
it. A piece can pick up a scale in two unrelated places — `mod_reduce` in the
chain, or `MappingConfig.quantize_to` on the way out — and the output mapping
wins when both are set, because it is what the exported frequencies actually
obey. A piece that quantizes nothing (`corrupted_dump`) reports the default and
says so, rather than silently pretending it declared a key.

**Register** picks where the notes sit — bass, mid, treble, the whole piano, or
the piece's own working range — always clamped to A0–C8.

**Routing.** Played notes go *after* the bitcrusher by default, which is the one
place serrin deliberately breaks its own "everything gets the same dirt" rule:
the intensity envelope drops the crusher to three bits at the climax, exactly
when a melody most needs to be audible. They still get the filter and the
tempo-synced delay, so they stay in the same room. There is a checkbox to crush
them anyway.

**Reproducibility.** The random draw is seed-derived, so the *n*th press of a
given piece is always the same note. The performance is not reproducible; the
instrument is. That is what will let a recorded key sequence replay exactly when
the beats mode arrives.

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
| `tempo.js` | the grid: BPM, subdivision, swing, note values |
| `reader.js` | frame lookup, loop policies, per-pass variation, onsets |
| `transport.js` | the single clock; lookahead scheduler + rAF |
| `audio.js` | oscillators, filter, delay, bitcrush |
| `visual.js` | banding, ASCII waterfall, bars, block displacement, scan |
| `envelope.js` | intensity curves, stroke capture, voice gating |
| `keyboard.js` | live playing: modes, note pool, which keys it claims |
| `session.js` | capture/restore, downloads, autosave |
| `recorder.js` | MediaRecorder takes, audio or audio+video |
| `source.js` | uploads and repo paths, posted to the render endpoint |
| `console.js` | the devtools drawer, and the JS reconstruction |
| `studio.js` | the design-time view: piece config, chain editor, key map |
| `keymap.js` | the physical layout, and what each position plays |
| `views.js` | which surface is showing, and which combinations are legal |
| `panel.js` | the author panel |
| `main.js` | wiring, URL params, keyboard |

### One clock

The frame index is a *pure function of transport time*, derived from the
`AudioContext` clock — there is no shared mutable cursor for the two engines to
get out of step over. The audio scheduler runs ahead of the clock with a 120 ms
lookahead; the visual loop runs at the clock and releases frames as their audio
onsets arrive. A janky animation frame therefore costs a picture, never a beat.

Two questions the design doc left open, answered here:

- **Tick resolution:** data-driven. One row (or aggregated window) is one frame.
  The *spacing* of those frames is a tempo (see above) — naming the grid does
  not sequence anything, and the data still decides what happens on each step.
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

Archetypes are **editable starting points, kept alongside the free curve** —
not a replacement for it and not a candidate for removal. Both resolve to the
same artifact, so keeping them costs nothing and they answer the common case.
(That settles the last open question from §5.1.1 of the design document.)

Drawing is Pointer Events with optional stylus pressure, and a live stroke is
captured against the transport clock — so, on the open question in section 8:
**yes, a live stroke is recordable and replays identically.** "Export stroke" in
the panel writes the points out; there is a test asserting the round-trip.

### The panel

Not part of the piece. It starts hidden, opens with <kbd>p</kbd> or `?panel=1`,
and hiding it changes nothing about what is playing. It lives in the same
document as the stage rather than a second window because live envelope drawing
has to be on the engine's clock.

**Keyboard:** <kbd>space</kbd> play/pause · <kbd>p</kbd> panel · <kbd>F2</kbd>
console · <kbd>F3</kbd> studio · <kbd>f</kbd> fullscreen · <kbd>1</kbd>–<kbd>8</kbd>
mute a voice · <kbd>↑</kbd>/<kbd>↓</kbd> shift octave while playing.

With the keyboard armed, letters and digits play notes instead.
<kbd>space</kbd>, <kbd>esc</kbd> and <kbd>p</kbd> stay reserved — the transport
and the ways back out, so arming the keyboard cannot lock you out of the panel.

Inverting to a light palette is a **panel checkbox, not a shortcut**. It used to
be <kbd>i</kbd>, which repainted the whole piece from a bare letter key —
undocumented, drastic, and only when the keyboard happened to be disarmed, so one
key did two unrelated things depending on hidden state.

**URL params:** `?preset=` · `?audio=&visual=` · `?panel=1` · `?autoplay=1` ·
`?speed=`.

---

## Tests

```bash
python tests/run_all.py          # both suites, and they cross-check each other
```

367 Python tests, 195 Node tests, plus a cross-language round trip. Weighted toward the two properties the aesthetic
depends on: **determinism** (a promise that is not tested is a wish) and
**invariants** (a pedal that breaks one fails hundreds of frames later, in the
browser, which is a miserable way to find out).

`/api/catalog` reports which endpoints the running server serves, and the page
checks that list against what it needs. Without it, a `serve.py` left running
from before an endpoint existed answers `404`, the browser logs one line, and the
UI shows nothing at all — an afternoon of debugging a page that was fine. Now it
says *the server is older than this page*, and names the fix.

That list is not written twice. `GET_ROUTES` and `POST_ROUTES` are declared once
and three things read them: the dispatcher, the `Allow` header, and the catalog.
A hand-maintained copy of a route list is a lie waiting for a release — it would
report a healthy server while the page still 404s — so the tests assert that
nothing is declared without a handler or handled without being declared, which is
the failure that actually happens, during a rename.

The same table fixes a **404 that was lying**. A `GET` to a POST-only route used
to answer `no such endpoint`, which is false about a route that exists and points
debugging in exactly the wrong direction. It now answers `405` with `Allow`:

```
$ curl -i -X GET localhost:8000/api/render
HTTP/1.1 405 Method Not Allowed
Allow: POST
{"error": "/api/render does not answer GET; it answers POST", "allow": ["POST"]}
```

`HEAD` on an API route used to fall through to the static handler and answer
`text/html` about a file called `api/source`. It now answers what a `GET` would,
without a body.

The same reasoning covers three silent failures reported together as "nothing
happened": choosing a file with no piece open, exporting a history with no piece
open, and selecting two files where a piece takes one. All three now say what
they did or would not do.

And `POST /api/piece/new` **requires a name.** It used to default to `untitled`,
so a request with an empty body created a piece — an endpoint that writes to the
disk without being told what to call it is a sharp edge, because the accident is
silent, it looks like a real piece in the album, and nobody asked for it. A name
that slugs away to nothing (`???`) is refused for the same reason: quietly
renaming it to `piece` is the same silent judgement in a smaller coat. Names that
merely need cleaning are still cleaned and the result is shown — `Datos meteo
UC50` becomes `Datos-meteo-UC50`, and `../escape` becomes `escape`, which is
also why a name cannot walk out of the pieces root.

**save vs render.** They were reported as indistinguishable, and fairly: render
already saves a dirty piece before running, so save is a strict subset of it.
Rather than explain that in a tooltip nobody opens, the render button says which
of the two things it is about to do — `save + render` when there are unsaved
edits, `render` when there are not. Save greying out when there is nothing to
save then tells the rest of the story by itself.

`test_ui_boot.mjs` constructs the panel, studio and console against a minimal
DOM stub and runs their first paint. It exists because a crash on load once
shipped with every other test green: the state machine was covered thoroughly
and the code that renders it was not covered at all, leaving the worst failure
mode — a blank page — undetectable. It checks that the code runs, not that
anything looks right; a real headless DOM to assert on a canvas painter would
still cost more than it catches.

The runner hands Python's numbers to the Node suite to assert against — the
voice-activation curve and the whole tempo grid, swung onsets included. Both
sides implement those independently, and a drift between them would not fail
anywhere: the browser would just play a slightly different piece from the one
the pipeline rendered.

---

## Layout

```
serrin/           the pipeline: rng, scales, tempo, ingest, ingest_git, graph,
                  pedals, chain, envelope, export, trace, session, piece, cli
presets/          chain definitions
scripts/          sample data generator, dev server
web/              the runtime (index.html, style.css, js/)
tests/            run_all.py + test_pipeline, test_tempo_lfsr,
                  test_scale_export, test_session (python) and
                  test_runtime, test_keyboard, test_session (node),
                  session_fixture.mjs
data/  out/       inputs and renders (out/ is gitignored)
```

---

## Known gaps

Honest accounting of what is specified but not yet real:

- **`samples` and `beats`** — the two remaining keyboard modes. Both are stored
  and validated in the piece format already; `samples` also needs an upload
  endpoint, and `beats` will share the piece's tempo grid so it inherits swing.
- **Live pedal reorder/toggle** (§4.5). The chain is rendered offline in phase 1,
  so the panel's pedal list is read-only and shows which pedals the current
  intensity has *notionally* switched on. Real live manipulation needs the chain
  ported to JS — roadmap step 5.
- **Preset switching** re-fetches a different rendered pair rather than re-running
  the chain, for the same reason.
- **Audio bitcrush** is a WaveShaper quantizing curve, which gets the stepped,
  aliased character but not true sample-rate reduction. That needs an
  AudioWorklet.
- **A lossless master** — recording is real-time WebM/Opus via `MediaRecorder`.
  An `OfflineAudioContext` render would be faster and uncompressed but cannot
  capture live playing.
- **WebGL** is untouched — Canvas2D is holding up fine at eight voices and a
  ~240-column waterfall, as §4.1 predicted it might.
- **Tides and UVB-76** — the ingestion layer is where they plug in; CSV, git
  repositories and exported histories exist so far.
