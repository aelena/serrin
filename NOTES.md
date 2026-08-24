# Implementation notes

Decisions taken while building v0.1 that the design document left open, plus the
things that turned out differently in practice. Kept separate from the README so
the README stays about *using* the thing.

## Answered from section 4.2 ("to decide during implementation")

**Tick resolution: data-driven.** One row — or one aggregated window — is one
frame, and `rate` sets frames per second. The alternative was a musical grid
(every 16th note), which would impose a metre the data does not have and would
make `delta` sound quantized rather than reactive. `granularity` is the knob for
"I want fewer, chunkier events" and it stays in the data's own terms.

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

**The LFSR could walk into zero.** All-zero is an absorbing state for an LFSR,
and the design document's own example taps (`[3, 1]` on 8 bits) reached it on the
second call, which meant `xor_mask` with `mask_source: lfsr` XORed against a
constant zero — a silent bypass. Bit 0 is now always part of the feedback, plus a
reload guard.

**The forked export collapsed on narrow streams.** The visual side positions a
voice using a *rotated* channel so it is not a readout of its own pitch. With the
default rotation of 3 and a 3-voice stream, `3 % 3 == 0` and the rotation
vanished — audio pitch and visual position correlated at r=0.91. There is now a
test asserting r < 0.6, and the rotation can never land on zero.

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

## Where the aesthetic knobs live

Everything subjective is in one place: `MappingConfig` in `serrin/export.py`.
Note range, frequency curve, gate threshold, how much of visual density comes
from delta versus absolute value, the glitch threshold, the channel rotation.
§5 calls this "the layer where subjective judgment lives" and warns it is the
least specified part of the document — so it is a dataclass with defaults and a
`mapping` block in every preset, meant to be argued with rather than a set of
constants buried in a loop.

## If the commit graph is explored (§6.3)

The ingestion layer is the seam. `ingest_csv` returns a `Stream` and everything
downstream only knows about `Stream`, so a `ingest_repo()` producing the same
shape would need no changes anywhere else. The open question (traversal order)
is a question about *that function* and nothing more, which is the strongest
argument that §6.3's instinct was right.
