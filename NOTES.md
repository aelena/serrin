# Implementation notes

Decisions taken while building v0.1 that the design document left open, plus the
things that turned out differently in practice. Kept separate from the README so
the README stays about *using* the thing.

## Answered from section 4.2 ("to decide during implementation")

**Tick resolution: data-driven.** One row — or one aggregated window — is one
frame. The alternative read of the question was a musical grid (every 16th note),
which would impose a metre the data does not have and would make `delta` sound
quantized rather than reactive. `granularity` is the knob for "I want fewer,
chunkier events" and it stays in the data's own terms.

*Revised after the first pass:* the frame **spacing** is now expressed as a
tempo, which is not the same as sequencing to a grid — see the tempo section
below. The data still decides what happens on each step.

**Stream exhaustion: author's choice, `vary` by default.** Four policies:

| policy | behaviour |
|---|---|
| `vary` | loops with a per-pass phase offset and small transposition, both seed-derived |
| `loop` | plain repeat |
| `pingpong` | odd passes read backwards — deltas invert, so the piece un-happens |
| `once` | silence at the end |

`vary` is the default because §5.1.2 asks for repetition without an obvious raw
loop, and because a plain loop of eight voices becomes audible as a loop within
about two passes.

## Answered from section 8 (open questions)

**"If the stroke is drawn live, can it be recorded for identical playback?"**
Yes. A live stroke is captured as `(t, intensity)` points against the transport
clock and resampled onto the same even grid an offline curve uses, so it
serialises and replays exactly. `tests/test_runtime.mjs` asserts the round-trip.
What is *not* preserved is the gesture's relation to a different-length piece —
hence time normalization, which lets a three-second stroke drive a six-minute
piece.

**Progression archetypes: kept, as a curve generator.** They produce points and
then get out of the way; nothing downstream can tell an archetype from a
free-hand stroke. That is cheap enough that the question of whether they "stay"
does not really need deciding — if they turn out to be unnecessary, deleting the
table costs nothing.

Still genuinely open: **commit-graph traversal order.** Nothing implemented, so
nothing to report. See "if the commit graph is explored" below.

## Things the document specified that turned out to need a decision

**`stutter_repeat` needed a sliding window, not aligned blocks.** The first
implementation tested block-aligned windows and walked straight past the flatline
in the sample data, because a wedged collector does not politely begin its
flatline on a multiple of eight. Detection now slides one frame at a time and
only jumps a whole block when it fires.

**Automatic column selection had to drop monotonic columns.** `timestamp` has
enormous variance and zero shape; by variance alone it was winning a voice slot
and quantizing to a rising ramp. Now anything >98% monotonic is skipped in
automatic selection — but honoured if named explicitly, since the ramp is
occasionally what you want.

**Channel references wrap instead of raising.** A preset written against an
8-column dump names `driver_column: 7`; pointed at a 3-column CSV that crashed
mid-render. Wrapping keeps presets portable across datasets of different widths.
Named columns still raise — an absent *name* is a mistake, not a shape mismatch.

**The LFSR could walk into zero.** Worth writing out, because the first fix
treated the symptom.

All-zero is an absorbing state for any LFSR, and the design document's own
example taps (`[3, 1]` on 8 bits) reached it on the *second* call — so
`xor_mask` with `mask_source: lfsr` XORed against a constant zero. A silent
bypass: the pedal ran, the chain reported success, and the audio was unchanged.

The first attempt reloaded the register whenever it hit zero. That works and
explains nothing. The real statement is algebraic:

> The state map is `a[t+n] = XOR of a[t+j] for j in taps`, whose characteristic
> polynomial is `x^n + Σ x^j`. That map is linear over GF(2) and invertible
> exactly when the polynomial has a nonzero constant term — i.e. when `0` is one
> of the taps. An invertible map on 2ⁿ states is a bijection, so all-zero has
> exactly one preimage: itself. A nonzero state therefore **cannot** reach zero.

