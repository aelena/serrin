"""A repository's commit graph as a source (section 6.3).

The design document parked this as "the most promising candidate for an
alternative data source", and its instinct about where it fits was right: this is
an *ingestion adapter*, nothing more. It produces the same ``Stream`` that
``ingest_csv`` does, so pedals, forked export and the whole runtime are untouched.

Three of the document's four claims hold up in practice. The fourth needed a
decision.

**The hashes really are noise.** A commit hash is designed to be
indistinguishable from randomness, so a hash-derived voice needs no aggressive
pedals to sound chaotic -- which makes it behave very differently under Caesar
and XOR than monitoring data does. Monitoring data has structure to break;
hashes have none to begin with, so the pedals stop *destroying* and start merely
*relabelling*. That is a genuinely different instrument, not a variation.

**Branches give voices with real semantics**, including the stale ones. A branch
with four commits produces a voice that speaks four times and holds still in
between, which the `delta` pedal turns into near-silence. Sparse voices are a
feature here, exactly as the doc predicted -- density becomes a property of the
repo's real activity rather than something to normalize away.

**Merges are `interleave` happening in the data.** A merge commit has two or
more parents, and the `parents` metric makes those moments legible as spikes.

**Ordering was the real question**, and the document left it open. The default
here is chronological, because the piece exists in time and a repository's
rhythm -- bursts, nights, dead weekends -- is the most musical property it has.
Topological order throws the real timestamps away and yields an even, meaningless
cadence: correct as a graph traversal, inert as music. `--traversal topo` is
there for when the graph's shape matters more than its rhythm.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ingest import quantize
from .rng import seed_from_bytes
from .stream import MAX_VOICES, Stream
from .tempo import Tempo

#: Field separator for the git log format. A control character, because commit
#: subjects contain every printable one.
_SEP = "\x1f"
_REC = "\x1e"

#: What a commit contributes to its voice.
METRICS = {
    "hash": "bytes of the commit hash -- real noise, needs no pedals to be chaotic",
    "interval": "seconds since that branch's previous commit -- the repo's rhythm",
    "churn": "lines added plus removed",
    "insertions": "lines added",
    "deletions": "lines removed",
    "files": "files touched",
    "parents": "number of parents -- merges spike, and merges are interleave",
    "hour": "hour of day, 0-23 -- diurnal periodicity",
    "author": "hashed author identity -- changes voice when the person changes",
}

#: Metrics whose distribution is heavy-tailed enough that a linear scale wastes
#: most of the range on a handful of outliers.
_LOG_BY_DEFAULT = {"interval", "churn", "insertions", "deletions", "files"}

#: Metrics with a meaningful absolute range, mapped against that range instead of
#: against whatever this repo happened to contain.
#:
#: Min/max normalization is right for churn -- "big for this repo" is the useful
#: reading -- and wrong for these two. Normalizing `parents` in a repo with no
#: merges yields a flat mid-scale line; normalizing `hour` in a repo whose
#: commits all land between 09:00 and 18:00 stretches office hours across the
#: full range and destroys the diurnal shape the metric exists for.
_FIXED_SCALE = {"parents": 8, "hour": 24}

TRAVERSALS = ("chrono", "topo", "reverse")

#: Branch names that mean "trunk" if they are present. Checked before falling
#: back to "reaches the most commits", because a long-lived feature branch can
#: easily out-reach main and then gets handed the shared history -- true by the
#: reachability metric, wrong by every other reading.
TRUNK_NAMES = ("main", "master", "trunk", "develop", "development")


class GitError(ValueError):
    pass


@dataclass
class Commit:
    sha: str
    timestamp: int
    parents: list[str]
    author: str
    subject: str
    insertions: int = 0
    deletions: int = 0
    files: int = 0

    @property
    def churn(self) -> int:
        return self.insertions + self.deletions


# ---------------------------------------------------------------------------
# talking to git
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str, timeout: int = 60) -> str:
    if shutil.which("git") is None:
        raise GitError("git is not on PATH")
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip().splitlines()
        raise GitError(f"git {' '.join(args[:2])} failed: {message[0] if message else '?'}")
    return completed.stdout


def branches(repo: Path) -> list[tuple[str, int]]:
    """Local branches with the timestamp of their tip, most recent first."""
    raw = _git(
        repo,
        "for-each-ref",
        f"--format=%(refname:short){_SEP}%(committerdate:unix)",
        "refs/heads",
    )
    found = []
    for line in raw.splitlines():
        if _SEP not in line:
            continue
        name, _, stamp = line.partition(_SEP)
        try:
            found.append((name.strip(), int(stamp.strip() or 0)))
        except ValueError:
            continue
    if not found:
        raise GitError(f"{repo} has no local branches (an empty repository?)")
    # Most recently touched first: with more than eight branches the ceiling
    # forces a choice, and "what is alive" is the least arbitrary one available.
    found.sort(key=lambda pair: -pair[1])
    return found


def commits(repo: Path, ref: str, traversal: str = "chrono", need_stats: bool = False,
            limit: int | None = None) -> list[Commit]:
    """Commits reachable from ``ref``, in the requested order."""
    order = {
        "chrono": ["--date-order"],
        "reverse": ["--date-order", "--reverse"],
        "topo": ["--topo-order"],
    }.get(traversal)
    if order is None:
        raise GitError(f"unknown traversal {traversal!r}; use one of {TRAVERSALS}")

    # The record separator goes at the *front*. With --numstat, git prints the
    # format line, a blank line, then the per-file stats -- so a separator at
    # the end would put each commit's stats in the *next* commit's chunk. That
    # is exactly the bug this comment exists to prevent someone re-introducing.
    fmt = _REC + _SEP.join(["%H", "%ct", "%P", "%an", "%s"])
    args = ["log", *order, f"--pretty=format:{fmt}", ref]
    if limit:
        args.append(f"-n{limit}")
    if need_stats:
        # --numstat rather than --shortstat: parseable per file, and it lets the
        # file count fall out of the same pass.
        args.append("--numstat")

    raw = _git(repo, *args)
    found: list[Commit] = []
    for chunk in raw.split(_REC):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        head, _, stat_block = chunk.partition("\n")
        fields = head.split(_SEP)
        if len(fields) < 5:
            continue
        sha, stamp, parents, author, subject = fields[:5]
        try:
            timestamp = int(stamp)
        except ValueError:
            continue
        commit = Commit(
            sha=sha.strip(),
            timestamp=timestamp,
            parents=[p for p in parents.split() if p],
            author=author.strip(),
            subject=subject.strip(),
        )
        if need_stats and stat_block.strip():
            for line in stat_block.splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                added, removed = parts[0], parts[1]
                # Binary files report "-" for both counts.
                commit.insertions += int(added) if added.isdigit() else 0
                commit.deletions += int(removed) if removed.isdigit() else 0
                commit.files += 1
        found.append(commit)
    if not found:
        raise GitError(f"{ref} has no commits")
    return found


# ---------------------------------------------------------------------------
# graph -> voices
# ---------------------------------------------------------------------------
def exclusive_shas(repo: Path, ref: str, others: list[str]) -> set[str]:
    """Commits reachable from ``ref`` and from none of ``others``.

    This is the question git can actually answer about branch membership, and
    asking it directly is what makes the assignment mean something.
    """
    if not others:
        return set(_git(repo, "rev-list", ref).split())
    return set(_git(repo, "rev-list", ref, "--not", *others).split())


def assign_to_branches(
    repo: Path,
    refs: list[str],
    traversal: str,
    need_stats: bool,
    limit: int | None,
) -> tuple[dict[str, list[Commit]], list[Commit]]:
    """Give every commit to exactly one branch.

    Git does not record which branch a commit was made on. A commit belongs to
    every branch that can reach it, and nearly all of them reach the whole
    trunk -- so ownership has to be assigned, and how it is assigned decides
    whether the voices mean anything.

    Two wrong answers came first, both of which only showed up on a real graph:

    * *Claim in branch-recency order.* The newest branch reaches the entire
      trunk, so it swallowed it, and `main` and the merged feature branch were
      left owning nothing and dropped as silent. Two voices where the repo
      plainly had four.
    * *Claim smallest-reachable-set first.* Better, but a feature branch reaches
      the trunk too and is smaller than `main`, so it took the trunk and `main`
      was left owning one commit. Four voices, three of them mislabelled.

    The right question is not "who claims it first" but "whose is it alone",
    which git answers directly: ``rev-list ref --not <every other ref>`` gives a
    branch's *exclusive* commits. Those are unambiguously that branch's work.

    What remains -- the shared trunk, reachable from everything -- belongs to the
    branch that reaches the most, which is the trunk-most branch by definition.

    The result is what section 6.3 described: each voice is its branch's own
    work, a stale branch becomes a sparse voice that blips and holds, and the
    trunk is one long dense voice rather than an accident of iteration order.
    """
    reachable: dict[str, list[Commit]] = {}
    timeline: dict[str, Commit] = {}
    for ref in refs:
        reachable[ref] = commits(repo, ref, traversal, need_stats, limit)
        for commit in reachable[ref]:
            timeline.setdefault(commit.sha, commit)

    # A conventional name wins; otherwise the branch reaching the most commits.
    named = [ref for ref in TRUNK_NAMES if ref in refs]
    trunk = named[0] if named else max(refs, key=lambda ref: (len(reachable[ref]), ref))

    owned: dict[str, list[Commit]] = {}
    claimed: set[str] = set()
    for ref in refs:
        if ref == trunk:
            continue
        mine = exclusive_shas(repo, ref, [other for other in refs if other != ref])
        owned[ref] = [c for c in reachable[ref] if c.sha in mine]
        claimed.update(c.sha for c in owned[ref])

    owned[trunk] = [c for c in reachable[trunk] if c.sha not in claimed]

    ordered = list(timeline.values())
    if traversal == "reverse":
        ordered.sort(key=lambda c: c.timestamp)
    elif traversal == "chrono":
        ordered.sort(key=lambda c: -c.timestamp)
    return owned, ordered


def trunk_of(refs: list[str], owned: dict[str, list]) -> str:
    """Which branch ended up holding the shared history. For the record only."""
    named = [ref for ref in TRUNK_NAMES if ref in refs]
    if named:
        return named[0]
    return max(owned, key=lambda ref: len(owned[ref])) if owned else ""


def _metric_value(commit: Commit, metric: str, previous: Commit | None, index: int) -> float:
    if metric == "hash":
        # Two bytes, so consecutive commits differ across the whole range rather
        # than by whatever the first hex digit happens to be.
        return int(commit.sha[:4], 16) if len(commit.sha) >= 4 else 0
    if metric == "interval":
        if previous is None:
            return 0.0
        return abs(commit.timestamp - previous.timestamp)
    if metric == "churn":
        return commit.churn
    if metric == "insertions":
        return commit.insertions
    if metric == "deletions":
        return commit.deletions
    if metric == "files":
        return commit.files
    if metric == "parents":
        return len(commit.parents)
    if metric == "hour":
        return (commit.timestamp // 3600) % 24
    if metric == "author":
        return seed_from_bytes(commit.author.encode("utf-8")) % 256
    raise GitError(f"unknown metric {metric!r}; have {', '.join(sorted(METRICS))}")


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------
def ingest_repo(
    path: str | Path,
    metric: str = "hash",
    traversal: str = "chrono",
    branch_names: list[str] | None = None,
    bit_depth: int = 8,
    tempo: Tempo | str | dict | None = None,
    rate: float | None = None,
    max_voices: int = MAX_VOICES,
    limit: int | None = None,
    log_scale: bool | None = None,
) -> Stream:
    """Read a repository's commit graph into a Stream.

    Every branch becomes a voice on one shared timeline. A voice takes a new
    value at each of *its own* commits and **holds** in between -- which is what
    turns a quiet branch into a quiet voice: a held value has zero delta, and
    `delta` is the pedal that reads change. Silence falls out of the data rather
    than being imposed on it.
    """
    repo = Path(path)
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        raise GitError(f"{repo} is not a git repository")
    if metric not in METRICS:
        raise GitError(f"unknown metric {metric!r}; have {', '.join(sorted(METRICS))}")

    ranked = branches(repo)
    if branch_names:
        available = {name for name, _ in ranked}
        missing = [name for name in branch_names if name not in available]
        if missing:
            raise GitError(f"no such branch(es): {', '.join(missing)}; have {sorted(available)}")
        refs = list(branch_names)
    else:
        refs = [name for name, _ in ranked]
    if len(refs) > max_voices:
        # Section 3.2's ceiling, doing exactly what it was designed to do.
        refs = refs[:max_voices]

    need_stats = metric in {"churn", "insertions", "deletions", "files"}
    owned, timeline = assign_to_branches(repo, refs, traversal, need_stats, limit)

    # Branches owning nothing are dropped, and named. A fully merged branch, or
    # one that is an ancestor of another, has no commits of its own -- every
    # commit it could contribute is already in some other voice, so giving it a
    # voice would only duplicate one. Dropping is right; doing it silently is
    # not, because "my branch is missing" is otherwise a mystery.
    voices = [ref for ref in refs if owned[ref]]
    dropped = [ref for ref in refs if not owned[ref]]
    if not voices:
        raise GitError(
            "no branch owns any commits -- every branch is contained in another. "
            "Pass --branches to pick explicitly."
        )

    frames = len(timeline)
    position = {commit.sha: index for index, commit in enumerate(timeline)}

    grid = (
        Tempo.parse(tempo)
        if tempo is not None
        else (Tempo.from_rate(rate) if rate else Tempo.from_rate(4.0))
    )

    channels: list[list[int]] = []
    for ref in voices:
        raw = [None] * frames  # type: list[float | None]
        previous: Commit | None = None
        for commit in sorted(owned[ref], key=lambda c: c.timestamp):
            index = position.get(commit.sha)
            if index is None:
                continue
            raw[index] = _metric_value(commit, metric, previous, index)
            previous = commit

        # Hold the last value forward, and backfill the head with the first one
        # so a branch that starts late does not open on a spurious zero.
        held: list[float] = []
        carry = next((value for value in raw if value is not None), 0.0)
        for value in raw:
            if value is not None:
                carry = value
            held.append(carry)

        if metric == "hash":
            # Already uniform over its range -- normalizing would only shuffle
            # which noise you get, so the bytes go through untouched.
            ceiling = (1 << bit_depth) - 1
            channels.append([int(value) & ceiling for value in held])
        elif metric in _FIXED_SCALE:
            ceiling = (1 << bit_depth) - 1
            top = _FIXED_SCALE[metric]
            channels.append(
                [max(0, min(ceiling, round(value / top * ceiling))) for value in held]
            )
        else:
            use_log = log_scale if log_scale is not None else metric in _LOG_BY_DEFAULT
            channels.append(quantize(held, bit_depth, use_log))

    stream = Stream(
        names=voices,
        data=channels,
        bit_depth=bit_depth,
        tempo=grid,
        meta={
            "source": str(repo),
            "source_kind": "git",
            "source_rows": frames,
            "columns": voices,
            "granularity": 1,
            "aggregation": None,
            "log_scale": bool(log_scale) if log_scale is not None else metric in _LOG_BY_DEFAULT,
            "tempo": grid.to_json(),
            "git": {
                "metric": metric,
                "traversal": traversal,
                "branches": voices,
                "dropped_branches": dropped,
                "considered_branches": refs,
                "commits": frames,
                "owned": {ref: len(owned[ref]) for ref in voices},
                "trunk": trunk_of(refs, owned),
                "merges": sum(1 for c in timeline if len(c.parents) > 1),
                "authors": len({c.author for c in timeline}),
                "span_seconds": (
                    max(c.timestamp for c in timeline) - min(c.timestamp for c in timeline)
                    if timeline
                    else 0
                ),
            },
        },
    )
    return stream


def auto_seed_repo(path: str | Path, count: int = 64) -> int:
    """Seed from the first commits, mirroring the CSV adapter's first-N-rows rule.

    The tip commit's hash alone would do -- it is already a hash of everything
    reachable from it -- but hashing a window of them keeps the behaviour
    parallel to the CSV side, where the seed changes when the head of the data
    changes and not otherwise.
    """
    repo = Path(path)
    raw = _git(repo, "log", "--pretty=format:%H", f"-n{count}")
    return seed_from_bytes((raw or str(repo.name)).encode("utf-8"))


def describe_repo(stream: Stream) -> str:
    """Human summary of what the graph turned out to be."""
    info = stream.meta.get("git", {})
    considered = len(info.get("considered_branches") or info.get("branches") or [])
    lines = [
        f"{info.get('commits', 0)} commits, {info.get('merges', 0)} merges, "
        f"{info.get('authors', 0)} authors",
        f"{len(info.get('branches', []))} voices from {considered} branches "
        f"/ metric {info.get('metric')} / traversal {info.get('traversal')}",
    ]
    span = info.get("span_seconds", 0)
    if span:
        lines.append(f"history spans {span / 86400:.1f} days")
    for ref, owned in (info.get("owned") or {}).items():
        share = owned / max(1, info.get("commits", 1))
        lines.append(f"  {ref:<28} owns {owned:>5} commits ({share:>5.1%})")
    for ref in info.get("dropped_branches") or []:
        lines.append(f"  {ref:<28} no commits of its own -- merged or an ancestor")
    return "\n".join(lines)
