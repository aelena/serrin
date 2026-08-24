"""Tests for the two things fixed after the first pass: the LFSR, and tempo.

Kept separate from test_pipeline.py because both are about *why* rather than
what. The LFSR tests exist to pin down an algebraic property that is easy to
lose in a refactor, and the tempo tests exist to protect one invariant the
transport silently depends on (onsets must stay strictly increasing).

    python -m unittest tests.test_tempo_lfsr -v
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serrin import pedals  # noqa: E402
from serrin.pedals import MIN_LFSR_PERIOD  # noqa: E402
from serrin.chain import default_chain  # noqa: E402
from serrin.pedals.base import Lfo, parse_lfo  # noqa: E402
from serrin.rng import PRIMITIVE_TAPS, Lfsr, Rng  # noqa: E402
from serrin.stream import Stream  # noqa: E402
from serrin.tempo import Tempo, TempoError  # noqa: E402


def make_stream(voices: int = 2, length: int = 64, **kwargs) -> Stream:
    rng = Rng(7)
    return Stream(
        names=[f"v{i}" for i in range(voices)],
        data=[[rng.below(256) for _ in range(length)] for _ in range(voices)],
        **kwargs,
    )


# ---------------------------------------------------------------------------
class TestLfsrCannotDie(unittest.TestCase):
    """The LFSR's one real failure mode, and the proof the fix closes it.

    All-zero is an absorbing state for any LFSR. The interesting question is not
    whether it can be *started* there -- trivially guarded -- but whether it can
    *walk* there, which is a question about the tap polynomial, not the seed.
    """

    def test_taps_without_bit_zero_would_die(self):
        # The design document's raw example, before normalization. Kept here so
        # the regression is documented rather than folklore.
        def naive_step(state: int, taps: list[int], width: int = 8) -> int:
            bit = 0
            for tap in taps:
                bit ^= (state >> tap) & 1
            return ((state >> 1) | (bit << (width - 1))) & 0xFF

        state = 1
        for _ in range(10):
            state = naive_step(state, [3, 1])
        self.assertEqual(state, 0, "the unfixed version was supposed to die")

    def test_normalization_adds_the_implicit_one(self):
        # Tap lists are polynomial exponents with the `+ 1` left implicit, so
        # [3, 1] means x^8 + x^3 + x + 1.
        self.assertEqual(Lfsr(1, [3, 1], width=8).taps, [3, 1, 0])

    def test_state_map_is_a_bijection(self):
        # This is the actual fix. An invertible map on 2**n states means all-zero
        # has exactly one preimage -- itself -- so no nonzero state can reach it.
        for width in (4, 6, 8):
            for taps in ([3, 1], [4, 3, 2, 0], [1], None):
                with self.subTest(width=width, taps=taps):
                    landed = {Lfsr(seed, taps, width=width).next() for seed in range(1, 1 << width)}
                    self.assertNotIn(0, landed, "a nonzero state reached zero")
                    self.assertEqual(len(landed), (1 << width) - 1, "map is not injective")

    def test_never_dies_from_any_seed_or_taps(self):
        for width in (2, 5, 8, 12):
            for taps in ([3, 1], [1], [0], None):
                with self.subTest(width=width, taps=taps):
                    lfsr = Lfsr(0, taps, width=width)  # even the degenerate seed
                    for _ in range(3000):
                        self.assertNotEqual(lfsr.next(), 0)

    def test_only_bit_zero_tapped_falls_back(self):
        # x^n + 1 merely rotates the register: it never dies, but it repeats the
        # seed forever, which is silence dressed up as noise.
        self.assertNotEqual(Lfsr(1, [0], width=8).taps, [0])

    def test_primitive_taps_are_maximal_length(self):
        # Measured, not trusted: a wrong table entry would quietly shorten the
        # period and nothing else in the system would complain.
        for width, taps in PRIMITIVE_TAPS.items():
            if width > 14:
                continue  # the measurement itself costs 2**n steps
            with self.subTest(width=width):
                lfsr = Lfsr(1, taps, width=width)
                self.assertEqual(lfsr.period(), (1 << width) - 1)
                self.assertTrue(lfsr.is_maximal)

    def test_default_taps_are_maximal(self):
        self.assertTrue(Lfsr(1, None, width=8).is_maximal)

    def test_doc_taps_give_a_short_but_live_period(self):
        # 42 frames at 8 bits: short enough to read as a pulse rather than as
        # noise. Not a bug -- a different pedal.
        lfsr = Lfsr(1, [3, 1], width=8)
        self.assertEqual(lfsr.period(), 42)
        self.assertFalse(lfsr.is_maximal)

    def test_period_does_not_disturb_the_sequence(self):
        lfsr = Lfsr(12345, [4, 3, 2, 0], width=8)
        sampled = [lfsr.next() for _ in range(5)]
        lfsr.period()
        sampled += [lfsr.next() for _ in range(5)]
        fresh = Lfsr(12345, [4, 3, 2, 0], width=8)
        self.assertEqual(sampled, [fresh.next() for _ in range(10)])

    def test_non_primitive_taps_give_seed_dependent_periods(self):
        # The subtlety worth pinning down. A primitive polynomial has ONE cycle
        # through every nonzero state, so the period cannot depend on the seed.
        # A non-primitive one partitions the space into several cycles of
        # different lengths, so it does -- [3, 1] runs 42 from one seed and 21
        # from another.
        lengths = {Lfsr(seed, [3, 1], width=8).period() for seed in range(1, 256)}
        # Four cycles, and two of them are degenerate: a seed landing on the
        # length-1 cycle makes the register emit one value forever, which is
        # `mask_source: const` wearing an lfsr label. Hence _seed_lfsr.
        self.assertEqual(sorted(lengths), [1, 2, 21, 42])

        maximal = {Lfsr(seed, [4, 3, 2, 0], width=8).period() for seed in range(1, 256)}
        self.assertEqual(maximal, {255}, "a primitive polynomial has one cycle")

    def test_seeding_avoids_degenerate_cycles(self):
        # Every channel must land on a cycle worth hearing, whatever the taps.
        for taps in ([3, 1], [1], [0], None, [5, 2]):
            with self.subTest(taps=taps):
                out = pedals.get("xor_mask")(
                    make_stream(voices=8), {"mask_source": "lfsr", "taps": taps}, Rng(3)
                )
                for period in out.meta["lfsr"]["periods"]:
                    self.assertGreaterEqual(period, MIN_LFSR_PERIOD, f"taps={taps}")

    def test_null_taps_mean_use_the_default(self):
        # A preset may legitimately write "taps": null.
        out = pedals.get("xor_mask")(
            make_stream(), {"mask_source": "lfsr", "taps": None}, Rng(1)
        )
        self.assertEqual(out.meta["lfsr"]["taps"], PRIMITIVE_TAPS[8])

    def test_xor_mask_reports_periods_per_channel(self):
        out = pedals.get("xor_mask")(
            make_stream(voices=4), {"mask_source": "lfsr", "taps": [3, 1]}, Rng(1)
        )
        lfsr = out.meta["lfsr"]
        self.assertEqual(lfsr["taps"], [3, 1, 0])
        self.assertEqual(len(lfsr["periods"]), 4, "one period per channel")
        self.assertTrue(all(p in (21, 42) for p in lfsr["periods"]), lfsr["periods"])
        # The mask as a whole only repeats when every channel does at once.
        self.assertEqual(lfsr["period_frames"], math.lcm(*lfsr["periods"]))
        self.assertFalse(lfsr["maximal"])

    def test_maximal_taps_report_one_period_for_every_channel(self):
        out = pedals.get("xor_mask")(
            make_stream(voices=4), {"mask_source": "lfsr", "taps": None}, Rng(1)
        )
        lfsr = out.meta["lfsr"]
        self.assertEqual(set(lfsr["periods"]), {255})
        self.assertEqual(lfsr["period_frames"], 255)
        self.assertTrue(lfsr["maximal"])

    def test_lfsr_mask_actually_masks(self):
        # The bug this whole class is about was silent: a dead LFSR XORs against
        # zero, which is a bypass that looks like a working pedal.
        stream = make_stream(voices=1, length=200)
        out = pedals.get("xor_mask")(
            stream, {"mask_source": "lfsr", "taps": [3, 1]}, Rng(1)
        )
        unchanged = sum(1 for a, b in zip(stream.data[0], out.data[0]) if a == b)
        self.assertLess(unchanged, len(stream.data[0]) // 4, "the mask did nothing")


# ---------------------------------------------------------------------------
class TestTempo(unittest.TestCase):
    def test_the_old_default_is_120_bpm_in_sixteenths(self):
        # serrin ran at 8 frames/s before tempo existed. That was never an
        # arbitrary number; it just had no name.
        self.assertEqual(Tempo().rate, 8.0)
        self.assertEqual(Tempo(bpm=120, subdivision=16).rate, 8.0)

    def test_rate_round_trips(self):
        for rate in (2.0, 4.0, 6.0, 8.0, 12.0, 33.3):
            with self.subTest(rate=rate):
                self.assertAlmostEqual(Tempo.from_rate(rate).rate, rate, places=9)

    def test_subdivision_is_a_note_value_not_a_count(self):
        self.assertEqual(Tempo(bpm=120, subdivision=4).rate, 2.0)  # quarters
        self.assertEqual(Tempo(bpm=120, subdivision=8).rate, 4.0)  # eighths
        self.assertEqual(Tempo(bpm=120, subdivision=16).rate, 8.0)  # sixteenths

    def test_shorthand_parsing(self):
        self.assertEqual(Tempo.parse("96").bpm, 96)
        self.assertEqual(Tempo.parse("96/8").subdivision, 8)
        self.assertAlmostEqual(Tempo.parse("128/16+0.3").swing, 0.3)
        self.assertEqual(Tempo.parse("140bpm").bpm, 140)
        self.assertEqual(Tempo.parse(140).bpm, 140)
        self.assertEqual(Tempo.parse({"bpm": 70, "subdivision": 8}).subdivision, 8)
        self.assertEqual(Tempo.parse(None).bpm, 120)

    def test_bad_tempo_is_loud(self):
        for bad in ("nonsense", "0", "120/16+9", "-20"):
            with self.subTest(spec=bad), self.assertRaises(TempoError):
                Tempo.parse(bad)

    def test_swing_only_moves_offbeats(self):
        tempo = Tempo(bpm=120, subdivision=16, swing=1.0)
        self.assertEqual(tempo.swing_offset(0), 0.0)
        self.assertEqual(tempo.swing_offset(2), 0.0)
        self.assertGreater(tempo.swing_offset(1), 0.0)

    def test_swing_never_reorders_frames(self):
        # The invariant the transport depends on. Its scheduler walks frames in
        # order, so an onset that moved backwards past its predecessor would be
        # dropped -- silently, as a missing note rather than an error.
        for swing in (0.0, 0.25, 0.5, 0.99, 1.0):
            with self.subTest(swing=swing):
                tempo = Tempo(bpm=140, subdivision=16, swing=swing)
                onsets = [tempo.onset(i) for i in range(64)]
                self.assertEqual(onsets, sorted(onsets), "swing reordered frames")
                self.assertEqual(len(set(onsets)), len(onsets), "swing collided frames")

    def test_triplet_swing_splits_the_pair_two_to_one(self):
        tempo = Tempo(bpm=120, subdivision=16, swing=1.0)
        first = tempo.onset(1) - tempo.onset(0)
        second = tempo.onset(2) - tempo.onset(1)
        self.assertAlmostEqual(first / second, 2.0, places=6)

    def test_straight_grid_is_evenly_spaced(self):
        tempo = Tempo(bpm=120, subdivision=16)
        gaps = [tempo.onset(i + 1) - tempo.onset(i) for i in range(16)]
        for gap in gaps:
            self.assertAlmostEqual(gap, tempo.seconds_per_step)

    def test_speed_scales_the_whole_grid(self):
        tempo = Tempo(bpm=120, subdivision=16, swing=0.5)
        for i in (1, 5, 33):
            self.assertAlmostEqual(tempo.onset(i, speed=2.0), tempo.onset(i) / 2.0)

    def test_positions_read_like_a_daw(self):
        tempo = Tempo(bpm=120, subdivision=16, beats_per_bar=4)
        self.assertEqual(tempo.format_position(0), "1.1.1")
        self.assertEqual(tempo.format_position(4), "1.2.1")
        self.assertEqual(tempo.format_position(16), "2.1.1")
        self.assertEqual(tempo.bars(32), 2.0)

    def test_note_values(self):
        tempo = Tempo(bpm=120)
        self.assertAlmostEqual(tempo.note_seconds("1/4"), 0.5)
        self.assertAlmostEqual(tempo.note_seconds("1/8."), 0.375)
        self.assertAlmostEqual(tempo.note_seconds("1/8t"), 1.0 / 6.0)
        with self.assertRaises(TempoError):
            tempo.note_seconds("1/7")

    def test_stream_reconciles_tempo_and_rate(self):
        inferred = Stream(names=["a"], data=[[1] * 8], rate=6.0)
        self.assertAlmostEqual(inferred.tempo.rate, 6.0)
        explicit = Stream(names=["a"], data=[[1] * 8], tempo=Tempo(bpm=90, subdivision=8))
        self.assertAlmostEqual(explicit.rate, 3.0)

    def test_tempo_survives_a_chain(self):
        stream = make_stream(length=64, tempo=Tempo(bpm=90, subdivision=8, swing=0.4))
        out = default_chain().apply(stream, seed=1)
        self.assertEqual(out.tempo.bpm, 90)
        self.assertEqual(out.tempo.subdivision, 8)
        self.assertEqual(out.tempo.swing, 0.4)

    def test_tempo_reaches_the_export(self):
        from serrin.export import build_piece

        stream = make_stream(length=64, tempo=Tempo(bpm=96, subdivision=8))
        piece = build_piece(stream)
        self.assertEqual(piece.meta["tempo"]["bpm"], 96)
        self.assertEqual(piece.meta["tempo"]["subdivision"], 8)
        self.assertAlmostEqual(piece.meta["tempo"]["rate"], 3.2)
        self.assertIn("bars", piece.meta)
        self.assertIn("delay_note", piece.meta)


# ---------------------------------------------------------------------------
class TestTempoSyncedLfo(unittest.TestCase):
    def test_beat_and_bar_units_land_on_the_grid(self):
        stream = Stream(names=["a"], data=[[100] * 64], tempo=Tempo(120, 16))
        cases = {
            "sine:4beats": 16.0,  # 4 beats x 4 steps per beat
            "saw:1bar": 16.0,  # the same length, said differently
            "triangle:1/2beat": 2.0,
            "square:2bars": 32.0,
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                self.assertAlmostEqual(Lfo(spec, stream, Rng(1))._period, expected)

    def test_hertz_still_means_hertz(self):
        stream = Stream(names=["a"], data=[[100] * 64], tempo=Tempo(120, 16))
        self.assertAlmostEqual(Lfo("sine:0.1hz", stream, Rng(1))._period, 80.0)

    def test_a_beat_lfo_tracks_tempo_and_a_hz_one_does_not(self):
        # The whole point of the beat unit: locked to the grid, not to the wall.
        slow = Stream(names=["a"], data=[[100] * 64], tempo=Tempo(60, 16))
        fast = Stream(names=["a"], data=[[100] * 64], tempo=Tempo(120, 16))
        self.assertEqual(
            Lfo("sine:2beats", slow, Rng(1))._period,
            Lfo("sine:2beats", fast, Rng(1))._period,
        )
        self.assertNotEqual(
            Lfo("sine:0.5hz", slow, Rng(1))._period,
            Lfo("sine:0.5hz", fast, Rng(1))._period,
        )

    def test_fraction_and_unit_parsing(self):
        self.assertEqual(parse_lfo("sine:1/4beat"), ("sine", 0.25, "beat", 1.0))
        self.assertEqual(parse_lfo("saw:2bars:0.5"), ("saw", 2.0, "bar", 0.5))
        self.assertEqual(parse_lfo("square:3hz"), ("square", 3.0, "hz", 1.0))
        self.assertIsNone(parse_lfo("fixed"))
        self.assertIsNone(parse_lfo(None))

    def test_bad_lfo_specs_are_loud(self):
        for bad in ("wobble:1beat", "sine:0beats", "sine:zzzbeats", "sine:-1hz"):
            with self.subTest(spec=bad), self.assertRaises(pedals.PedalError):
                parse_lfo(bad)

    def test_a_beat_synced_lfo_actually_modulates(self):
        stream = Stream(names=["a"], data=[[100] * 128], tempo=Tempo(120, 16))
        out = pedals.get("caesar")(stream, {"shift": 5, "shift_lfo": "sine:4bars"}, Rng(1))
        self.assertGreater(len(set(out.data[0])), 16, "the LFO did not move the shift")


if __name__ == "__main__":
    unittest.main(verbosity=2)