So the fix is to require tap 0, which is also just the standard convention —
tap lists are polynomial exponents with the `+ 1` left implicit. The reload
guard became provably dead code and was deleted. `test_state_map_is_a_bijection`
checks the property directly rather than checking that nothing died in practice.

Two further findings fell out of testing it:

- **Period depends on the seed, for non-primitive taps.** A primitive polynomial
  has one cycle through all 2ⁿ−1 nonzero states, so every channel runs the same
  length. A non-primitive one partitions the space into cycles of *different*
  lengths — `[3, 1, 0]` on 8 bits has cycles of 1, 2, 21 and 42. Channels are
  seeded independently, so they can buzz at different periods and drift in and
  out of alignment. That is a feature, but it means "the period" is per-channel,
  and the mask as a whole only repeats at their least common multiple. Both are
  recorded in the export.
- **Some of those cycles are degenerate.** A seed landing on the length-1 cycle
  makes the register emit one value forever — `mask_source: const` wearing an
  `lfsr` label, which is the same silent-degradation failure one level up. Seeds
  are now drawn until they land on a cycle worth hearing, deterministically,
  from the pedal's own substream.

Defaults are primitive polynomials per bit width, verified by measuring the real
period rather than by trusting the table.

**The forked export collapsed on narrow streams.** The visual side positions a
voice using a *rotated* channel so it is not a readout of its own pitch. With the
default rotation of 3 and a 3-voice stream, `3 % 3 == 0` and the rotation
vanished — audio pitch and visual position correlated at r=0.91. There is now a
test asserting r < 0.6, and the rotation can never land on zero.

## Tempo, added after the first pass

Section 4.2 offered a fixed musical grid *or* a data-driven tick, and the first
pass picked data-driven and left the frame rate as a bare number. That was half
an answer. The two framings were never opposed: 8 frames per second **is**
sixteenth notes at 120 BPM — serrin's default was already a tempo, it just had
no name, so nothing could be expressed in terms of it.

Naming it changes nothing about *what* happens on a step. It changes what can be
said about *when* the steps are, which is the one thing the data has no opinion
about — a CSV row has no duration.

What it bought:

- **Beat-relative LFOs.** `sine:4beats` stays locked to the grid at any tempo;
  `sine:0.1hz` still drifts across it. Before, only the second was expressible,
  so a "tremolo" was really a slow sweep that happened to look rhythmic.
- **A delay in note values.** Previously the delay was three frames of whatever
  the rate happened to be. Now it is a dotted eighth, and it stays one when the
  BPM changes.
- **Swing** — the actual difference between a grid and a tempo. Applied in
  exactly one place, `Reader.frameOnset`, which both engines already time off,
  so the feel reaches the picture as well as the sound for free.

Two things that needed care:

**Swing must not reorder frames.** The transport's scheduler walks frames in
order and would silently drop any whose onset moved backwards past its
predecessor — a missing note, not an error. Swing is capped at a third of a step
(triplet feel), which keeps onsets strictly increasing. There is a test asserting
that at every swing value.

**Changing tempo live has to re-anchor the transport.** `startTime` fixes where
frame 0 sounds and every other onset is measured from it, so moving the BPM
slider silently moves every future onset, including ones already inside the
lookahead window. `Transport.retime()` pins the current frame where it is and
applies the new grid from there. Without it the transport either stalls or dumps
a burst of frames at once.

The formulas now exist in both Python and JS, which is a real cost. They are not
shared because the pipeline needs tempo to place frames in a file and the runtime
needs it to place them against the audio clock *and* to let the author change it
live — a baked-in rate would make the BPM slider a re-render rather than a knob.
`tests/run_all.py` computes the grid in Python and hands it to the Node suite to
assert against, swung onsets included, so the two cannot drift apart quietly.

## Deliberate departures

