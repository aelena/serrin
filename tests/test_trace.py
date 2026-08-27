"""Tests for the render trace.

The trace exists to answer "which stage did that", so the tests are mostly about
whether it answers correctly. Two of them are the interesting ones: that entropy
moves in the direction each pedal's description claims, and that the values shown
are honestly labelled as a window while the statistics cover everything. A trace
that quietly reported window statistics would be worse than no trace, because it
would look authoritative.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serrin.chain import Chain, default_chain  # noqa: E402
from serrin.export import MappingConfig, build_render, trace_mapping  # noqa: E402
from serrin.ingest import ingest_csv  # noqa: E402
from serrin.rng import Rng  # noqa: E402
from serrin.stream import Stream  # noqa: E402
from serrin.trace import (  # noqa: E402
    DEFAULT_WINDOW,
    FORMAT,
    Trace,
    channel_stats,
    entropy_bits,
)

CSV = "t,cpu,mem,flat,spiky\n" + "\n".join(
    f"{i},{20 + (i * 7) % 60},{40 + (i * 3) % 40},7,{255 if i % 37 == 0 else 1}"
    for i in range(400)
)


def sample_csv() -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write(CSV)
    handle.close()
    return Path(handle.name)


class TestMeasurements(unittest.TestCase):
    def test_entropy_of_a_constant_is_zero(self):
        self.assertEqual(entropy_bits([7] * 100), 0.0)

    def test_entropy_of_a_uniform_byte_stream_is_eight_bits(self):
        self.assertAlmostEqual(entropy_bits(list(range(256))), 8.0, places=6)

    def test_entropy_of_a_coin_flip_is_one_bit(self):
        self.assertAlmostEqual(entropy_bits([0, 1] * 50), 1.0, places=6)

    def test_entropy_rises_with_disorder(self):
        rng = Rng(3)
        ordered = [i % 4 for i in range(400)]
        noisy = [rng.below(256) for _ in range(400)]
        self.assertLess(entropy_bits(ordered), entropy_bits(noisy))

    def test_stats_over_an_empty_channel_do_not_explode(self):
        stats = channel_stats([])
        self.assertEqual(stats["frames"], 0)
        self.assertEqual(stats["entropy"], 0.0)

    def test_flat_longest_finds_the_longest_run(self):
        stats = channel_stats([1, 2, 2, 2, 2, 3, 4, 4])
        self.assertEqual(stats["flat_longest"], 4)

    def test_change_rate_counts_transitions(self):
        self.assertEqual(channel_stats([5, 5, 5, 5])["change_rate"], 0.0)
        self.assertEqual(channel_stats([1, 2, 3, 4])["change_rate"], 1.0)


class TestTraceStructure(unittest.TestCase):
    def setUp(self):
        self.path = sample_csv()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_the_values_are_a_window_and_the_stats_are_not(self):
        # The claim the whole format depends on being trustworthy.
        recorder = Trace(window=10)
        stream = ingest_csv(self.path, columns=["cpu"], trace=recorder)
        channel = recorder.stages[0].channels[0]

        self.assertEqual(len(channel["values"]), 10)
        self.assertTrue(channel["truncated"])
        self.assertEqual(channel["stats"]["frames"], stream.length)
        self.assertGreater(stream.length, 10)
        # And the stats really are over everything, not over the window.
        self.assertEqual(channel["stats"]["min"], min(stream.data[0]))
        self.assertEqual(channel["stats"]["max"], max(stream.data[0]))

    def test_a_short_stream_is_not_marked_truncated(self):
        recorder = Trace(window=DEFAULT_WINDOW)
        ingest_csv(self.path, columns=["cpu"], limit=20, trace=recorder)
        self.assertFalse(recorder.stages[0].channels[0]["truncated"])

    def test_zero_window_records_stages_without_values(self):
        recorder = Trace(window=0)
        ingest_csv(self.path, columns=["cpu"], trace=recorder)
        # Still one stage, still full statistics, no value window.
        self.assertEqual(len(recorder.stages), 1)
        self.assertEqual(recorder.stages[0].channels[0]["values"], [])
        self.assertGreater(recorder.stages[0].channels[0]["stats"]["frames"], 0)

    def test_the_document_declares_its_format(self):
        recorder = Trace(window=4, label="probe")
        ingest_csv(self.path, columns=["cpu"], trace=recorder)
        document = recorder.to_json()
        self.assertEqual(document["format"], FORMAT)
        self.assertEqual(document["label"], "probe")
        self.assertEqual(document["window"], 4)

    def test_no_trace_means_no_cost(self):
        # The default path must not build any of this.
        stream = ingest_csv(self.path, columns=["cpu"])
        self.assertNotIn("conversions", stream.meta)


class TestConversionTable(unittest.TestCase):
    """The stage that answers "how does a cell become a number"."""

    def setUp(self):
        self.path = sample_csv()
        self.recorder = Trace(window=12)
        self.stream = ingest_csv(self.path, trace=self.recorder)
        self.detail = self.recorder.stages[0].detail

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_it_records_the_whole_chain_per_column(self):
        conversion = self.detail["conversions"][0]
        for key in ("cells", "parsed", "aggregated", "bytes", "range"):
            self.assertIn(key, conversion)
        self.assertEqual(len(conversion["cells"]), 12)

    def test_the_recorded_bytes_match_the_stream(self):
        for index, conversion in enumerate(self.detail["conversions"]):
            self.assertEqual(conversion["bytes"], self.stream.data[index][:12])
            self.assertEqual(conversion["name"], self.stream.names[index])

    def test_the_cells_are_the_raw_text(self):
        conversion = self.detail["conversions"][0]
        self.assertIsInstance(conversion["cells"][0], str)
        self.assertAlmostEqual(float(conversion["cells"][0]), conversion["parsed"][0])

    def test_the_normalization_range_is_reported(self):
        # The lossy step: magnitude is discarded, shape kept. Being able to see
        # the range is what makes that legible rather than mysterious.
        conversion = self.detail["conversions"][0]
        self.assertIn("low", conversion["range"])
        self.assertIn("high", conversion["range"])
        self.assertLess(conversion["range"]["low"], conversion["range"]["high"])

    def test_dropped_columns_are_named(self):
        self.assertIn("t", self.detail["columns_dropped"])  # monotonic
        self.assertIn("flat", self.detail["columns_dropped"])  # constant
        self.assertNotIn("t", self.detail["columns_chosen"])

    def test_aggregation_is_visible_when_it_happens(self):
        recorder = Trace(window=8)
        ingest_csv(self.path, columns=["cpu"], granularity=4, trace=recorder)
        conversion = recorder.stages[0].detail["conversions"][0]
        # Four rows collapse into one frame, so the two columns differ in length.
        self.assertNotEqual(conversion["parsed"][:8], conversion["aggregated"][:8])


class TestPedalStages(unittest.TestCase):
    def setUp(self):
        self.path = sample_csv()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def _trace_chain(self, chain: Chain, **ingest) -> Trace:
        recorder = Trace(window=64)
        stream = ingest_csv(self.path, trace=recorder, **ingest)
        chain.apply(stream, seed=99, recorder=recorder)
        return recorder

    def test_one_stage_per_applied_pedal(self):
        chain = default_chain()
        recorder = self._trace_chain(chain)
        pedals = [s for s in recorder.stages if s.kind == "pedal"]
        self.assertEqual(len(pedals), len(chain.slots))
        self.assertEqual([s.name for s in pedals], [slot.pedal for slot in chain.slots])

    def test_a_disabled_pedal_gets_no_stage(self):
        chain = Chain.from_json(
            {
                "name": "half",
                "chain": [
                    {"pedal": "delta", "params": {"order": 1}},
                    {"pedal": "bit_reverse", "params": {}, "enabled": False},
                ],
            }
        )
        recorder = self._trace_chain(chain)
        self.assertEqual([s.name for s in recorder.stages if s.kind == "pedal"], ["delta"])

    def test_params_are_recorded_as_applied(self):
        recorder = self._trace_chain(default_chain())
        bitcrush = next(s for s in recorder.stages if s.name == "bitcrush")
        self.assertEqual(bitcrush.params["target_bits"], 4)

    def test_changed_fraction_is_measured(self):
        recorder = self._trace_chain(default_chain())
        for stage in recorder.stages:
            if stage.kind != "pedal":
                continue
            self.assertIn("changed_fraction", stage.detail)
            self.assertGreaterEqual(stage.detail["changed_fraction"], 0.0)
            self.assertLessEqual(stage.detail["changed_fraction"], 1.0)

    def test_a_bypass_pedal_reports_almost_nothing_moved(self):
        chain = Chain.from_json(
            {
                "name": "bypass",
                "chain": [{"pedal": "cross_mix", "params": {"depth": 0.0}}],
            }
        )
        recorder = self._trace_chain(chain)
        stage = recorder.stages[-1]
        self.assertEqual(stage.detail["changed_fraction"], 0.0)

    def test_bitcrush_lowers_entropy(self):
        # The headline claim of the whole trace: entropy moves the way each
        # pedal's description says it should. bitcrush throws bits away, so it
        # must lose entropy -- and collapse the value range while doing it.
        chain = Chain.from_json(
            {"name": "c", "chain": [{"pedal": "bitcrush", "params": {"target_bits": 2}}]}
        )
        recorder = self._trace_chain(chain)
        stage = recorder.stages[-1]
        self.assertLess(stage.detail["entropy_delta"], -1.0)
        self.assertLessEqual(max(c["stats"]["unique"] for c in stage.channels), 4)

    def test_an_lfsr_mask_raises_entropy(self):
        chain = Chain.from_json(
            {
                "name": "c",
                "chain": [{"pedal": "xor_mask", "params": {"mask_source": "lfsr"}}],
            }
        )
        recorder = self._trace_chain(chain)
        self.assertGreater(recorder.stages[-1].detail["entropy_delta"], 0.0)

    def test_the_lfsr_period_is_carried_into_the_stage(self):
        chain = Chain.from_json(
            {
                "name": "c",
                "chain": [{"pedal": "xor_mask", "params": {"mask_source": "lfsr", "taps": [3, 1]}}],
            }
        )
        recorder = self._trace_chain(chain)
        self.assertIn("lfsr", recorder.stages[-1].detail)
        self.assertIn("periods", recorder.stages[-1].detail["lfsr"])

    def test_a_chain_applied_to_an_untraced_stream_still_gets_a_baseline(self):
        # Otherwise the first pedal has nothing to be a difference from.
        recorder = Trace(window=16)
        stream = Stream(names=["a"], data=[[i % 256 for i in range(64)]])
        default_chain().apply(stream, seed=1, recorder=recorder)
        self.assertEqual(recorder.stages[0].kind, "ingest")
        self.assertIn("changed_fraction", recorder.stages[1].detail)

    def test_a_length_changing_pedal_is_flagged(self):
        chain = Chain.from_json(
            {
                "name": "c",
                "chain": [
                    {
                        "pedal": "stutter_repeat",
                        "params": {"threshold": 200, "repeats": 3, "block": 4},
                    }
                ],
            }
        )
        recorder = self._trace_chain(chain, columns=["cpu"])
        self.assertIn("stall", recorder.stages[-1].note)


class TestMappingStage(unittest.TestCase):
    def setUp(self):
        self.path = sample_csv()
        self.recorder = Trace(window=32)
        stream = ingest_csv(self.path, columns=["cpu", "mem", "spiky"], trace=self.recorder)
        chain = default_chain()
        self.transformed = chain.apply(stream, seed=5, recorder=self.recorder)
        self.rendered = build_render(
            self.transformed, chain=chain, config=MappingConfig(quantize_to="pentatonic_minor")
        )
        trace_mapping(self.transformed, self.rendered, self.recorder, examples=5)
        self.stage = self.recorder.stages[-1]

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_the_fork_is_shown_as_worked_examples(self):
        self.assertEqual(self.stage.kind, "mapping")
        examples = self.stage.detail["examples"]
        self.assertEqual(len(examples), 5)
        for row in examples:
            self.assertEqual(len(row["voices"]), self.transformed.n_voices)

    def test_each_example_ties_a_byte_to_both_readings(self):
        row = self.stage.detail["examples"][1]
        for voice in row["voices"]:
            for key in ("byte", "freq", "amp", "gate", "x", "y", "density", "glyph"):
                self.assertIn(key, voice)
            self.assertGreater(voice["freq"], 0)

    def test_the_examples_match_the_exported_documents(self):
        # A trace that disagreed with the render would be actively misleading.
        row = self.stage.detail["examples"][2]
        frame = row["frame"]
        for index, voice in enumerate(row["voices"]):
            self.assertEqual(voice["byte"], self.transformed.data[index][frame])
            self.assertEqual(voice["freq"], self.rendered.audio["voices"][index]["freq"][frame])
            self.assertEqual(voice["y"], self.rendered.visual["voices"][index]["y"][frame])

    def test_the_scale_is_carried(self):
        self.assertEqual(self.stage.detail["scale"]["name"], "pentatonic_minor")

    def test_it_says_what_each_side_reads(self):
        self.assertIn("absolute value", self.stage.detail["audio_reads"])
        self.assertIn("rotated channel", self.stage.detail["visual_reads"])

    def test_describe_renders_without_exploding(self):
        text = self.recorder.describe()
        self.assertIn("mapping", text)
        self.assertIn("H=", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
