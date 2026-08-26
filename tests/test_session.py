"""Tests for the session format.

The property that matters is at the bottom: **re-rendering from a session
reproduces the render byte for byte**. Everything above it is the validation
that makes that property trustworthy -- a session that half-loads, or that
silently drops a field, would produce a piece that is *nearly* the one you
saved, which is worse than an error.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serrin.chain import Chain, default_chain  # noqa: E402
from serrin.export import build_piece  # noqa: E402
from serrin.ingest import fingerprint, ingest_csv  # noqa: E402
from serrin.session import FORMAT, Session, SessionError, promote_to_preset  # noqa: E402
from serrin.tempo import Tempo  # noqa: E402

CSV = "t,cpu,mem,net\n" + "\n".join(
    f"{i},{20 + (i * 7) % 60},{40 + (i * 3) % 40},{100 + (i * 13) % 900}" for i in range(120)
)


def sample_csv() -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write(CSV)
    handle.close()
    return Path(handle.name)


def make_session(path: Path, **overrides) -> Session:
    payload = {
        "format": FORMAT,
        "label": "test+gritty_01+42",
        "fingerprint": "deadbeefdeadbeef",
        "saved_at": "2026-08-26T10:00:00.000Z",
        "source": {
            "path": str(path),
            "columns": ["cpu", "mem", "net"],
            "bit_depth": 8,
            "granularity": 2,
            "aggregation": "mean",
            "log_scale": False,
            "tempo": {"bpm": 96, "subdivision": 8, "swing": 0.25, "beats_per_bar": 4},
        },
        "preset": default_chain().to_json(),
        "runtime": {
            "audio": {"master": 0.31, "mutes": [2, 5]},
            "visual": {"invert": True},
            "keyboard": {"enabled": True, "register": "treble"},
        },
        "streams": {"audio": "out/a.json", "visual": "out/v.json"},
    }
    payload.update(overrides)
    return Session.from_json(payload)


class TestSessionValidation(unittest.TestCase):
    def setUp(self):
        self.path = sample_csv()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_loads_a_well_formed_session(self):
        session = make_session(self.path)
        self.assertEqual(session.path, str(self.path))
        self.assertEqual(session.chain().name, "gritty_01")

    def test_rejects_a_foreign_format(self):
        # Refusing beats guessing: a future format would half-apply and leave
        # the author wondering which fields were ignored.
        with self.assertRaises(SessionError):
            make_session(self.path, format="serrin-session/99")

    def test_a_missing_format_is_tolerated(self):
        # Hand-written sessions are a legitimate thing to do.
        payload = make_session(self.path).to_json()
        del payload["format"]
        self.assertEqual(Session.from_json(payload).path, str(self.path))

    def test_rejects_a_session_with_no_source(self):
        with self.assertRaises(SessionError):
            make_session(self.path, source={})

    def test_validates_the_chain_on_load_not_at_render_time(self):
        with self.assertRaises(Exception):
            make_session(self.path, preset={"chain": [{"pedal": "nonexistent_pedal"}]})

    def test_ingest_kwargs_only_carries_what_was_recorded(self):
        session = make_session(self.path)
        kwargs = session.ingest_kwargs()
        self.assertEqual(kwargs["granularity"], 2)
        self.assertEqual(kwargs["aggregation"], "mean")
        self.assertIsInstance(kwargs["tempo"], Tempo)
        # log_scale was False and limit absent: both left to ingestion's defaults
        # rather than pinned, so a later default change is not frozen in.
        self.assertNotIn("limit", kwargs)

    def test_tempo_falls_back_to_the_preset_ingest_hints(self):
        session = make_session(
            self.path,
            source={"path": str(self.path)},
            preset={**default_chain().to_json(), "ingest": {"tempo": {"bpm": 140, "subdivision": 16}}},
        )
        self.assertEqual(session.tempo().bpm, 140)

    def test_no_tempo_anywhere_is_not_an_error(self):
        session = make_session(self.path, source={"path": str(self.path)})
        self.assertIsNone(session.tempo())

    def test_round_trip_preserves_the_opaque_runtime_block(self):
        # Python must not interpret the runtime layer, only carry it: the browser
        # owns that half and would lose settings to a well-meaning normalization.
        original = make_session(self.path)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "s.json"
            original.save(target)
            restored = Session.load(target)
        self.assertEqual(restored.runtime, original.runtime)
        self.assertEqual(restored.saved_at, original.saved_at)
        self.assertEqual(restored.streams, original.streams)

    def test_describe_mentions_the_essentials(self):
        text = make_session(self.path).describe()
        for expected in ("source", "chain", "tempo", "runtime"):
            self.assertIn(expected, text)


class TestPromoteToPreset(unittest.TestCase):
    def setUp(self):
        self.path = sample_csv()
        self.session = make_session(self.path)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_the_result_is_a_loadable_preset(self):
        preset = promote_to_preset(self.session, "frozen")
        chain = Chain.from_json(preset)
        self.assertEqual(chain.name, "frozen")
        self.assertEqual(len(chain.slots), len(self.session.chain().slots))

    def test_ingestion_choices_are_folded_in(self):
        # So `render -c frozen.json` on the same CSV reproduces the render
        # without needing the session file as well.
        preset = promote_to_preset(self.session)
        self.assertEqual(preset["ingest"]["granularity"], 2)
        self.assertEqual(preset["ingest"]["columns"], ["cpu", "mem", "net"])
        self.assertEqual(preset["ingest"]["tempo"]["bpm"], 96)
        self.assertEqual(preset["ingest"]["tempo"]["swing"], 0.25)

    def test_the_runtime_layer_is_dropped(self):
        # Not a bug -- none of it has an offline meaning. The CLI says so out loud.
        preset = promote_to_preset(self.session)
        flattened = json.dumps(preset)
        self.assertNotIn("mutes", flattened)
        self.assertNotIn("master", flattened)
        self.assertNotIn("register", flattened)

    def test_provenance_is_recorded(self):
        self.assertIn("session", promote_to_preset(self.session)["notes"])

    def test_a_name_is_always_present(self):
        session = make_session(self.path, label="")
        self.assertTrue(promote_to_preset(session)["name"])


class TestReRenderReproduces(unittest.TestCase):
    """The property the whole format exists for."""

    def setUp(self):
        self.path = sample_csv()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def _render(self, **ingest_kwargs) -> tuple[str, dict]:
        stream = ingest_csv(self.path, **ingest_kwargs)
        chain = default_chain()
        transformed = chain.apply(stream, source=self.path)
        piece = build_piece(transformed, chain=chain)
        return fingerprint(transformed), piece.meta

    def test_a_session_reproduces_its_render_exactly(self):
        first, meta = self._render(
            columns=["cpu", "mem", "net"], granularity=2, tempo=Tempo(96, 8, swing=0.25)
        )

        session = make_session(self.path, fingerprint=first)
        second, meta_again = self._render(**session.ingest_kwargs())

        self.assertEqual(first, second, "re-rendering from a session drifted")
        self.assertEqual(meta["seed"], meta_again["seed"])
        self.assertEqual(meta["tempo"], meta_again["tempo"])
        self.assertEqual(meta["scale"], meta_again["scale"])

    def test_a_frozen_preset_reproduces_the_same_render(self):
        session = make_session(self.path)
        preset = promote_to_preset(session, "frozen")
        chain = Chain.from_json(preset)

        from_session = ingest_csv(self.path, **session.ingest_kwargs())
        from_preset = ingest_csv(
            self.path,
            columns=preset["ingest"]["columns"],
            granularity=preset["ingest"]["granularity"],
            aggregation=preset["ingest"]["aggregation"],
            tempo=Tempo.parse(preset["ingest"]["tempo"]),
        )
        self.assertEqual(
            fingerprint(chain.apply(from_session, source=self.path)),
            fingerprint(chain.apply(from_preset, source=self.path)),
        )

    def test_tempo_is_a_render_input_when_an_lfo_is_measured_in_hertz(self):
        """Tempo is not decoration, and how much it matters depends on the chain.

        A hertz LFO is defined against wall-clock time, so it is resolved into
        *frames* using the stream rate -- which the tempo sets. Change the BPM
        and a 0.1 Hz sweep covers a different number of frames, so the pedal
        writes different values. That is the whole difference between the two
        LFO units, and it means a session must record the tempo to be able to
        reproduce a render at all.
        """
        hertz = Chain.from_json(
            {
                "name": "hz",
                "chain": [{"pedal": "caesar", "params": {"shift": 5, "shift_lfo": "sine:0.1hz"}}],
            }
        )
        beats = Chain.from_json(
            {
                "name": "beats",
                "chain": [{"pedal": "caesar", "params": {"shift": 5, "shift_lfo": "sine:4beats"}}],
            }
        )

        def render(chain, bpm):
            stream = ingest_csv(self.path, columns=["cpu"], tempo=Tempo(bpm, 16))
            return fingerprint(chain.apply(stream, source=self.path))

        self.assertNotEqual(
            render(hertz, 60), render(hertz, 180), "a hertz LFO ignored the tempo"
        )
        # A beat-locked LFO covers the same frames at any tempo, so the samples
        # are identical and only their spacing changes.
        self.assertEqual(
            render(beats, 60), render(beats, 180), "a beat LFO tracked wall-clock time"
        )

    def test_tempo_changes_the_duration_either_way(self):
        slow = ingest_csv(self.path, columns=["cpu"], tempo=Tempo(60, 16))
        fast = ingest_csv(self.path, columns=["cpu"], tempo=Tempo(180, 16))
        self.assertEqual(slow.length, fast.length, "tempo changed the frame count")
        self.assertNotEqual(slow.duration, fast.duration)


if __name__ == "__main__":
    unittest.main(verbosity=2)