**Columnar export.** `voice.freq[i]` rather than `frames[i].voices[n].freq`. About
a third of the bytes and no per-frame allocation in the reader. The document's
`{freq, amp, dur, gate}` shape is what the *reader* hands out; it is just not how
it is stored.

**Persistent oscillators.** One oscillator per voice for the session, with gain
envelopes for events, rather than a node per note. Eight voices at 12 fps would
otherwise mean ~100 node allocations a second and a GC pause exactly at the
climax. It also lets frequency glide when the data has not moved enough to
retrigger, which is what turns flat stretches into drones rather than a stutter
of identical notes.

**Bitcrush by WaveShaper.** A quantizing transfer curve, not an AudioWorklet.
Gets the stepped, aliased character with no module to serve; does not get true
sample-rate reduction, which is the other half of the lo-fi sound. Upgrade path
if the Amiga/tracker idea (§6.2) is ever pursued.

**One document, not two windows.** The panel is an overlay in the same page as
the stage. §4.5 wants it out of the piece, and §5.1 wants live drawing on the
engine's clock; a separate window would satisfy the first and then have to fake
the second.

## The commit graph, and three wrong answers about branches

Section 6.3 guessed that a repository would "fit as an ingestion adapter, not a
separate system". That was right, and cheaply so: the only place in serrin that
knows there is more than one kind of source is a single `if kind == "git"` in the
CLI. Pedals, export and runtime never learned anything.

The document's other predictions also held. Hash bytes really are noise, and the
consequence is more interesting than it sounds: monitoring data has structure for
the pedals to *break*, and hashes have none, so on a graph the aggressive pedals
stop destroying and start merely relabelling. `xor_mask` on a hash is one flavour
of randomness exchanged for another. That is why `merkle_drift` is the gentlest
preset in the repo -- the source arrives pre-shredded, so the work left to do is
the opposite one: `mod_reduce` pulling the noise *onto* a scale so it sounds like
an instrument rather than static.

Stale branches producing sparse voices worked exactly as described, and is now
asserted: a three-commit branch changes value in under 15% of frames, because a
voice holds its value between its own commits and `delta` reads holding as
silence.

**The hard part was branch membership, and it took three attempts.** Git does not
record which branch a commit was made on -- a commit belongs to every branch that
can reach it, and nearly all of them reach the whole trunk. So ownership has to
be *assigned*, and the first two rules were both wrong in the worst way: they
produced plausible output.

1. *Claim in branch-recency order.* The newest branch reaches the entire trunk,
   so it swallowed it. `main` and the merged feature branch were left owning
   nothing and dropped as silent: two voices where the repo visibly had four.
2. *Claim smallest-reachable-set first.* Better, and still wrong -- a feature
   branch reaches the trunk too and is smaller than `main`, so it took the trunk
   and `main` was left owning one commit. Four voices, three mislabelled.
3. *Ask git the question it can actually answer.* `rev-list ref --not <all other
   refs>` gives a branch's exclusive commits, which are unambiguously its own.
   The shared trunk then goes to the trunk branch. Correct, and it needed a
   conventional-name preference on top, because a long-lived feature branch can
   out-reach `main` and would otherwise be handed the shared history -- true by
   reachability, wrong by every other reading.

None of the three would have been caught by a test against this repository, which
had one branch and four commits. The fixture had to be built first -- and then
fixed, because its first version stopped advancing `main` after branching, which
made `main` an ancestor of the work branch and legitimately voiceless. The
fixture was hiding the case it existed to test.

**A `--numstat` parsing bug worth naming.** Git prints the pretty-format line,
then a blank line, then the per-file stats. With the record separator at the *end*
of the format string, each commit's stats landed in the *next* commit's chunk --
which did not crash, did not lose data visibly, and quietly produced four commits
all reporting identical churn. Separator at the front fixes it. The lesson is
that a parser for a format with trailing blocks wants leading delimiters.

## Recording, and why not the clean way

There are two ways to get audio out of a Web Audio graph, and the tempting one is
wrong for this project.

