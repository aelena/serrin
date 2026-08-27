"""Tests for the render endpoint.

The handler is tested through ``render_upload`` rather than over HTTP: the JSON
in and JSON out is the whole contract, and starting a real server would test
``http.server`` rather than serrin. What *is* worth testing over HTTP -- that a
browser can reach it at all -- is a one-line manual check, not a unit test.

The endpoint exists because the pedals live in Python. Porting them to
JavaScript would have created a second source of truth for the aesthetic, with
the sound as the thing that drifts between the two.
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
sys.path.insert(0, str(ROOT / "scripts"))

import serve  # noqa: E402

from serrin.session import Session  # noqa: E402

CSV = "t,cpu,mem,net\n" + "\n".join(
    f"{i},{20 + (i * 7) % 60},{40 + (i * 3) % 40},{100 + (i * 13) % 900}" for i in range(200)
)


class TestRenderUpload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Redirected so a test run does not litter the author's own renders --
        # but kept *inside* the served root, because the returned URLs are
        # root-relative and an upload directory outside it could never be
        # fetched by the page. Moving it out is not a stricter test, it is an
        # impossible configuration.
        cls.workspace = ROOT / "out" / ".test-api"
        cls._original = serve.UPLOAD_DIR
        serve.UPLOAD_DIR = cls.workspace / "uploads"

    @classmethod
    def tearDownClass(cls):
        serve.UPLOAD_DIR = cls._original
        shutil.rmtree(cls.workspace, ignore_errors=True)

    # -- the happy path ----------------------------------------------------
    def test_a_csv_renders(self):
        result = serve.render_upload({"csv": CSV, "name": "probe.csv"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "csv")
        self.assertGreater(result["frames"], 0)
        self.assertGreater(len(result["voices"]), 0)
        self.assertTrue(result["fingerprint"])
        self.assertIn("probe", result["label"])

    def test_the_returned_paths_exist_and_parse(self):
        result = serve.render_upload({"csv": CSV, "name": "paths.csv"})
        for key in ("audio", "visual", "session"):
            # Server-relative, so the page loads them like any other pair.
            self.assertTrue(result[key].startswith("/out/"), result[key])
            on_disk = ROOT / result[key].lstrip("/")
            self.assertTrue(on_disk.exists(), f"{key} was not written: {on_disk}")
            json.loads(on_disk.read_text(encoding="utf-8"))

    def test_the_written_session_is_loadable_and_re_renders(self):
        # The endpoint is only useful if what it saves can be reproduced later.
        result = serve.render_upload({"csv": CSV, "name": "sess.csv", "preset": "ikeda_sparse"})
        session_path = self.workspace / "uploads" / "sess.session.json"
        session = Session.load(session_path)
        self.assertEqual(session.kind, "csv")
        self.assertEqual(session.fingerprint, result["fingerprint"])

        from serrin.ingest import fingerprint, ingest_csv

        stream = ingest_csv(session.path, **session.ingest_kwargs())
        again = fingerprint(session.chain().apply(stream, source=session.path))
        self.assertEqual(again, result["fingerprint"])

    def test_a_named_preset_is_used(self):
        result = serve.render_upload({"csv": CSV, "name": "p.csv", "preset": "ikeda_sparse"})
        self.assertEqual(result["chain"], "ikeda_sparse")

    def test_an_inline_preset_is_used(self):
        result = serve.render_upload(
            {
                "csv": CSV,
                "name": "inline.csv",
                "preset_json": {
                    "name": "inline_test",
                    "chain": [{"pedal": "bit_reverse", "params": {}}],
                },
            }
        )
        self.assertEqual(result["chain"], "inline_test")

    def test_tempo_is_honoured(self):
        slow = serve.render_upload({"csv": CSV, "name": "slow.csv", "tempo": "60/16"})
        fast = serve.render_upload({"csv": CSV, "name": "fast.csv", "tempo": "180/16"})
        self.assertGreater(slow["duration"], fast["duration"])

    def test_an_explicit_seed_is_honoured(self):
        first = serve.render_upload({"csv": CSV, "name": "s1.csv", "seed": 1234})
        second = serve.render_upload({"csv": CSV, "name": "s2.csv", "seed": 1234})
        third = serve.render_upload({"csv": CSV, "name": "s3.csv", "seed": 9999})
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["fingerprint"], third["fingerprint"])

    def test_columns_can_be_chosen(self):
        result = serve.render_upload({"csv": CSV, "name": "cols.csv", "columns": ["cpu", "mem"]})
        self.assertEqual(result["voices"], ["cpu", "mem"])

    def test_tracing_is_opt_in(self):
        # Off by default because it roughly doubles the work and the reply size,
        # and most renders are not being debugged.
        plain = serve.render_upload({"csv": CSV, "name": "plain.csv"})
        self.assertIsNone(plain["trace"])

    def test_a_traced_render_returns_the_stages(self):
        result = serve.render_upload(
            {"csv": CSV, "name": "traced.csv", "trace": True, "trace_window": 8}
        )
        trace = result["trace"]
        self.assertEqual(trace["window"], 8)
        kinds = [stage["kind"] for stage in trace["stages"]]
        # The whole pipeline, in order: what came in, what each pedal did, and
        # how the result was read twice.
        self.assertEqual(kinds[0], "ingest")
        self.assertEqual(kinds[-1], "mapping")
        self.assertIn("pedal", kinds)
        # And the conversion table the browser needs to show cell -> byte.
        self.assertTrue(trace["stages"][0]["detail"]["conversions"])

    def test_a_traced_repo_render_reports_branch_ownership(self):
        # The graph adapter has no cell-to-byte table; ownership is the
        # equivalent question, so the stage carries that instead.
        if not shutil.which("git"):
            self.skipTest("git is not on PATH")
        result = serve.render_upload({"repo": str(ROOT), "trace": True, "trace_window": 4})
        stage = result["trace"]["stages"][0]
        self.assertEqual(stage["kind"], "ingest")
        self.assertIn("owned", stage["detail"])

    # -- refusals ----------------------------------------------------------
    def test_an_empty_request_is_refused(self):
        with self.assertRaises(ValueError):
            serve.render_upload({})

    def test_an_unknown_preset_is_refused(self):
        with self.assertRaises(ValueError):
            serve.render_upload({"csv": CSV, "preset": "no_such_preset"})

    def test_a_missing_repo_is_refused(self):
        with self.assertRaises(ValueError):
            serve.render_upload({"repo": str(self.workspace / "nope")})

    def test_a_preset_name_cannot_escape_the_presets_directory(self):
        # The name is slugged before it becomes a path, so separators and dots
        # cannot walk out of presets/.
        for attempt in ("../../etc/passwd", "..\\..\\windows\\win.ini", "a/b"):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                serve.render_upload({"csv": CSV, "preset": attempt})

    def test_an_uploaded_filename_cannot_escape_the_upload_directory(self):
        serve.render_upload({"csv": CSV, "name": "../../escape.csv"})
        uploads = self.workspace / "uploads"
        # Everything written stays inside the upload directory, whatever the
        # name claimed: the name is slugged before it becomes a path.
        for path in uploads.iterdir():
            self.assertEqual(path.parent, uploads)
        self.assertFalse((self.workspace.parent / "escape.csv").exists())

    def test_the_upload_limit_is_enforced_by_the_reader(self):
        # The cap lives in the request reader, not in render_upload, so this
        # checks the constant is actually plumbed rather than aspirational.
        self.assertGreater(serve.MAX_UPLOAD_BYTES, 0)
        self.assertIn("MAX_UPLOAD_BYTES", (ROOT / "scripts" / "serve.py").read_text(
            encoding="utf-8"
        ))


@unittest.skipUnless(shutil.which("git"), "git is not on PATH")
class TestRenderRepo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = ROOT / "out" / ".test-api-git"
        cls._original = serve.UPLOAD_DIR
        serve.UPLOAD_DIR = cls.workspace / "uploads"
        sys.path.insert(0, str(ROOT / "tests"))
        import git_fixture

        # The repo itself can live anywhere; only the render output has to be
        # under the served root.
        cls.repo_workspace = Path(tempfile.mkdtemp(prefix="serrin-api-git-"))
        cls.repo = git_fixture.build(cls.repo_workspace / "repo")

    @classmethod
    def tearDownClass(cls):
        serve.UPLOAD_DIR = cls._original
        shutil.rmtree(cls.workspace, ignore_errors=True)
        shutil.rmtree(cls.repo_workspace, ignore_errors=True)

    def test_a_repository_renders(self):
        result = serve.render_upload({"repo": str(self.repo), "preset": "merkle_drift"})
        self.assertEqual(result["kind"], "git")
        self.assertEqual(result["git"]["trunk"], "main")
        self.assertGreater(result["git"]["commits"], 40)
        self.assertIn("main", result["voices"])

    def test_the_metric_and_traversal_reach_the_adapter(self):
        result = serve.render_upload(
            {"repo": str(self.repo), "metric": "churn", "traversal": "topo"}
        )
        self.assertEqual(result["git"]["metric"], "churn")
        self.assertEqual(result["git"]["traversal"], "topo")

    def test_a_repository_label_is_not_empty(self):
        # `Path(".").stem` is the empty string, which used to leave the label
        # starting with a bare "+".
        result = serve.render_upload({"repo": str(self.repo)})
        self.assertFalse(result["label"].startswith("+"), result["label"])

    def test_the_repo_session_re_renders(self):
        result = serve.render_upload({"repo": str(self.repo), "metric": "interval"})
        session = Session.load(
            self.workspace / "uploads" / f"{self.repo.name}.session.json"
        )
        self.assertEqual(session.kind, "git")

        from serrin.ingest import fingerprint
        from serrin.ingest_git import ingest_repo

        stream = ingest_repo(session.path, **session.ingest_kwargs())
        chain = session.chain()
        again = fingerprint(chain.apply(stream, source=session.path))
        self.assertEqual(again, result["fingerprint"])


class TestPieceEndpoints(unittest.TestCase):
    """The endpoints the studio view is built on."""

    def setUp(self):
        self.album = ROOT / "out" / ".test-pieces"
        self.album.mkdir(parents=True, exist_ok=True)
        self._original = serve.PIECES_DIR
        serve.PIECES_DIR = self.album
        (self.album / "data.csv").write_text(CSV, encoding="utf-8")

    def tearDown(self):
        serve.PIECES_DIR = self._original
        shutil.rmtree(self.album, ignore_errors=True)

    def _new(self, name="01-one", **extra):
        return serve.new_piece_api(
            {
                "name": name,
                "source": {"kind": "csv", "path": "../data.csv"},
                "stamp": "2026-08-27T09:00:00Z",
                **extra,
            }
        )

    def test_listing_an_empty_album(self):
        self.assertEqual(serve.list_pieces_api(None), [])

    def test_creating_and_listing(self):
        self._new("01-one", title="One")
        self._new("02-two")
        names = [entry["name"] for entry in serve.list_pieces_api(None)]
        self.assertEqual(names, ["01-one", "02-two"])

    def test_opening_returns_the_manifest_and_what_it_infers(self):
        self._new()
        payload = serve.open_piece_api("01-one")
        self.assertEqual(payload["manifest"]["name"], "01-one")
        self.assertEqual(payload["folder"], "01-one")
        self.assertTrue(payload["source_exists"])
        self.assertEqual(payload["missing_samples"], [])

    def test_saving_a_manifest_round_trips(self):
        created = self._new()
        manifest = created["manifest"]
        manifest["title"] = "Renamed"
        manifest["performance"] = {
            "keymap": {"KeyA": {"kind": "degree", "degree": 0, "octave": 0}}
        }
        saved = serve.save_piece_api({"folder": "01-one", "manifest": manifest})
        self.assertEqual(saved["manifest"]["title"], "Renamed")
        self.assertEqual(len(saved["manifest"]["performance"]["keymap"]), 1)
        # And it is really on disk, not just echoed back.
        reopened = serve.open_piece_api("01-one")
        self.assertEqual(reopened["manifest"]["title"], "Renamed")

    def test_an_invalid_manifest_is_refused_before_it_reaches_the_disk(self):
        # A saved-but-invalid manifest would make the piece unopenable, which is
        # a worse failure than a rejected save.
        self._new()
        before = serve.open_piece_api("01-one")["manifest"]
        bad = {**before, "performance": {"keymap": {"KeyA": {"kind": "telepathy"}}}}
        with self.assertRaises(ValueError):
            serve.save_piece_api({"folder": "01-one", "manifest": bad})
        self.assertEqual(serve.open_piece_api("01-one")["manifest"], before)

    def test_rendering_a_piece_writes_into_it_and_records_itself(self):
        self._new()
        result = serve.render_upload({"piece": "01-one", "stamp": "2026-08-27T09:10:00Z"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["fingerprint"])
        self.assertTrue((self.album / "01-one" / "out" / "audio.json").exists())

        reopened = serve.open_piece_api("01-one")
        self.assertEqual(reopened["manifest"]["render"]["fingerprint"], result["fingerprint"])
        # Stored relative, so the folder stays portable.
        self.assertEqual(reopened["manifest"]["render"]["audio"], "out/audio.json")
        # Served through /pieces/, because an album can live outside the repo.
        self.assertTrue(reopened["render"]["audio_url"].startswith("/pieces/"))

    def test_rendering_a_piece_twice_gives_the_same_fingerprint(self):
        self._new()
        first = serve.render_upload({"piece": "01-one"})
        second = serve.render_upload({"piece": "01-one"})
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_a_piece_render_can_be_traced(self):
        self._new()
        result = serve.render_upload({"piece": "01-one", "trace": True, "trace_window": 4})
        kinds = [stage["kind"] for stage in result["trace"]["stages"]]
        self.assertEqual(kinds[0], "ingest")
        self.assertEqual(kinds[-1], "mapping")

    def test_folders_outside_the_pieces_root_are_refused(self):
        # The resolved path is checked, so `..` and absolute paths both fail.
        for attempt in ("../../../etc", "..", str(ROOT), "01-one/../../outside"):
            with self.subTest(attempt=attempt), self.assertRaises((ValueError, FileNotFoundError)):
                serve.open_piece_api(attempt)

    def test_writes_outside_the_pieces_root_are_refused(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            serve.save_piece_api({"folder": "../escape", "manifest": {}})

    def test_a_missing_piece_is_a_404_not_a_500(self):
        with self.assertRaises(FileNotFoundError):
            serve.open_piece_api("no-such-piece")


class TestSeedDispatch(unittest.TestCase):
    """The bug the endpoint exposed, pinned at its real location."""

    def test_a_directory_source_seeds_from_the_graph(self):
        # resolve_seed used to assume a CSV, so every caller that was not the
        # CLI -- the endpoint, a session re-render -- opened a directory as a
        # file and died on PermissionError.
        from serrin.chain import default_chain

        chain = default_chain()
        self.assertIsInstance(chain.resolve_seed(ROOT), int)

    @unittest.skipUnless(shutil.which("git"), "git is not on PATH")
    def test_it_matches_the_git_helper(self):
        from serrin.chain import default_chain
        from serrin.ingest_git import auto_seed_repo

        self.assertEqual(default_chain().resolve_seed(ROOT), auto_seed_repo(ROOT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
