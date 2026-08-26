"""Tests for the piece's declared scale -- what anything playing along reads.

Its own file because the question it answers is narrow and specific: *what key
is this piece in?* A piece can pick one up in two unrelated places, or in
neither, and the browser cannot guess which. Getting it wrong is silent -- the
keyboard would simply play slightly wrong notes over the noise.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serrin import pedals  # noqa: E402
from serrin.chain import Chain, Slot  # noqa: E402
from serrin.export import MappingConfig, build_piece, effective_scale  # noqa: E402
from serrin.rng import Rng  # noqa: E402
from serrin.scales import DEFAULT_SCALE, resolve  # noqa: E402
from serrin.stream import Stream  # noqa: E402


def make_stream(voices: int = 3, length: int = 64) -> Stream:
    rng = Rng(11)
    return Stream(
        names=[f"v{i}" for i in range(voices)],
        data=[[rng.below(256) for _ in range(length)] for _ in range(voices)],
    )


class TestEffectiveScale(unittest.TestCase):
    def test_output_mapping_is_the_source_of_truth(self):
        stream = make_stream()
        scale = effective_scale(stream, MappingConfig(quantize_to="dorian"))
        self.assertEqual(scale["name"], "dorian")
        self.assertEqual(scale["source"], "mapping")
        self.assertEqual(scale["offsets"], resolve("dorian")[0])

    def test_mod_reduce_is_picked_up_from_the_chain(self):
        stream = make_stream()
        quantized = pedals.get("mod_reduce")(stream, {"scale": "blues"}, Rng(1))
        scale = effective_scale(quantized, MappingConfig())
        self.assertEqual(scale["name"], "blues")
        self.assertEqual(scale["source"], "mod_reduce")

    def test_the_output_mapping_wins_when_both_are_set(self):
        # It is what the exported frequencies actually obey, so it is the only
        # answer that matches what you would hear.
        stream = make_stream()
        quantized = pedals.get("mod_reduce")(stream, {"scale": "blues"}, Rng(1))
        scale = effective_scale(quantized, MappingConfig(quantize_to="lydian"))
        self.assertEqual(scale["name"], "lydian")
        self.assertEqual(scale["source"], "mapping")

    def test_an_unquantized_piece_says_so(self):
        # corrupted_dump is this case: full chromaticism is a deliberate option,
        # but something playing along still needs notes to choose from.
        scale = effective_scale(make_stream(), MappingConfig())
        self.assertEqual(scale["source"], "default")
        self.assertEqual(scale["name"], DEFAULT_SCALE)

    def test_the_root_is_the_mapping_floor(self):
        # The audio fork measures its offsets from note_low, so that is where
        # the scale is rooted -- a pitch class alone would lose the octave.
        scale = effective_scale(make_stream(), MappingConfig(note_low=40, note_high=76))
        self.assertEqual(scale["root"], 40)
        self.assertEqual(scale["note_low"], 40)
        self.assertEqual(scale["note_high"], 76)

    def test_offsets_are_within_one_octave_and_sorted(self):
        for name in ("ionian", "harmonic_minor", "blues", "whole_tone", "chromatic"):
            with self.subTest(scale=name):
                scale = effective_scale(make_stream(), MappingConfig(quantize_to=name))
                self.assertEqual(scale["offsets"], sorted(scale["offsets"]))
                self.assertTrue(all(0 <= o < scale["span"] for o in scale["offsets"]))

    def test_a_custom_interval_spec_resolves(self):
        scale = effective_scale(
            make_stream(), MappingConfig(quantize_to="1, 1/2, 1, 1, 1/2, 1 1/2, 1/2")
        )
        self.assertEqual(scale["offsets"], resolve("harmonic_minor")[0])

    def test_the_scale_reaches_the_export(self):
        piece = build_piece(make_stream(), config=MappingConfig(quantize_to="pentatonic_minor"))
        self.assertIn("scale", piece.meta)
        self.assertEqual(piece.meta["scale"]["name"], "pentatonic_minor")
        # Both documents carry it, so either can drive an instrument.
        self.assertEqual(
            piece.audio_document()["meta"]["scale"],
            piece.visual_document()["meta"]["scale"],
        )

    def test_every_exported_pitch_is_in_the_declared_scale(self):
        # The property that actually matters: if the keyboard trusts meta.scale,
        # the piece had better be in it.
        import math

        stream = Chain(slots=[Slot("delta", {"order": 1})]).apply(make_stream(), seed=5)
        config = MappingConfig(quantize_to="pentatonic_minor", note_low=36, note_high=72)
        piece = build_piece(stream, config=config)
        offsets = set(piece.meta["scale"]["offsets"])
        root = piece.meta["scale"]["root"]
        for voice in piece.audio["voices"]:
            for freq in voice["freq"]:
                midi = round(69 + 12 * math.log2(freq / 440.0))
                self.assertIn((midi - root) % 12, offsets, f"{freq} Hz is off-scale")


if __name__ == "__main__":
    unittest.main(verbosity=2)