`OfflineAudioContext` renders faster than real time, deterministically, to
uncompressed samples. It is the obvious choice for a *generator*. But it renders
a graph, not a performance — it cannot capture anything played on the keyboard or
an envelope drawn while listening, because in an offline render there is no
listening. It also means building the node graph a second time, which is a second
place for the sound to diverge from what the piece actually is.

`MediaRecorder` taps the live master and captures exactly what was heard,
including the performance. It costs real time and a lossy codec. Given that the
keyboard and the live stroke are the two features that make this a performance
tool rather than a batch renderer, capturing the performance is the default that
matches what the thing is. The offline master is a later addition, not a
replacement.

One implementation note worth keeping: the tap is created once and kept.
Connecting a fresh `MediaStreamAudioDestinationNode` per take would leave the
previous ones attached to the master, quietly summing.

## The upload endpoint, and the port that had to change

The browser cannot transform anything: the pedals are Python. Three ways out, and
the middle one is the only honest one.

Porting the chain to JavaScript is roadmap step 5, and doing it *for this* would
have been the wrong reason. It duplicates nine pedals, ingestion and the export
mapping, and creates a second source of truth for the aesthetic — two
implementations that drift, with the *sound* as the thing that drifts. Staying
CLI-only was the other option, and it works, but "drop the file in the folder and
re-run the command" is not a source picker.

So: the browser reads the file as text, posts JSON, and the one real pipeline
renders it. JSON rather than multipart because hand-rolling a multipart parser for
a one-field form is a poor trade; the cost is that the CSV is fully buffered,
hence the size cap.

The endpoint also writes a session next to the pair, so an uploaded render is
reproducible from the moment it exists rather than only once someone remembers to
save one.

**The bind address had to change.** The server listened on `0.0.0.0` from the
start, which was unremarkable while it only served static files. An endpoint that
writes files it is given and runs renders on request is a different proposition,
so the default is now `127.0.0.1`, with `--host` as an explicit opt-out that
prints what it is doing.

**And the endpoint found a real bug elsewhere.** `Chain.resolve_seed` assumed the
source was a CSV and opened it to hash its first rows. The CLI had quietly worked
around this by special-casing repositories before calling it — so the *other*
callers, the endpoint and any session re-render, tried to read a directory as a
file and died on `PermissionError`. The fix was to move the dispatch into
`resolve_seed` where it belongs; one place that knows how to seed from whatever
the source is, rather than three callers each remembering to special-case it.

## Sessions, and the seam that had to be visible

The temptation was one file that "saves the piece". That would have been a lie,
because two different things were being saved.

Some settings are *render inputs*: change the chain, the columns, the mapping,
and the exported JSON is a different file. Others are *runtime state*: master
level, filter cutoff, mutes, visual toggles, keyboard register. Those have no
offline meaning at all -- there is no offline for them to have a meaning in.

Pretending the two were one thing would produce the worst failure available:
loading a session, watching every control move, and hearing something that is
not what was saved, because the chain half silently did not apply. So the format
names the halves, `apply()` restores only the runtime one, and a fingerprint
mismatch produces a warning naming the command that *would* re-render.

Three consequences worth recording:

**The render layer is not a new schema.** It is exactly the preset schema the CLI
already accepted. That was not obvious at the outset, and inventing a parallel
"session render format" would have meant two loaders to keep in step forever. It
also made `--to-preset` nearly free: freezing a session is lifting one block out.

**Tempo legitimately lives in both halves.** It is the only field that is both a
render input -- it sets the frame rate, and a hertz LFO resolves against it --
and a live control. It sits in `source`, is applied on load, and folds into a
frozen preset's `ingest`. A test pins down the surprising half: with a *hertz*
LFO in the chain, changing the tempo changes the rendered samples; with a
*beat-locked* one it does not. That is the entire difference between the two
units, now asserted rather than merely described.

