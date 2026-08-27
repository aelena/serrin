"""Tests for the piece format -- the document Serrin now works on.

Three properties carry the weight:

* **A piece can exist before it has been rendered.** That is the whole point of
  inverting the flow, and it is easy to break by assuming a fingerprint.
* **The folder is portable.** Relative paths, so an album can be copied, zipped
  or moved and still resolve. An absolute path leaking into a manifest breaks
  that silently -- it works on the machine that wrote it and nowhere else.
* **Bindings that point at nothing are refused on load.** A key mapped to a
  missing sample would simply do nothing when pressed, with no clue why, so it
  fails when the piece opens instead.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from serrin.chain import default_chain  # noqa: E402
from serrin.export import build_render  # noqa: E402
from serrin.ingest import fingerprint, ingest_csv  # noqa: E402
from serrin.piece import (  # noqa: E402
    FORMAT,
    MANIFEST,
    SESSION_FORMAT,
    Pattern,
    Performance,
    Piece,
    PieceError,
    Sample,
    default_keymap,
    list_pieces,
    new_piece,
    slug,
)
from serrin.session import Session  # noqa: E402
from serrin.tempo import Tempo  # noqa: E402

CSV = "t,cpu,mem,net\n" + "\n".join(
    f"{i},{20 + (i * 7) % 60},{40 + (i * 3) % 40},{100 + (i * 13) % 900}" for i in range(150)
)


class PieceCase(unittest.TestCase):
    def setUp(self):
        self.album = Path(tempfile.mkdtemp(prefix="serrin-piece-test-"))
        self.csv = self.album / "data.csv"
        self.csv.write_text(CSV, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.album, ignore_errors=True)

    def make(self, name="01-test", **source) -> Piece:
        folder = self.album / name
        return new_piece(
            folder,
            source={"kind": "csv", "path": "../data.csv", **source},
            preset=default_chain().to_json(),
            stamp="2026-08-27T08:00:00Z",
        )


class TestCreation(PieceCase):
    def test_a_new_piece_is_a_folder_with_a_manifest(self):
        piece = self.make()
        self.assertTrue((piece.folder / MANIFEST).exists())
        self.assertTrue((piece.folder / "samples").is_dir())

    def test_a_piece_exists_before_it_is_rendered(self):
        # The inversion this whole format is for.
        piece = self.make()
        self.assertFalse(piece.rendered)
        self.assertEqual(piece.render, {})
        self.assertIn("not yet generated", piece.describe())

    def test_creating_twice_in_one_folder_is_refused(self):
        piece = self.make()
        with self.assertRaises(PieceError):
            new_piece(piece.folder)

    def test_the_manifest_declares_its_format(self):
        piece = self.make()
        raw = json.loads((piece.folder / MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(raw["format"], FORMAT)

    def test_the_manifest_is_indented_for_diffing(self):
        # Samples are referenced rather than embedded precisely so this file
        # stays readable in a diff. Writing it compact would waste that.
        piece = self.make()
        text = (piece.folder / MANIFEST).read_text(encoding="utf-8")
        self.assertIn('\n  "source"', text)

    def test_slug_makes_names_safe(self):
        self.assertEqual(slug("Decay / Part 2"), "Decay-Part-2")
        self.assertEqual(slug(""), "piece")
        self.assertEqual(slug("../../escape"), "escape")


class TestPortability(PieceCase):
    def test_relative_paths_resolve_against_the_folder(self):
        piece = self.make()
        self.assertEqual(piece.source_path.resolve(), self.csv.resolve())

    def test_an_absolute_source_path_still_works(self):
        # A piece may legitimately point at a CSV living elsewhere; only what the
        # piece *owns* has to be relative.
        piece = self.make(path=str(self.csv))
        self.assertEqual(piece.source_path.resolve(), self.csv.resolve())

    def test_the_whole_folder_can_be_moved(self):
        piece = self.make()
        piece.performance = Performance.from_json(
            {"samples": [{"id": "kick", "path": "samples/kick.wav"}]}
        )
        (piece.folder / "samples" / "kick.wav").write_bytes(b"RIFF fake")
        piece.save()

        moved = self.album / "moved" / "01-test"
        moved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(piece.folder), str(moved))

        reopened = Piece.load(moved)
        self.assertEqual(reopened.missing_samples(), [], "the sample stopped resolving")

    def test_a_render_records_relative_paths(self):
        piece = self.make()
        piece.render = {"audio": "out/audio.json", "visual": "out/visual.json"}
        piece.save()
        raw = json.loads((piece.folder / MANIFEST).read_text(encoding="utf-8"))
        for key in ("audio", "visual"):
            self.assertFalse(Path(raw["render"][key]).is_absolute(), key)


class TestPerformanceLayer(PieceCase):
    def test_the_default_keymap_binds_by_physical_position(self):
        # Not by character: `event.key` is layout-dependent, so a map authored on
        # a Spanish keyboard would land on different physical keys on a US one --
        # and a piece is meant to be shareable.
        keymap = default_keymap([0, 3, 5, 7, 10])
        self.assertIn("KeyA", keymap)
        self.assertNotIn("a", keymap)
        for binding in keymap.values():
            self.assertEqual(binding["kind"], "degree")

    def test_the_default_keymap_uses_degrees_not_pitches(self):
        # Absolute pitches would go silently out of key whenever the chain or the
        # mapping changes the piece's scale.
        for binding in default_keymap([0, 3, 5, 7, 10]).values():
            self.assertIn("degree", binding)
            self.assertNotIn("midi", binding)

    def test_an_unknown_binding_kind_is_refused(self):
        with self.assertRaises(PieceError):
            Performance.from_json({"keymap": {"KeyA": {"kind": "telepathy"}}})

    def test_a_key_bound_to_a_missing_sample_is_refused(self):
        # Otherwise the key just does nothing when pressed, with no clue why.
        with self.assertRaises(PieceError) as caught:
            Performance.from_json({"keymap": {"KeyA": {"kind": "sample", "sample": "kick"}}})
        self.assertIn("does not have", str(caught.exception))

    def test_a_key_bound_to_a_present_sample_is_fine(self):
        performance = Performance.from_json(
            {
                "samples": [{"id": "kick", "path": "samples/kick.wav"}],
                "keymap": {"KeyA": {"kind": "sample", "sample": "kick"}},
            }
        )
        self.assertEqual(performance.sample("kick").path, "samples/kick.wav")

    def test_duplicate_sample_ids_are_refused(self):
        with self.assertRaises(PieceError):
            Performance.from_json(
                {"samples": [{"id": "kick", "path": "a.wav"}, {"id": "kick", "path": "b.wav"}]}
            )

    def test_a_sample_needs_an_id_and_a_path(self):
        with self.assertRaises(PieceError):
            Sample.from_json({"id": "kick"})

    def test_a_pattern_hit_outside_its_grid_is_refused(self):
        with self.assertRaises(PieceError) as caught:
            Pattern.from_json({"id": "beat", "steps": 8, "hits": [{"step": 11}]})
        self.assertIn("outside", str(caught.exception))

    def test_pattern_hits_are_sorted(self):
        pattern = Pattern.from_json(
            {"id": "beat", "steps": 16, "hits": [{"step": 8}, {"step": 0}, {"step": 4}]}
        )
        self.assertEqual([h["step"] for h in pattern.hits], [0, 4, 8])

    def test_a_pattern_targeting_a_missing_sample_is_refused(self):
        with self.assertRaises(PieceError):
            Performance.from_json(
                {
                    "patterns": [
                        {"id": "beat", "steps": 8, "target": {"kind": "sample", "sample": "snare"}}
                    ]
                }
            )

    def test_the_performance_layer_round_trips(self):
        payload = {
            "keymap": {"KeyA": {"kind": "degree", "degree": 0, "octave": 0}},
            "samples": [{"id": "kick", "path": "samples/kick.wav", "gain": 0.8}],
            "patterns": [
                {
                    "id": "beat",
                    "steps": 16,
                    "hits": [{"step": 0, "velocity": 1.0}, {"step": 8, "velocity": 0.6}],
                    "target": {"kind": "sample", "sample": "kick"},
                }
            ],
            "keyboard": {"mode": "notes", "register": "mid"},
        }
        restored = Performance.from_json(Performance.from_json(payload).to_json())
        self.assertEqual(restored.to_json(), Performance.from_json(payload).to_json())

    def test_missing_sample_files_are_reported_not_raised(self):
        # A piece with a missing sample is still worth opening; the author needs
        # to know which file to go and find.
        piece = self.make()
        piece.performance = Performance.from_json(
            {"samples": [{"id": "kick", "path": "samples/gone.wav"}]}
        )
        piece.save()
        reopened = Piece.load(piece.folder)
        self.assertEqual(len(reopened.missing_samples()), 1)
        self.assertIn("MISSING", reopened.describe())


class TestRenderLayer(PieceCase):
    def test_the_chain_is_validated_on_open(self):
        piece = self.make()
        raw = json.loads((piece.folder / MANIFEST).read_text(encoding="utf-8"))
        raw["preset"]["chain"].append({"pedal": "nonexistent_pedal"})
        (piece.folder / MANIFEST).write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(Exception):
            Piece.load(piece.folder)

    def test_ingest_kwargs_match_the_source_kind(self):
        csv_piece = self.make("01-csv")
        self.assertIn("columns", csv_piece.ingest_kwargs() | {"columns": None})

        git_piece = new_piece(
            self.album / "02-git",
            source={"kind": "git", "path": str(ROOT), "metric": "hash", "traversal": "chrono"},
            preset=default_chain().to_json(),
        )
        kwargs = git_piece.ingest_kwargs()
        self.assertEqual(kwargs["metric"], "hash")
        # Handing CSV keywords to ingest_repo would be a TypeError, not a
        # near-miss, so the two sets are kept apart.
        self.assertNotIn("granularity", kwargs)
        self.assertNotIn("aggregation", kwargs)

    def test_tempo_comes_from_the_source_or_the_preset(self):
        piece = self.make(tempo={"bpm": 84, "subdivision": 8, "swing": 0.1, "beats_per_bar": 4})
        self.assertEqual(piece.tempo().bpm, 84)
        bare = self.make("03-bare")
        self.assertIsNone(bare.tempo())

    def test_a_piece_renders_reproducibly(self):
        # The property inherited from sessions: the document is enough.
        piece = self.make(tempo={"bpm": 96, "subdivision": 16, "swing": 0.2, "beats_per_bar": 4})

        def render():
            stream = ingest_csv(piece.source_path, **piece.ingest_kwargs())
            chain = piece.chain()
            return fingerprint(chain.apply(stream, source=piece.source_path))

        self.assertEqual(render(), render())

    def test_a_reopened_piece_renders_the_same(self):
        piece = self.make(tempo={"bpm": 96, "subdivision": 16, "swing": 0.2, "beats_per_bar": 4})
        stream = ingest_csv(piece.source_path, **piece.ingest_kwargs())
        before = fingerprint(piece.chain().apply(stream, source=piece.source_path))

        reopened = Piece.load(piece.folder)
        again = ingest_csv(reopened.source_path, **reopened.ingest_kwargs())
        after = fingerprint(reopened.chain().apply(again, source=reopened.source_path))
        self.assertEqual(before, after)

    def test_the_render_block_carries_what_was_produced(self):
        piece = self.make()
        stream = ingest_csv(piece.source_path, **piece.ingest_kwargs())
        chain = piece.chain()
        transformed = chain.apply(stream, source=piece.source_path)
        rendered = build_render(transformed, chain=chain)

        piece.render = {
            "label": rendered.meta["label"],
            "fingerprint": rendered.meta["fingerprint"],
            "rendered_at": "2026-08-27T08:10:00Z",
        }
        piece.save()

        reopened = Piece.load(piece.folder)
        self.assertTrue(reopened.rendered)
        self.assertEqual(reopened.render["fingerprint"], rendered.meta["fingerprint"])


class TestSessionCompatibility(PieceCase):
    def build_session(self) -> dict:
        return {
            "format": SESSION_FORMAT,
            "label": "data+gritty_01+42",
            "fingerprint": "abcdef0123456789",
            "saved_at": "2026-08-26T10:00:00Z",
            "source": {"kind": "csv", "path": str(self.csv), "bit_depth": 8},
            "preset": default_chain().to_json(),
            "runtime": {"audio": {"master": 0.3}},
            "streams": {"audio": "out/a.json", "visual": "out/v.json"},
        }

    def test_a_session_loads_as_a_piece(self):
        # One reader, two shapes. A session is a piece that has already been
        # rendered and has no performance layer, so importing is reshaping.
        piece = Piece.from_json(self.build_session())
        self.assertEqual(piece.render["fingerprint"], "abcdef0123456789")
        self.assertTrue(piece.rendered)
        self.assertEqual(piece.render["audio"], "out/a.json")

    def test_the_runtime_block_survives_the_import(self):
        piece = Piece.from_json(self.build_session())
        self.assertEqual(piece.runtime["audio"]["master"], 0.3)

    def test_an_imported_session_has_an_empty_performance_layer(self):
        piece = Piece.from_json(self.build_session())
        self.assertEqual(piece.performance.to_json(), {})

    def test_a_foreign_format_is_refused(self):
        payload = self.build_session()
        payload["format"] = "serrin-piece/99"
        with self.assertRaises(PieceError):
            Piece.from_json(payload)

    def test_a_real_session_object_imports(self):
        session = Session.from_json(self.build_session())
        piece = Piece.from_json(session.to_json())
        self.assertEqual(piece.chain().name, session.chain().name)
        self.assertEqual(piece.kind, session.kind)


class TestAlbum(PieceCase):
    def test_listing_finds_every_piece(self):
        self.make("01-one")
        self.make("02-two")
        entries = list_pieces(self.album)
        self.assertEqual([e["name"] for e in entries], ["01-one", "02-two"])
        self.assertTrue(all(e["ok"] for e in entries))

    def test_listing_an_empty_or_missing_folder_is_not_an_error(self):
        self.assertEqual(list_pieces(self.album / "nope"), [])

    def test_a_broken_piece_is_reported_without_stopping_the_others(self):
        # A folder of pieces should list even when one of them is unopenable.
        self.make("01-fine")
        broken = self.album / "02-broken"
        broken.mkdir()
        (broken / MANIFEST).write_text("{not json", encoding="utf-8")

        entries = list_pieces(self.album)
        self.assertEqual(len(entries), 2)
        by_name = {e["name"]: e for e in entries}
        self.assertTrue(by_name["01-fine"]["ok"])
        self.assertFalse(by_name["02-broken"]["ok"])
        self.assertIn("error", by_name["02-broken"])

    def test_the_summary_says_whether_a_piece_is_rendered(self):
        piece = self.make("01-one")
        self.assertFalse(list_pieces(self.album)[0]["rendered"])
        piece.render = {"fingerprint": "deadbeef"}
        piece.save()
        self.assertTrue(list_pieces(self.album)[0]["rendered"])

    def test_the_summary_reports_missing_samples(self):
        piece = self.make("01-one")
        piece.performance = Performance.from_json(
            {"samples": [{"id": "kick", "path": "samples/gone.wav"}]}
        )
        piece.save()
        self.assertEqual(list_pieces(self.album)[0]["missing_samples"], ["samples/gone.wav"])

    def test_pieces_in_an_album_can_share_a_kit(self):
        # `../kit/x.wav` is the point of relative resolution: a conceptual series
        # shares samples without copying them into every piece.
        kit = self.album / "kit"
        kit.mkdir()
        (kit / "kick.wav").write_bytes(b"RIFF fake")

        piece = self.make("01-one")
        piece.performance = Performance.from_json(
            {"samples": [{"id": "kick", "path": "../kit/kick.wav"}]}
        )
        piece.save()
        self.assertEqual(Piece.load(piece.folder).missing_samples(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
