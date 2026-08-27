"""Tests for the portable commit-history format.

The property the whole format exists for is at the bottom: **a repository and its
exported history produce the same stream.** Everything else is the validation
that makes that trustworthy — a history that loads but quietly collapses to one
voice, or reads churn as flat, looks like a bug in the pipeline rather than a gap
in the file, so it has to say so.
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
sys.path.insert(0, str(ROOT / "tests"))

from serrin.chain import Chain, default_chain  # noqa: E402
from serrin.graph import (  # noqa: E402
    FORMAT,
    GraphError,
    auto_seed_graph,
    describe_graph,
    export_graph,
    graph_facts,
    ingest_graph,
    validate_graph,
)
from serrin.ingest import fingerprint  # noqa: E402
from serrin.ingest_git import auto_seed_repo, ingest_repo  # noqa: E402
from serrin.tempo import Tempo  # noqa: E402

HAVE_GIT = shutil.which("git") is not None

_FIXTURE: dict = {}


def shared_repo() -> Path:
    if "repo" not in _FIXTURE:
        import git_fixture

        workspace = tempfile.mkdtemp(prefix="serrin-graph-test-")
        _FIXTURE["workspace"] = workspace
        _FIXTURE["repo"] = git_fixture.build(Path(workspace) / "repo")
    return _FIXTURE["repo"]


def tearDownModule():
    if _FIXTURE.get("workspace"):
        shutil.rmtree(str(_FIXTURE["workspace"]), ignore_errors=True)


@unittest.skipUnless(HAVE_GIT, "git is not on PATH")
class GraphCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = shared_repo()
        cls.document = export_graph(cls.repo, stamp="2026-08-27T12:00:00Z")
        cls.workspace = Path(tempfile.mkdtemp(prefix="serrin-graph-files-"))
        cls.path = cls.workspace / "history.json"
        cls.path.write_text(json.dumps(cls.document, indent=1), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workspace, ignore_errors=True)


class TestExport(GraphCase):
    def test_it_declares_its_format(self):
        self.assertEqual(self.document["format"], FORMAT)
        self.assertEqual(self.document["exported_at"], "2026-08-27T12:00:00Z")

    def test_every_commit_carries_what_the_metrics_need(self):
        for commit in self.document["commits"]:
            for key in ("sha", "timestamp", "parents", "author"):
                self.assertIn(key, commit)

    def test_ownership_is_baked_in(self):
        # Deciding which branch owns a commit needs rev-list against a real
        # repository; a flat commit list cannot recompute it. So the exporter
        # writes down the answer it already has.
        owners = {commit["owner"] for commit in self.document["commits"]}
        self.assertIn("main", owners)
        self.assertGreater(len(owners), 1)
        self.assertNotIn(None, owners)

    def test_stats_are_included_by_default(self):
        # Without them churn, insertions, deletions and files read as flat, and a
        # metric that produces a flat line without saying why is worse than one
        # that refuses.
        self.assertTrue(self.document["has_stats"])
        self.assertTrue(any(c.get("insertions") for c in self.document["commits"]))

    def test_stats_can_be_skipped_and_the_file_admits_it(self):
        document = export_graph(self.repo, with_stats=False)
        self.assertFalse(document["has_stats"])
        self.assertNotIn("insertions", document["commits"][0])
        self.assertTrue(
            any("stats" in problem for problem in validate_graph(document)),
            "a statless export should say so",
        )

    def test_the_traversal_is_recorded_and_is_the_file_order(self):
        chrono = export_graph(self.repo, traversal="chrono")
        reverse = export_graph(self.repo, traversal="reverse")
        self.assertEqual(chrono["traversal"], "chrono")
        self.assertEqual(reverse["traversal"], "reverse")
        self.assertEqual(
            [c["sha"] for c in chrono["commits"]],
            list(reversed([c["sha"] for c in reverse["commits"]])),
        )

    def test_exporting_something_that_is_not_a_repository(self):
        with self.assertRaises(GraphError):
            export_graph(self.workspace)

    def test_facts_summarise_the_document(self):
        facts = graph_facts(self.document)
        self.assertEqual(facts["commits"], len(self.document["commits"]))
        self.assertEqual(facts["merges"], 1)
        self.assertEqual(facts["authors"], 3)
        self.assertGreater(facts["span_seconds"], 86400)
        self.assertEqual(sum(facts["owned"].values()), facts["commits"])


class TestValidation(GraphCase):
    def test_a_good_document_has_no_problems(self):
        self.assertEqual(validate_graph(self.document), [])

    def test_it_reports_everything_at_once(self):
        # Problems rather than exceptions: the author needs to see all of it, not
        # one raise at a time.
        problems = validate_graph({"commits": [{"sha": "a"}, {"sha": "a"}]})
        self.assertTrue(any("duplicate" in p for p in problems))
        self.assertTrue(any("timestamp" in p for p in problems))
        self.assertTrue(any("owning branch" in p for p in problems))

    def test_it_refuses_a_non_object(self):
        self.assertEqual(validate_graph([1, 2, 3]), ["the file is not a JSON object"])

    def test_it_refuses_an_empty_history(self):
        self.assertTrue(any("no commits" in p for p in validate_graph({"commits": []})))

    def test_it_notices_a_foreign_format(self):
        document = {**self.document, "format": "someone-elses/9"}
        self.assertTrue(any("format is" in p for p in validate_graph(document)))

    def test_it_warns_when_nothing_names_an_owner(self):
        # A file that quietly collapses to one voice looks like a pipeline bug.
        document = {
            "commits": [
                {"sha": f"{i:040x}", "timestamp": 1000 + i} for i in range(5)
            ]
        }
        self.assertTrue(any("one\nvoice" in p or "one voice" in p for p in validate_graph(document)))

    def test_it_warns_past_the_voice_ceiling(self):
        document = {
            "commits": [
                {"sha": f"{i:040x}", "timestamp": 1000 + i, "owner": f"branch{i}"}
                for i in range(12)
            ]
        }
        self.assertTrue(any("ceiling" in p for p in validate_graph(document)))


class TestIngest(GraphCase):
    def test_a_history_loads_into_a_stream(self):
        stream = ingest_graph(self.path)
        self.assertEqual(stream.meta["source_kind"], "graph")
        self.assertGreater(stream.n_voices, 1)
        self.assertEqual(stream.meta["git"]["from_file"], True)
        self.assertEqual(stream.meta["git"]["repo"], self.repo.name)

    def test_it_refuses_a_history_with_no_timestamps(self):
        # Fatal rather than a warning: the chronological order and the interval
        # metric both read timestamps.
        broken = self.workspace / "broken.json"
        broken.write_text(json.dumps({"commits": [{"sha": "a"}]}), encoding="utf-8")
        with self.assertRaises(GraphError):
            ingest_graph(broken)

    def test_it_refuses_a_missing_file(self):
        with self.assertRaises(GraphError):
            ingest_graph(self.workspace / "nope.json")

    def test_it_refuses_invalid_json(self):
        bad = self.workspace / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(GraphError):
            ingest_graph(bad)

    def test_an_unknown_metric_is_loud(self):
        with self.assertRaises(GraphError):
            ingest_graph(self.path, metric="vibes")

    def test_topological_order_is_refused_rather_than_faked(self):
        # It needs the DAG walked and the file is a flat list, so pretending
        # would silently produce a different piece.
        with self.assertRaises(GraphError) as caught:
            ingest_graph(self.path, traversal="topo")
        self.assertIn("re-export", str(caught.exception))

    def test_a_topological_export_can_be_read_back_as_topological(self):
        topo = self.workspace / "topo.json"
        topo.write_text(json.dumps(export_graph(self.repo, traversal="topo")), encoding="utf-8")
        stream = ingest_graph(topo, traversal="topo")
        self.assertEqual(stream.meta["git"]["traversal"], "topo")

    def test_every_metric_produces_a_usable_stream(self):
        from serrin.ingest_git import METRICS

        for metric in METRICS:
            with self.subTest(metric=metric):
                stream = ingest_graph(self.path, metric=metric)
                self.assertEqual(len({len(channel) for channel in stream.data}), 1)
                for channel in stream.data:
                    self.assertTrue(all(0 <= v < 256 for v in channel))

    def test_explicit_branches_are_honoured(self):
        stream = ingest_graph(self.path, branch_names=["main"])
        self.assertEqual(stream.names, ["main"])

    def test_an_unknown_branch_is_loud(self):
        with self.assertRaises(GraphError):
            ingest_graph(self.path, branch_names=["no/such"])

    def test_the_voice_ceiling_is_respected(self):
        stream = ingest_graph(self.path, max_voices=2)
        self.assertLessEqual(stream.n_voices, 2)

    def test_a_history_without_owners_becomes_one_voice(self):
        # Usable, and the validation says why it is thin.
        flat = self.workspace / "flat.json"
        flat.write_text(
            json.dumps(
                {
                    "commits": [
                        {"sha": f"{i:040x}", "timestamp": 1_700_000_000 + i * 60}
                        for i in range(30)
                    ]
                }
            ),
            encoding="utf-8",
        )
        stream = ingest_graph(flat)
        self.assertEqual(stream.n_voices, 1)


class TestTheGuarantee(GraphCase):
    """A repository and its exported history must produce the same stream."""

    def test_the_streams_are_identical(self):
        for metric in ("hash", "interval", "churn", "parents", "hour", "author"):
            with self.subTest(metric=metric):
                from_repo = ingest_repo(self.repo, metric=metric)
                from_file = ingest_graph(self.path, metric=metric)
                self.assertEqual(
                    fingerprint(from_repo),
                    fingerprint(from_file),
                    f"{metric} drifts between the repository and its export",
                )

    def test_the_voices_are_the_same_and_in_the_same_order(self):
        from_repo = ingest_repo(self.repo)
        from_file = ingest_graph(self.path)
        self.assertEqual(from_repo.names, from_file.names)
        self.assertEqual(from_repo.meta["git"]["owned"], from_file.meta["git"]["owned"])
        self.assertEqual(from_repo.meta["git"]["merges"], from_file.meta["git"]["merges"])

    def test_the_seed_is_the_same(self):
        # So a piece rendered from a repo and from its export land on the same
        # seed, not merely the same samples. The export bakes it: the repository
        # seeds from HEAD's own first commits in git's order, and a merged
        # timeline is a different list -- so it is recorded, not recomputed.
        self.assertEqual(self.document["seed"], auto_seed_repo(self.repo))
        self.assertEqual(auto_seed_graph(self.path), auto_seed_repo(self.repo))

    def test_a_hand_written_history_seeds_from_its_own_commits(self):
        # No repository to agree with, so it falls back to its own head.
        flat = self.workspace / "unseeded.json"
        commits = [{"sha": f"{i:040x}", "timestamp": 1_700_000_000 + i} for i in range(10)]
        flat.write_text(json.dumps({"commits": commits}), encoding="utf-8")
        first = auto_seed_graph(flat)
        self.assertIsInstance(first, int)
        # Changing the head changes the seed; changing the tail does not.
        commits[0]["sha"] = "f" * 40
        flat.write_text(json.dumps({"commits": commits}), encoding="utf-8")
        self.assertNotEqual(auto_seed_graph(flat), first)

    def test_a_whole_render_matches(self):
        chain = Chain.load(ROOT / "presets" / "merkle_drift.json")
        tempo = Tempo(84, 8)
        from_repo = chain.apply(ingest_repo(self.repo, tempo=tempo), seed=7)
        from_file = chain.apply(ingest_graph(self.path, tempo=tempo), seed=7)
        self.assertEqual(fingerprint(from_repo), fingerprint(from_file))

    def test_chain_seeding_dispatches_on_a_json_history(self):
        # resolve_seed has to recognise a history file, or a piece whose source is
        # one would seed from the file's bytes as if it were a CSV.
        self.assertEqual(default_chain().resolve_seed(self.path), auto_seed_graph(self.path))

    def test_describe_reads_without_exploding(self):
        text = describe_graph(ingest_graph(self.path))
        self.assertIn("history file", text)
        self.assertIn("main", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
