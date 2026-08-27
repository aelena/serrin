"""Tests for the offline pipeline.

Weighted toward the two properties the project's aesthetic actually depends on,
rather than toward line coverage:

  * **determinism** -- "reproducible but not hand-predictable" (section 1) is a
    promise, and a promise that is not tested is a wish. If a chain stops being
    reproducible, every labelled piece stops being regenerable.
  * **invariants** -- pedals must preserve voice count, channel length, and bit
    depth. Break one and the failure surfaces hundreds of frames later as a
    browser exception, which is a miserable way to find out.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serrin import envelope as env_mod  # noqa: E402
from serrin import pedals  # noqa: E402
from serrin.chain import Chain, ChainError, Slot, default_chain  # noqa: E402
from serrin.envelope import Envelope, active_voice_count, voice_gates  # noqa: E402
from serrin.export import MappingConfig, build_render, to_audio, to_visual  # noqa: E402
from serrin.ingest import IngestError, auto_seed, ingest_csv, monotonicity, quantize  # noqa: E402
from serrin.rng import Lfsr, Rng, derive, seed_from_bytes  # noqa: E402
from serrin.scales import ScaleError, midi_to_hz, parse_intervals, resolve  # noqa: E402
from serrin.stream import MAX_VOICES, Stream  # noqa: E402

CSV = """timestamp,cpu,mem,flat,spiky
0,10,50,7,1
1,12,51,7,1
2,40,52,7,200
3,11,53,7,2
4,13,54,7,1
5,90,55,7,255
6,14,56,7,3
7,15,57,7,1
8,16,58,7,1
9,17,59,7,1
10,18,60,7,1
11,19,61,7,1
"""


def sample_csv() -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write(CSV)
    handle.close()
    return Path(handle.name)


def make_stream(voices: int = 3, length: int = 64, bit_depth: int = 8) -> Stream:
    rng = Rng(7)
    return Stream(
        names=[f"v{i}" for i in range(voices)],
        data=[[rng.below(1 << bit_depth) for _ in range(length)] for _ in range(voices)],
        bit_depth=bit_depth,
        rate=8.0,
    )


# ---------------------------------------------------------------------------
class TestRng(unittest.TestCase):
    def test_same_seed_same_sequence(self):
        a = [Rng(42).next_u64() for _ in range(5)]
        b = [Rng(42).next_u64() for _ in range(5)]
        self.assertEqual(a, b)

    def test_different_seeds_diverge(self):
        self.assertNotEqual(Rng(1).next_u64(), Rng(2).next_u64())

    def test_float01_stays_in_range(self):
        rng = Rng(3)
        for _ in range(2000):
            value = rng.float01()
            self.assertGreaterEqual(value, 0.0)
            self.assertLess(value, 1.0)

    def test_derive_is_label_keyed_and_stable(self):
        self.assertEqual(derive(9, "delta/0").next_u64(), derive(9, "delta/0").next_u64())
        self.assertNotEqual(derive(9, "delta/0").next_u64(), derive(9, "delta/1").next_u64())

    def test_seed_from_bytes_is_64_bit(self):
        value = seed_from_bytes(b"serrin")
        self.assertTrue(0 <= value < (1 << 64))

    def test_lfsr_never_dies_and_stays_in_width(self):
        # A zero state is absorbing: the LFSR would output silence forever where
        # the chain expects noise.
        lfsr = Lfsr(0, [3, 1], width=8)
        values = [lfsr.next() for _ in range(300)]
        self.assertTrue(all(0 <= v < 256 for v in values))
        self.assertGreater(len(set(values)), 1, "LFSR is stuck")


class TestScales(unittest.TestCase):
    def test_doc_example_harmonic_minor(self):
        # The design document spells this one out, so it is the reference case.
        offsets, span = resolve("1, 1/2, 1, 1, 1/2, 1 1/2, 1/2")
        self.assertEqual(offsets, [0, 2, 3, 5, 7, 8, 11])
        self.assertEqual(span, 12)
        self.assertEqual(resolve("harmonic_minor")[0], offsets)

    def test_greek_modes_all_span_an_octave(self):
        for name in ("ionian", "dorian", "phrygian", "lydian", "mixolydian", "aeolian", "locrian"):
            offsets, span = resolve(name)
            self.assertEqual(len(offsets), 7, name)
            self.assertEqual(span, 12, name)

    def test_aliases(self):
        self.assertEqual(resolve("major"), resolve("ionian"))
        self.assertEqual(resolve("minor"), resolve("aeolian"))
        self.assertEqual(resolve("octatonic"), resolve("diminished"))

    def test_interval_parsing_handles_mixed_numbers(self):
        self.assertEqual(parse_intervals("1, 1/2, 1 1/2"), [Fraction(1), Fraction(1, 2), Fraction(3, 2)])

    def test_raw_offsets_accepted(self):
        offsets, _ = resolve([0, 3, 7, 10])
        self.assertEqual(offsets, [0, 3, 7, 10])

    def test_bad_scale_is_loud(self):
        with self.assertRaises(ScaleError):
            resolve("definitely_not_a_scale")

    def test_concert_pitch(self):
        self.assertAlmostEqual(midi_to_hz(69), 440.0)
        self.assertAlmostEqual(midi_to_hz(81), 880.0)


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.path = sample_csv()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_auto_selection_drops_constant_and_monotonic_columns(self):
        stream = ingest_csv(self.path)
        self.assertNotIn("flat", stream.names, "a constant column is not a voice")
        self.assertNotIn("timestamp", stream.names, "a ramp is not a voice")

    def test_explicit_columns_are_honoured_however_dull(self):
        stream = ingest_csv(self.path, columns=["timestamp", "flat"])
        self.assertEqual(stream.names, ["timestamp", "flat"])

    def test_voice_ceiling_is_enforced(self):
        with self.assertRaises(IngestError):
            ingest_csv(self.path, columns=["cpu"] * (MAX_VOICES + 1))

    def test_quantization_fills_the_range(self):
        values = quantize([0.0, 5.0, 10.0], 8)
        self.assertEqual(values[0], 0)
        self.assertEqual(values[-1], 255)

    def test_constant_column_quantizes_to_mid_scale(self):
        self.assertEqual(quantize([7.0] * 5, 8), [127] * 5)

    def test_granularity_aggregates(self):
        fine = ingest_csv(self.path, columns=["cpu"], granularity=1)
        coarse = ingest_csv(self.path, columns=["cpu"], granularity=4)
        self.assertEqual(coarse.length, -(-fine.length // 4))

    def test_bit_depth_is_respected(self):
        stream = ingest_csv(self.path, columns=["cpu"], bit_depth=4)
        self.assertTrue(all(0 <= v < 16 for v in stream.data[0]))

    def test_auto_seed_is_stable_and_content_sensitive(self):
        self.assertEqual(auto_seed(self.path), auto_seed(self.path))
        other = sample_csv()
        try:
            other.write_text(CSV.replace("cpu", "cpu_load"), encoding="utf-8")
            self.assertNotEqual(auto_seed(self.path), auto_seed(other))
        finally:
            other.unlink(missing_ok=True)

    def test_monotonicity_detects_ramps(self):
        self.assertEqual(monotonicity([1, 2, 3, 4, 5]), 1.0)
        self.assertLess(monotonicity([1, 5, 2, 6, 3]), 0.8)


class TestPedals(unittest.TestCase):
    """Every pedal, checked against the invariants the chain relies on."""

    def test_catalog_has_the_v01_nine(self):
        expected = {
            "caesar",
            "xor_mask",
            "delta",
            "mod_reduce",
            "bit_reverse",
            "interleave",
            "cross_mix",
            "bitcrush",
            "stutter_repeat",
        }
        self.assertEqual(set(pedals.REGISTRY), expected)

    def test_every_pedal_preserves_the_invariants(self):
        stream = make_stream()
        for pedal in pedals.catalog():
            with self.subTest(pedal=pedal.name):
                out = pedal(stream, {}, Rng(1))
                self.assertEqual(out.n_voices, stream.n_voices, "voice count changed")
                self.assertEqual(
                    len({len(channel) for channel in out.data}), 1, "channels desynchronised"
                )
                for channel in out.data:
                    self.assertTrue(
                        all(0 <= v < stream.ceiling for v in channel), "value escaped the bit depth"
                    )
                if pedal.name != "stutter_repeat":
                    self.assertEqual(out.length, stream.length, "length changed")

    def test_every_pedal_is_deterministic(self):
        stream = make_stream()
        for pedal in pedals.catalog():
            with self.subTest(pedal=pedal.name):
                self.assertEqual(
                    pedal(stream, {}, Rng(5)).data,
                    pedal(stream, {}, Rng(5)).data,
                )

    def test_unknown_params_are_rejected(self):
        with self.assertRaises(pedals.PedalError):
            pedals.get("caesar")(make_stream(), {"shft": 3}, Rng(1))

    def test_caesar_shifts_and_wraps(self):
        stream = Stream(names=["a"], data=[[0, 1, 254, 255]], bit_depth=8)
        out = pedals.get("caesar")(stream, {"shift": 5}, Rng(1))
        self.assertEqual(out.data[0], [5, 6, 3, 4])

    def test_caesar_lfo_moves_the_shift(self):
        # A constant input, so anything that varies in the output came from the
        # LFO. The LFO must be slow relative to the frame rate: one cycle per 8
        # frames only ever samples 8 phases, and the modulo folds several of
        # those onto the same byte.
        stream = Stream(names=["a"], data=[[100] * 128], bit_depth=8, rate=8.0)
        fixed = pedals.get("caesar")(stream, {"shift": 5}, Rng(1))
        moving = pedals.get("caesar")(stream, {"shift": 5, "shift_lfo": "sine:0.05hz"}, Rng(1))
        self.assertEqual(len(set(fixed.data[0])), 1)
        self.assertGreater(len(set(moving.data[0])), 16, "the LFO did not move the shift")

    def test_xor_const_is_its_own_inverse(self):
        stream = make_stream(voices=1)
        once = pedals.get("xor_mask")(stream, {"mask_source": "const", "mask": 0xA5}, Rng(1))
        twice = pedals.get("xor_mask")(once, {"mask_source": "const", "mask": 0xA5}, Rng(1))
        self.assertEqual(twice.data, stream.data)

    def test_xor_column_leaves_the_driver_alone(self):
        stream = make_stream(voices=3)
        out = pedals.get("xor_mask")(stream, {"mask_source": "column", "column": 0}, Rng(1))
        self.assertEqual(out.data[0], stream.data[0], "the driver silenced itself")
        self.assertNotEqual(out.data[1], stream.data[1])

    def test_delta_marks_stability_as_zero(self):
        stream = Stream(names=["a"], data=[[10, 10, 10, 40]], bit_depth=8)
        out = pedals.get("delta")(stream, {"order": 1}, Rng(1))
        self.assertEqual(out.data[0], [0, 0, 0, 30])

    def test_delta_of_delta(self):
        # First difference of [0,1,3,6,10] is [0,1,2,3,4] (frame 0 padded to 0),
        # so the second is [0,1,1,1,1]. The 1 at frame 1 is the padding showing
        # through -- an unavoidable transient of differencing a stream that has
        # no "before", and audible as one click at the top of the piece.
        stream = Stream(names=["a"], data=[[0, 1, 3, 6, 10]], bit_depth=8)
        out = pedals.get("delta")(stream, {"order": 2}, Rng(1))
        self.assertEqual(out.data[0], [0, 1, 1, 1, 1])

    def test_mod_reduce_bare_modulus(self):
        stream = Stream(names=["a"], data=[[0, 7, 8, 15]], bit_depth=8)
        out = pedals.get("mod_reduce")(stream, {"modulus": 7}, Rng(1))
        self.assertEqual(out.data[0], [0, 0, 1, 1])

    def test_mod_reduce_lands_only_on_scale_members(self):
        stream = make_stream(voices=1, length=256)
        out = pedals.get("mod_reduce")(
            stream, {"scale": "pentatonic_minor", "octaves": 2}, Rng(1)
        )
        allowed = {
            offset + 12 * octave
            for octave in range(3)
            for offset in resolve("pentatonic_minor")[0]
        }
        self.assertTrue(set(out.data[0]) <= allowed, "produced an out-of-scale pitch")

    def test_bit_reverse_is_an_involution(self):
        stream = make_stream(voices=1)
        once = pedals.get("bit_reverse")(stream, {}, Rng(1))
        twice = pedals.get("bit_reverse")(once, {}, Rng(1))
        self.assertEqual(twice.data, stream.data)

    def test_interleave_alternates_by_stride(self):
        stream = Stream(names=["a", "b"], data=[[1, 1, 1, 1], [9, 9, 9, 9]], bit_depth=8)
        out = pedals.get("interleave")(stream, {"a": 0, "b": 1, "stride": 2}, Rng(1))
        self.assertEqual(out.data[0], [1, 1, 9, 9])

    def test_cross_mix_depth_zero_is_a_bypass(self):
        stream = make_stream(voices=3)
        out = pedals.get("cross_mix")(stream, {"driver_column": 0, "depth": 0.0}, Rng(1))
        self.assertEqual(out.data, stream.data)

    def test_cross_mix_rejects_a_bad_op(self):
        with self.assertRaises(pedals.PedalError):
            pedals.get("cross_mix")(make_stream(), {"op": "nope"}, Rng(1))

    def test_bitcrush_quantizes_to_a_coarse_grid(self):
        stream = make_stream(voices=1, length=200)
        out = pedals.get("bitcrush")(stream, {"target_bits": 2}, Rng(1))
        self.assertLessEqual(len(set(out.data[0])), 4)
        self.assertTrue(all(v % 64 == 0 for v in out.data[0]))

    def test_bitcrush_rejects_impossible_depth(self):
        with self.assertRaises(pedals.PedalError):
            pedals.get("bitcrush")(make_stream(), {"target_bits": 99}, Rng(1))

    def test_stutter_repeat_stalls_on_flat_data(self):
        # A flat run that deliberately does not begin on a block boundary.
        data = [[5, 200, 5, 200] + [42] * 24 + [7, 199, 7]]
        stream = Stream(names=["a"], data=data, bit_depth=8)
        out = pedals.get("stutter_repeat")(
            stream, {"threshold": 1, "repeats": 3, "block": 8, "max_growth": 4.0}, Rng(1)
        )
        self.assertGreater(out.length, stream.length, "the flatline did not stall")

    def test_stutter_repeat_leaves_busy_data_alone(self):
        stream = make_stream(voices=2, length=128)
        out = pedals.get("stutter_repeat")(stream, {"threshold": 0, "block": 8}, Rng(1))
        self.assertEqual(out.length, stream.length)

    def test_stutter_repeat_respects_the_growth_cap(self):
        stream = Stream(names=["a"], data=[[42] * 128], bit_depth=8)
        out = pedals.get("stutter_repeat")(
            stream, {"threshold": 1, "repeats": 8, "block": 8, "max_growth": 1.5}, Rng(1)
        )
        self.assertLessEqual(out.length, int(128 * 1.5))


class TestChain(unittest.TestCase):
    def setUp(self):
        self.path = sample_csv()
        self.stream = ingest_csv(self.path, columns=["cpu", "mem", "spiky"])

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_same_seed_regenerates_identically(self):
        chain = default_chain()
        first = chain.apply(self.stream, seed=1234)
        second = chain.apply(self.stream, seed=1234)
        self.assertEqual(first.data, second.data)

    def test_different_seed_gives_a_different_piece(self):
        chain = default_chain()
        self.assertNotEqual(
            chain.apply(self.stream, seed=1).data,
            chain.apply(self.stream, seed=2).data,
        )

    def test_order_matters(self):
        forward = Chain(slots=[Slot("delta", {"order": 1}), Slot("bit_reverse")])
        backward = Chain(slots=[Slot("bit_reverse"), Slot("delta", {"order": 1})])
        self.assertNotEqual(
            forward.apply(self.stream, seed=9).data,
            backward.apply(self.stream, seed=9).data,
        )

    def test_disabled_slot_is_skipped(self):
        with_slot = Chain(slots=[Slot("bit_reverse", enabled=True)])
        without = Chain(slots=[Slot("bit_reverse", enabled=False)])
        self.assertEqual(without.apply(self.stream, seed=1).data, self.stream.data)
        self.assertNotEqual(with_slot.apply(self.stream, seed=1).data, self.stream.data)

    def test_intensity_gates_slots(self):
        chain = Chain(slots=[Slot("bit_reverse", at_intensity=0.8)])
        quiet = chain.apply(self.stream, seed=1, intensity=0.2)
        loud = chain.apply(self.stream, seed=1, intensity=0.9)
        self.assertEqual(quiet.data, self.stream.data)
        self.assertNotEqual(loud.data, self.stream.data)

    def test_metadata_records_what_ran(self):
        result = default_chain().apply(self.stream, seed=77)
        self.assertEqual(result.meta["seed"], 77)
        self.assertEqual(result.meta["chain_name"], "gritty_01")
        self.assertEqual(len(result.meta["chain_applied"]), 4)

    def test_preset_round_trips(self):
        chain = default_chain()
        restored = Chain.from_json(json.loads(json.dumps(chain.to_json())))
        self.assertEqual(restored.to_json(), chain.to_json())

    def test_unknown_pedal_fails_at_load_time(self):
        with self.assertRaises(pedals.PedalError):
            Chain.from_json({"chain": [{"pedal": "reverb_of_the_soul"}]})

    def test_fixed_seed_mode_needs_a_seed(self):
        with self.assertRaises(ChainError):
            Chain.from_json({"seed_mode": "fixed", "chain": []})

    def test_shipped_presets_all_load_and_run(self):
        presets = sorted((Path(__file__).resolve().parent.parent / "presets").glob("*.json"))
        self.assertGreater(len(presets), 0, "no presets shipped")
        for path in presets:
            with self.subTest(preset=path.stem):
                chain = Chain.load(path)
                result = chain.apply(self.stream, source=self.path)
                self.assertEqual(result.n_voices, self.stream.n_voices)


class TestEnvelope(unittest.TestCase):
    def test_constant_is_flat(self):
        envelope = Envelope.constant(0.4)
        self.assertEqual([envelope.at(t / 10) for t in range(11)], [0.4] * 11)

    def test_equations_stay_in_range(self):
        for name in env_mod.EQUATIONS:
            with self.subTest(equation=name):
                envelope = Envelope.from_equation(name)
                for value in envelope.sample(64):
                    self.assertGreaterEqual(value, 0.0)
                    self.assertLessEqual(value, 1.0)

    def test_arc_builds_then_resolves(self):
        arc = Envelope.from_equation("arc")
        self.assertGreater(arc.at(0.6), arc.at(0.05))
        self.assertGreater(arc.at(0.6), arc.at(0.98))

    def test_archetypes_are_all_usable(self):
        for name in env_mod.ARCHETYPES:
            with self.subTest(archetype=name):
                envelope = Envelope.from_archetype(name)
                self.assertGreater(len(envelope.points), 2)

    def test_stroke_time_is_normalized(self):
        # A gesture drawn over 3 arbitrary time units must drive a whole piece.
        envelope = Envelope.from_points([(100, 0.0), (250, 1.0), (400, 0.5)])
        self.assertAlmostEqual(envelope.at(0.0), 0.0, places=5)
        self.assertAlmostEqual(envelope.at(1.0), 0.5, places=5)

    def test_stroke_doubling_back_stays_a_function(self):
        envelope = Envelope.from_points([(0, 0.1), (0.5, 0.9), (0.5, 0.2), (1, 0.4)])
        times = [t for t, _ in envelope.points]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(times), len(set(times)))

    def test_spec_shorthands(self):
        self.assertEqual(Envelope.from_spec("archetype:climax").origin["kind"], "archetype")
        self.assertEqual(Envelope.from_spec("sigmoid:centre=0.3").origin["kind"], "equation")
        self.assertEqual(Envelope.from_spec(None).origin["kind"], "constant")

    def test_voice_activation_matches_the_js_side(self):
        # These exact numbers are asserted again in tests/test_runtime.mjs.
        self.assertEqual(active_voice_count(0.0, 8), 1)
        self.assertEqual(active_voice_count(1.0, 8), 8)
        self.assertLessEqual(active_voice_count(0.5, 8), 4)

    def test_voice_activation_is_monotonic(self):
        counts = [active_voice_count(i / 20, 8) for i in range(21)]
        self.assertEqual(counts, sorted(counts))

    def test_gates_follow_entry_order(self):
        gates = voice_gates(0.01, [2, 0, 1], minimum=1)
        self.assertEqual(gates, [False, False, True])

    def test_voice_order_strategies(self):
        stream = Stream(
            names=["quiet", "loud"],
            data=[[10, 10, 11, 10], [0, 255, 0, 255]],
            bit_depth=8,
        )
        self.assertEqual(env_mod.voice_order(stream, "columns"), [0, 1])
        self.assertEqual(env_mod.voice_order(stream, "variance"), [1, 0])
        self.assertEqual(env_mod.voice_order(stream, "sparse"), [0, 1])


class TestExport(unittest.TestCase):
    def setUp(self):
        self.path = sample_csv()
        stream = ingest_csv(self.path, columns=["cpu", "mem", "spiky"])
        self.stream = default_chain().apply(stream, seed=4242)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_audio_fork_shape(self):
        doc = to_audio(self.stream)
        self.assertEqual(len(doc["voices"]), self.stream.n_voices)
        for voice in doc["voices"]:
            for key in ("freq", "amp", "dur", "gate"):
                self.assertEqual(len(voice[key]), self.stream.length, key)
            self.assertIn(voice["waveform"], ("sawtooth", "square", "triangle", "sine"))
            self.assertTrue(all(f > 0 for f in voice["freq"]))
            self.assertTrue(all(0 <= a <= 1 for a in voice["amp"]))
            self.assertTrue(all(g in (0, 1) for g in voice["gate"]))

    def test_visual_fork_shape(self):
        doc = to_visual(self.stream)
        for voice in doc["voices"]:
            for key in ("x", "y", "density", "gray", "glyph", "glitch"):
                self.assertEqual(len(voice[key]), self.stream.length, key)
            self.assertTrue(all(0 <= v <= 1 for v in voice["density"]))
            self.assertTrue(all(0 <= g < len(doc["glyphs"]) for g in voice["glyph"]))

    def test_the_two_forks_are_not_the_same_number_twice(self):
        # Section 3.5's explicit warning, as an assertion.
        audio = to_audio(self.stream)["voices"][0]
        visual = to_visual(self.stream)["voices"][0]
        pitch = audio["freq"]
        position = visual["y"]
        self.assertLess(abs(_pearson(pitch, position)), 0.6, "the fork collapsed")

    def test_scale_quantization_on_the_way_out(self):
        # Every exported frequency must be a member of the requested scale,
        # measured back through the pitch mapping rather than trusted.
        config = MappingConfig(quantize_to="pentatonic_minor", note_low=36, note_high=72)
        doc = to_audio(self.stream, config)
        offsets = set(resolve("pentatonic_minor")[0])
        for voice in doc["voices"]:
            for freq in voice["freq"]:
                midi = round(69 + 12 * _log2(freq / 440.0))
                self.assertIn((midi - config.note_low) % 12, offsets, f"{freq} Hz is off-scale")

    def test_piece_metadata_identifies_the_render(self):
        rendered = build_render(self.stream, chain=default_chain(), envelope=Envelope.from_equation("arc"))
        meta = rendered.meta
        self.assertEqual(meta["seed"], 4242)
        self.assertIn("gritty_01", meta["label"])
        self.assertEqual(meta["frames"], self.stream.length)
        self.assertEqual(len(meta["voices"]), self.stream.n_voices)
        self.assertEqual(len(meta["voice_entry_order"]), self.stream.n_voices)
        self.assertTrue(meta["fingerprint"])
        self.assertEqual(len(rendered.envelope["curve"]), rendered.envelope["resolution"])

    def test_both_documents_carry_the_same_meta(self):
        rendered = build_render(self.stream, chain=default_chain())
        self.assertEqual(rendered.audio_document()["meta"], rendered.visual_document()["meta"])

    def test_fingerprint_tracks_content(self):
        other = default_chain().apply(
            ingest_csv(self.path, columns=["cpu", "mem", "spiky"]), seed=1
        )
        rendered_a = build_render(self.stream)
        rendered_b = build_render(other)
        self.assertNotEqual(rendered_a.meta["fingerprint"], rendered_b.meta["fingerprint"])


def _pearson(a, b) -> float:
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    dev_a = sum((x - mean_a) ** 2 for x in a) ** 0.5
    dev_b = sum((y - mean_b) ** 2 for y in b) ** 0.5
    return 0.0 if dev_a * dev_b == 0 else num / (dev_a * dev_b)


def _log2(x: float) -> float:
    import math

    return math.log2(x)


if __name__ == "__main__":
    unittest.main(verbosity=2)