**The cross-language check earns its keep.** Both suites test their own half of
the format thoroughly, which leaves the seam between them untested -- and a
format goes wrong exactly there. A field the browser writes as `delay_note` and
Python reads as `delayNote` fails no unit test and yields a piece that is nearly
right. `run_all.py` now has the browser write a session through the real
`capture()`, has Python load it, and asserts the re-render reproduces the saved
fingerprint.

## The keyboard, and the first thing that is performed

Everything in serrin up to this point is reproducible from `source + chain +
seed`. A person hitting keys is not, so the keyboard is the first feature that
sits outside the project's central promise.

The compromise is the same one the live envelope stroke takes: the randomness is
seed-derived, so a given *sequence of presses* always produces the same notes.
The performance is not reproducible; the instrument is. That is also the
groundwork for the beats mode — a recorded key sequence can replay exactly.

Three decisions worth recording:

**The piece had to start declaring its key.** A piece can acquire a scale in two
unrelated places — `mod_reduce` inside the chain, or `MappingConfig.quantize_to`
on the way out — and neither was exported. `mod_reduce` wrote its scale into
`stream.meta` and nothing ever read it. The output mapping wins when both are
set, because it is what the exported frequencies actually obey. There is a test
asserting every exported pitch really is in the declared scale, which is the
property the keyboard depends on.

A piece may legitimately declare nothing — full chromaticism is a deliberate
option and `corrupted_dump` takes it. That case reports the documented default
*and says so in `source`*, so the panel can be honest rather than inventing a
key the piece never had.

**Played notes bypass the bitcrusher.** The one deliberate exception to
"everything gets the same dirt". The intensity envelope drops the crusher to
three bits at the climax, which is exactly when a hand-played melody most needs
to be heard; crushing it would defeat the point of playing along. It still gets
the filter and the tempo-synced delay, so it is in the same room rather than
pasted on top. There is a checkbox for authors who disagree.

**A node per note, unlike the data voices — for the opposite reason.** Data
voices are eight continuous streams, so persistent oscillators avoid constant
allocation. Played notes are short, sparse and polyphonic, so spawning one per
press is both simpler and correct. Ten fingers cannot out-allocate a garbage
collector.

The modes are a registry with one entry implemented and three declared inert.
They appear in the dropdown, disabled. Listing them makes the shape visible and
gives the next commit an obvious place to land; disabling them means nothing
pretends.

## Two bugs found by running it for real

**The dev server was single-threaded.** It began as `socketserver.TCPServer`,
which serves one request at a time. That is invisible until a browser is
actually attached — then any second request stalls behind the connection the
page is holding, and a `curl` against it hangs forever. Now
`ThreadingHTTPServer`. Worth stating because it only showed up once someone was
using the thing, not in any test.

**A hidden tab accumulated an unbounded visual backlog.** The transport hands
frames to the visual loop through a queue, drained on `requestAnimationFrame`.
Browsers suspend rAF on a hidden tab while the `AudioContext` clock keeps
running, so the scheduler kept filling a queue nobody was draining — a slow leak
in exactly the installation scenario the piece is meant for. Capped now, dropping
the *oldest* frames: sound is authoritative and pictures are transient, so on
returning to the tab you want the present, not a fast-forward through the
backlog.

## Where the aesthetic knobs live

Everything subjective is in one place: `MappingConfig` in `serrin/export.py`.
Note range, frequency curve, gate threshold, how much of visual density comes
from delta versus absolute value, the glitch threshold, the channel rotation.
§5 calls this "the layer where subjective judgment lives" and warns it is the
least specified part of the document — so it is a dataclass with defaults and a
`mapping` block in every preset, meant to be argued with rather than a set of
constants buried in a loop.

## The commit graph, in hindsight

The prediction above turned out to be exactly right, which is why it is left
here unedited: `ingest_repo()` returns a `Stream` and nothing downstream needed
changing. The traversal question was indeed a question about that one function --
answered chronological, because rhythm is the most musical thing a repository
has and topological order discards it.
