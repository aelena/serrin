"""Tests for the commit-graph source (section 6.3).

Built against a synthetic repository, not whatever is checked out, because the
properties worth testing -- a merged branch, a stale branch, a trunk that keeps
moving, bursts separated by quiet days -- do not exist in a fresh repo and would
otherwise depend on the developer's own history.

The assignment tests carry most of the weight. "Which branch does this commit
belong to" is a question git cannot answer directly, and two plausible answers
were wrong in ways that produced a *plausible-looking* result: the right number
of voices with the wrong content, or fewer voices than the repo visibly has.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serrin.chain import Chain  # noqa: E402
from serrin.export import build_render  # noqa: E402
from serrin.ingest import fingerprint  # noqa: E402
from serrin.ingest_git import (  # noqa: E402
    METRICS,
    TRUNK_NAMES,
    GitError,
    auto_seed_repo,
    branches,
    commits,
    describe_repo,
    exclusive_shas,
    ingest_repo,
)
from serrin.session import Session  # noqa: E402
from serrin.stream import MAX_VOICES  # noqa: E402
from serrin.tempo import Tempo  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_fixture  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


#: One repo for the whole module, built on first use.
#:
#: Building it costs ~60 real git commits, which is seconds -- so a per-class
#: setUpClass rebuilt it four times and turned this file into the slowest thing
#: in the suite. It is read-only for every test, so sharing is safe.
_FIXTURE: dict[str, object] = {}


def _shared_repo() -> Path:
    if "repo" not in _FIXTURE:
        workspace = tempfile.mkdtemp(prefix="serrin-git-test-")
        _FIXTURE["workspace"] = workspace
        _FIXTURE["repo"] = git_fixture.build(Path(workspace) / "repo")
    return _FIXTURE["repo"]  # type: ignore[return-value]


def tearDownModule():
    workspace = _FIXTURE.get("workspace")
    if workspace:
        shutil.rmtree(str(workspace), ignore_errors=True)


@unittest.skipUnless(HAVE_GIT, "git is not on PATH")
class GitFixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = _shared_repo()
        cls.workspace = str(_FIXTURE["workspace"])


class TestReadingTheGraph(GitFixtureCase):
    def test_branches_are_found_newest_first(self):
        found = branches(self.repo)
        names = [name for name, _ in found]
        self.assertIn("main", names)
        self.assertIn("spike/abandoned", names)
        stamps = [stamp for _, stamp in found]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_commits_carry_the_fields_the_metrics_need(self):
        found = commits(self.repo, "main")
        self.assertGreater(len(found), 20)
        first = found[0]
        self.assertEqual(len(first.sha), 40)
        self.assertGreater(first.timestamp, 0)
        self.assertTrue(first.author)

    def test_the_merge_commit_has_two_parents(self):
        # Merges are `interleave` happening in the data, so they have to survive
        # parsing -- a --no-ff merge is the only multi-parent commit here.
        found = commits(self.repo, "main")
        merges = [c for c in found if len(c.parents) > 1]
        self.assertEqual(len(merges), 1)
        self.assertEqual(len(merges[0].parents), 2)

    def test_numstat_lands_on_the_right_commit(self):
        # The bug this guards: with --numstat, git prints the format line, a
        # blank line, then the stats. A record separator at the *end* of the
        # format put each commit's stats into the next commit's chunk, which
        # silently produced four commits with identical churn.
        without = commits(self.repo, "main", need_stats=False)
        with_stats = commits(self.repo, "main", need_stats=True)
        self.assertEqual(len(without), len(with_stats), "stats parsing lost commits")
        touched = [c for c in with_stats if c.files > 0]
        self.assertGreater(len(touched), len(with_stats) // 2, "almost nothing has stats")
        self.assertTrue(any(c.insertions > 0 for c in with_stats))

    def test_traversals_all_return_the_same_set(self):
        sets = {}
        for traversal in ("chrono", "topo", "reverse"):
            sets[traversal] = {c.sha for c in commits(self.repo, "main", traversal)}
        self.assertEqual(sets["chrono"], sets["topo"])
        self.assertEqual(sets["chrono"], sets["reverse"])

    def test_reverse_really_reverses(self):
        forward = [c.sha for c in commits(self.repo, "main", "chrono")]
        backward = [c.sha for c in commits(self.repo, "main", "reverse")]
        self.assertEqual(forward[0], backward[-1])

    def test_a_bad_traversal_is_loud(self):
        with self.assertRaises(GitError):
            commits(self.repo, "main", "sideways")

    def test_not_a_repository(self):
        with self.assertRaises(GitError):
            ingest_repo(Path(self.workspace) / "definitely-not-a-repo")


class TestBranchAssignment(GitFixtureCase):
    """Who owns which commit -- the part that took three attempts."""

    def test_exclusive_shas_are_exclusive(self):
        names = [name for name, _ in branches(self.repo)]
        seen: set[str] = set()
        for ref in names:
            mine = exclusive_shas(self.repo, ref, [o for o in names if o != ref])
            self.assertFalse(mine & seen, f"{ref} shares a commit with another branch")
            seen |= mine

    def test_the_trunk_keeps_the_shared_history(self):
        stream = ingest_repo(self.repo)
        owned = stream.meta["git"]["owned"]
        self.assertEqual(stream.meta["git"]["trunk"], "main")
        # main holds the trunk, so it owns far more than the feature branches.
        self.assertGreater(owned["main"], owned["work/current"])
        self.assertGreater(owned["main"], owned["spike/abandoned"])

    def test_a_conventional_name_wins_over_reachability(self):
        # work/current reaches more commits than main does, so a pure
        # "reaches the most" rule would hand it the trunk and mislabel
        # every voice in the piece.
        self.assertIn("main", TRUNK_NAMES)
        stream = ingest_repo(self.repo)
        self.assertEqual(stream.meta["git"]["trunk"], "main")

    def test_the_stale_branch_owns_exactly_its_own_commits(self):
        stream = ingest_repo(self.repo)
        self.assertEqual(stream.meta["git"]["owned"]["spike/abandoned"], 3)

    def test_the_open_branch_owns_exactly_its_own_commits(self):
        stream = ingest_repo(self.repo)
        self.assertEqual(stream.meta["git"]["owned"]["work/current"], 14)

    def test_a_fully_merged_branch_is_dropped_and_named(self):
        # Every commit it could contribute is already in another voice, so a
        # voice for it would only duplicate one. Dropping is right; doing it
        # silently is not.
        stream = ingest_repo(self.repo)
        self.assertIn("feature/long", stream.meta["git"]["dropped_branches"])
        self.assertNotIn("feature/long", stream.names)
        self.assertIn("no commits of its own", describe_repo(stream))

    def test_every_commit_is_owned_exactly_once(self):
        stream = ingest_repo(self.repo)
        info = stream.meta["git"]
        self.assertEqual(sum(info["owned"].values()), info["commits"])

    def test_explicit_branches_are_honoured(self):
        stream = ingest_repo(self.repo, branch_names=["main", "spike/abandoned"])
        self.assertEqual(set(stream.names), {"main", "spike/abandoned"})

    def test_an_unknown_branch_is_loud(self):
        with self.assertRaises(GitError):
            ingest_repo(self.repo, branch_names=["no/such/branch"])

    def test_the_voice_ceiling_is_respected(self):
        stream = ingest_repo(self.repo, max_voices=2)
        self.assertLessEqual(stream.n_voices, 2)


class TestVoicesAndMetrics(GitFixtureCase):
    def test_all_channels_are_the_same_length(self):
        # The Stream invariant every pedal depends on.
        for metric in METRICS:
            with self.subTest(metric=metric):
                stream = ingest_repo(self.repo, metric=metric)
                self.assertEqual(len({len(channel) for channel in stream.data}), 1)

    def test_values_stay_inside_the_bit_depth(self):
        for metric in ("hash", "interval", "churn", "parents", "hour", "author"):
            with self.subTest(metric=metric):
                stream = ingest_repo(self.repo, metric=metric, bit_depth=8)
                for channel in stream.data:
                    self.assertTrue(all(0 <= v < 256 for v in channel))

    def test_a_quiet_branch_becomes_a_quiet_voice(self):
        """The doc's claim, as an assertion.

        A branch with three commits should hold its value almost everywhere,
        because holding is what makes `delta` read silence. Density becomes a
        property of the repo's real activity rather than something normalized
        away.
        """
        stream = ingest_repo(self.repo, metric="hash")
        rates = {}
        for index, name in enumerate(stream.names):
            channel = stream.data[index]
            changes = sum(1 for a, b in zip(channel, channel[1:]) if a != b)
            rates[name] = changes / max(1, len(channel) - 1)

        self.assertLess(rates["spike/abandoned"], 0.15, "the stale branch is not sparse")
        self.assertGreater(rates["main"], rates["spike/abandoned"] * 3)

    def test_hash_values_are_not_renormalized(self):
        # Hash bytes are already uniform; min/max normalizing them would only
        # exchange one flavour of noise for another while pretending to help.
        stream = ingest_repo(self.repo, metric="hash")
        trunk = stream.data[stream.names.index("main")]
        self.assertGreater(max(trunk) - min(trunk), 120)

    def test_parents_uses_a_fixed_scale(self):
        # Normalizing this in a merge-free repo yields a flat mid-scale line, and
        # in a repo with merges maps "one parent" to zero. Neither is a reading.
        stream = ingest_repo(self.repo, metric="parents")
        values = {v for channel in stream.data for v in channel}
        self.assertIn(32, values, "an ordinary commit should read as 1 parent")
        self.assertTrue(any(v >= 64 for v in values), "the merge did not register")

    def test_hour_uses_a_fixed_scale(self):
        stream = ingest_repo(self.repo, metric="hour")
        for channel in stream.data:
            self.assertTrue(all(0 <= v <= 255 for v in channel))

    def test_an_unknown_metric_is_loud(self):
        with self.assertRaises(GitError):
            ingest_repo(self.repo, metric="vibes")

    def test_metadata_describes_the_graph(self):
        stream = ingest_repo(self.repo, metric="churn", traversal="topo")
        info = stream.meta["git"]
        self.assertEqual(info["metric"], "churn")
        self.assertEqual(info["traversal"], "topo")
        self.assertEqual(info["merges"], 1)
        self.assertEqual(info["authors"], 3)
        self.assertGreater(info["span_seconds"], 86400)
        self.assertEqual(stream.meta["source_kind"], "git")


class TestDeterminismAndIntegration(GitFixtureCase):
    def test_the_same_repo_gives_the_same_stream(self):
        first = ingest_repo(self.repo, metric="hash")
        second = ingest_repo(self.repo, metric="hash")
        self.assertEqual(first.data, second.data)
        self.assertEqual(fingerprint(first), fingerprint(second))

    def test_the_seed_is_stable(self):
        self.assertEqual(auto_seed_repo(self.repo), auto_seed_repo(self.repo))

    def test_the_traversal_changes_the_piece(self):
        chrono = ingest_repo(self.repo, traversal="chrono")
        reverse = ingest_repo(self.repo, traversal="reverse")
        self.assertNotEqual(fingerprint(chrono), fingerprint(reverse))

    def test_a_graph_stream_runs_the_whole_pipeline_unchanged(self):
        # The claim section 6.3 makes: a commit graph is an ingestion adapter and
        # nothing downstream needs to know.
        stream = ingest_repo(self.repo, metric="hash", tempo=Tempo(84, 8))
        chain = Chain.load(Path(__file__).resolve().parent.parent / "presets" / "merkle_drift.json")
        transformed = chain.apply(stream, seed=auto_seed_repo(self.repo))
        rendered = build_render(transformed, chain=chain)

        self.assertEqual(len(rendered.audio["voices"]), stream.n_voices)
        self.assertEqual(len(rendered.visual["voices"]), stream.n_voices)
        self.assertEqual(rendered.meta["source_kind"], "git")
        self.assertEqual(rendered.meta["git"]["metric"], "hash")
        self.assertTrue(all(v["freq"] for v in rendered.audio["voices"]))

    def test_a_git_session_round_trips(self):
        stream = ingest_repo(self.repo, metric="interval", traversal="chrono")
        chain = Chain.load(Path(__file__).resolve().parent.parent / "presets" / "merkle_drift.json")
        before = fingerprint(chain.apply(stream, seed=7))

        session = Session.from_json(
            {
                "source": {
                    "path": str(self.repo),
                    "kind": "git",
                    "metric": "interval",
                    "traversal": "chrono",
                    "bit_depth": 8,
                },
                "preset": chain.to_json(),
            }
        )
        self.assertEqual(session.kind, "git")
        # The git branch of ingest_kwargs must not hand CSV keywords to
        # ingest_repo -- that would be a TypeError, not a near-miss.
        again = ingest_repo(session.path, **session.ingest_kwargs())
        self.assertEqual(before, fingerprint(chain.apply(again, seed=7)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
