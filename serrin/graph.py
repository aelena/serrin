"""A repository's history as a portable file.

The live adapter needs the repository on this machine and a working `git`. That
is fine for your own work and useless for three things people actually want:
rendering a piece from a repo you do not have a clone of, sharing a piece whose
source travels with it, and putting a graph-based piece in an album that someone
else can open.

So a history can be exported to JSON and ingested from JSON, and the two produce
the same stream -- guaranteed by both going through `build_stream`, not by
carefulness.

**Ownership is baked in at export time.** Deciding which branch owns a commit
needs `rev-list ref --not <others>` against a real repository; recomputing it
from a flat commit list would mean reimplementing reachability, badly. The
exporter already knows the answer, so it writes it down. A hand-written file
without `owner` still loads -- it falls back to `branch`, then to a single voice
-- and `validate` says which happened, because a file that quietly collapses to
one voice looks like a bug in the pipeline rather than a gap in the file.
"""

from __future__ import annotations

import json
from pathlib import Path

from .ingest_git import (
    METRICS,
    TRAVERSALS,
    Commit,
    assign_to_branches,
    auto_seed_repo,
    branches,
    build_stream,
    trunk_of,
)
from .rng import seed_from_bytes
from .stream import MAX_VOICES, Stream
from .tempo import Tempo

FORMAT = "serrin-graph/1"


class GraphError(ValueError):
    pass


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def export_graph(
    repo: str | Path,
    branch_names: list[str] | None = None,
    traversal: str = "chrono",
    with_stats: bool = True,
    limit: int | None = None,
    max_voices: int = MAX_VOICES,
    stamp: str | None = None,
) -> dict:
    """Read a repository and write its history out as a portable document.

    ``with_stats`` costs a ``--numstat`` pass over every commit, which is the
    expensive part of reading a large repo. It is on by default because without
    it the churn, insertions, deletions and files metrics silently read as zero
    from the exported file -- and a metric that produces a flat line without
    saying why is worse than one that refuses.
    """
    path = Path(repo)
    if not (path / ".git").exists() and not (path / "HEAD").exists():
        raise GraphError(f"{path} is not a git repository")

    ranked = branches(path)
    if branch_names:
        available = {name for name, _ in ranked}
        missing = [name for name in branch_names if name not in available]
        if missing:
            raise GraphError(f"no such branch(es): {', '.join(missing)}")
        refs = list(branch_names)
    else:
        refs = [name for name, _ in ranked]
    if len(refs) > max_voices:
        refs = refs[:max_voices]

    owned, timeline = assign_to_branches(path, refs, traversal, with_stats, limit)
    owner_of = {commit.sha: ref for ref, commits in owned.items() for commit in commits}

    return {
        "format": FORMAT,
        "repo": path.resolve().name,
        "exported_at": stamp,
        "traversal": traversal,
        "has_stats": bool(with_stats),
        "branches": [
            {"name": name, "tip": tip} for name, tip in ranked if name in refs
        ],
        "trunk": trunk_of(refs, owned),
        # Baked for the same reason ownership is: the exporter can compute what
        # the file cannot. The repository's automatic seed comes from HEAD's own
        # first commits in git's order, and a flat merged timeline is neither --
        # so without writing it down, a piece would change seed the moment its
        # source switched from the clone to the export.
        "seed": auto_seed_repo(path),
        # Timeline order is preserved, so the file *is* the traversal. Re-sorting
        # on import would silently discard the choice made here.
        "commits": [
            {
                "sha": commit.sha,
                "timestamp": commit.timestamp,
                "parents": commit.parents,
                "author": commit.author,
                "subject": commit.subject,
                "owner": owner_of.get(commit.sha),
                **(
                    {
                        "insertions": commit.insertions,
                        "deletions": commit.deletions,
                        "files": commit.files,
                    }
                    if with_stats
                    else {}
                ),
            }
            for commit in timeline
        ],
    }


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def validate_graph(document: object) -> list[str]:
    """Everything wrong with a history document, in order of how much it matters.

    Returns problems rather than raising, because a file can be usable and still
    worth complaining about -- no ownership, no stats -- and the author needs to
    see all of it at once rather than one exception at a time.
    """
    problems: list[str] = []
    if not isinstance(document, dict):
        return ["the file is not a JSON object"]

    declared = document.get("format")
    if declared and declared != FORMAT:
        problems.append(f"format is {declared!r}, expected {FORMAT!r}")

    commits = document.get("commits")
    if not isinstance(commits, list) or not commits:
        return problems + ["no commits: a history needs a non-empty 'commits' array"]

    seen: set[str] = set()
    missing_sha = missing_time = duplicate = 0
    for index, commit in enumerate(commits):
        if not isinstance(commit, dict):
            problems.append(f"commit {index} is not an object")
            break
        sha = commit.get("sha")
        if not sha or not isinstance(sha, str):
            missing_sha += 1
        elif sha in seen:
            duplicate += 1
        else:
            seen.add(sha)
        stamp = commit.get("timestamp")
        if not isinstance(stamp, (int, float)):
            missing_time += 1

    if missing_sha:
        problems.append(f"{missing_sha} commit(s) have no sha")
    if duplicate:
        problems.append(f"{duplicate} duplicate sha(s)")
    if missing_time:
        # Fatal in practice: the chronological traversal and the `interval`
        # metric both read timestamps, and a missing one poisons the ordering.
        problems.append(f"{missing_time} commit(s) have no numeric timestamp")

    owners = {c.get("owner") or c.get("branch") for c in commits if isinstance(c, dict)}
    owners.discard(None)
    if not owners:
        problems.append(
            "no commit names an owning branch: every commit will land in one "
            "voice. Export with `serrin graph --repo …` to get ownership."
        )
    elif len(owners) > MAX_VOICES:
        problems.append(
            f"{len(owners)} owning branches but the voice ceiling is {MAX_VOICES} "
            "-- the busiest will be kept"
        )

    if not document.get("has_stats") and not any(
        isinstance(c, dict) and "insertions" in c for c in commits
    ):
        problems.append(
            "no per-commit stats: the churn, insertions, deletions and files "
            "metrics will read as flat"
        )
    return problems


def graph_facts(document: dict) -> dict:
    """A summary of a history document, for the studio to display."""
    commits = [c for c in document.get("commits", []) if isinstance(c, dict)]
    owners: dict[str, int] = {}
    for commit in commits:
        owner = commit.get("owner") or commit.get("branch") or "(unowned)"
        owners[owner] = owners.get(owner, 0) + 1
    stamps = [c["timestamp"] for c in commits if isinstance(c.get("timestamp"), (int, float))]
    return {
        "repo": document.get("repo", ""),
        "commits": len(commits),
        "branches": sorted(owners, key=lambda name: -owners[name]),
        "owned": owners,
        "merges": sum(1 for c in commits if len(c.get("parents") or []) > 1),
        "authors": len({c.get("author") for c in commits if c.get("author")}),
        "traversal": document.get("traversal", "chrono"),
        "has_stats": bool(document.get("has_stats"))
        or any("insertions" in c for c in commits),
        "span_seconds": (max(stamps) - min(stamps)) if stamps else 0,
        "exported_at": document.get("exported_at"),
    }


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------
def load_graph(path: str | Path) -> dict:
    target = Path(path)
    if not target.exists():
        raise GraphError(f"no such history file: {target}")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GraphError(f"{target} is not valid JSON: {exc}") from exc
    return document


def ingest_graph(
    path: str | Path,
    metric: str = "hash",
    traversal: str | None = None,
    branch_names: list[str] | None = None,
    bit_depth: int = 8,
    tempo: Tempo | str | dict | None = None,
    rate: float | None = None,
    max_voices: int = MAX_VOICES,
    limit: int | None = None,
    log_scale: bool | None = None,
) -> Stream:
    """Read an exported history into a Stream.

    Produces exactly what the live adapter would from the same repository, since
    both hand off to ``build_stream``. What this cannot do is re-derive ownership
    -- that needed the repository -- so it trusts what the file says.
    """
    document = load_graph(path)
    fatal = [
        problem
        for problem in validate_graph(document)
        if "no commits" in problem or "timestamp" in problem or "format is" in problem
    ]
    if fatal:
        raise GraphError("; ".join(fatal))
    if metric not in METRICS:
        raise GraphError(f"unknown metric {metric!r}; have {', '.join(sorted(METRICS))}")

    order = traversal or document.get("traversal") or "chrono"
    if order not in TRAVERSALS:
        raise GraphError(f"unknown traversal {order!r}; use one of {TRAVERSALS}")

    timeline: list[Commit] = []
    owner_of: dict[str, str] = {}
    for raw in document["commits"]:
        commit = Commit(
            sha=str(raw["sha"]),
            timestamp=int(raw["timestamp"]),
            parents=[str(p) for p in (raw.get("parents") or [])],
            author=str(raw.get("author") or ""),
            subject=str(raw.get("subject") or ""),
            insertions=int(raw.get("insertions") or 0),
            deletions=int(raw.get("deletions") or 0),
            files=int(raw.get("files") or 0),
        )
        timeline.append(commit)
        owner_of[commit.sha] = str(raw.get("owner") or raw.get("branch") or "history")

    if limit:
        timeline = timeline[:limit]

    # The file's own order is the traversal it was exported with; only re-sort
    # when asked for a different one, and only where that is possible without the
    # repository. Topological order is not: it needs the DAG walked, and the file
    # is a flat list -- so it is refused rather than silently ignored.
    if traversal == "chrono":
        timeline.sort(key=lambda c: -c.timestamp)
    elif traversal == "reverse":
        timeline.sort(key=lambda c: c.timestamp)
    elif traversal == "topo" and document.get("traversal") != "topo":
        raise GraphError(
            "topological order cannot be recovered from an exported history -- "
            "re-export with `--traversal topo`, or render from the repository"
        )

    owned: dict[str, list[Commit]] = {}
    for commit in timeline:
        owned.setdefault(owner_of[commit.sha], []).append(commit)

    # Busiest first, so the voice ceiling keeps what carries the piece.
    refs = sorted(owned, key=lambda name: -len(owned[name]))
    if branch_names:
        unknown = [name for name in branch_names if name not in owned]
        if unknown:
            raise GraphError(
                f"no such branch(es) in this history: {', '.join(unknown)}; "
                f"have {', '.join(refs)}"
            )
        refs = list(branch_names)
    if len(refs) > max_voices:
        refs = refs[:max_voices]

    stream = build_stream(
        owned=owned,
        timeline=timeline,
        refs=refs,
        metric=metric,
        traversal=order,
        bit_depth=bit_depth,
        tempo=tempo,
        rate=rate,
        log_scale=log_scale,
        source=str(path),
        extra={
            "trunk": document.get("trunk") or (refs[0] if refs else ""),
            "from_file": True,
            "repo": document.get("repo", ""),
        },
    )
    stream.meta["source_kind"] = "graph"
    return stream


def auto_seed_graph(path: str | Path, count: int = 64) -> int:
    """The seed this history should render with.

    An exported file carries the seed its repository would have produced, so a
    piece keeps the same seed when its source switches from the clone to the
    export. That has to be *recorded* rather than recomputed: the repository
    seeds from HEAD's own first commits in git's order, and a timeline merged
    across branches is a different list in a different order.

    A hand-written history has no repository to agree with, so it falls back to
    hashing its own first commit ids -- same window, same rule as the CSV
    adapter: the seed changes when the head of the data changes and not
    otherwise.
    """
    document = load_graph(path)
    baked = document.get("seed")
    if isinstance(baked, int) and not isinstance(baked, bool):
        return baked & ((1 << 64) - 1)
    shas = [
        str(commit.get("sha", ""))
        for commit in document.get("commits", [])[:count]
        if isinstance(commit, dict)
    ]
    return seed_from_bytes(("\n".join(shas) or str(Path(path).name)).encode("utf-8"))


def describe_graph(stream: Stream) -> str:
    info = stream.meta.get("git", {})
    lines = [
        f"{info.get('commits', 0)} commits from a history file"
        + (f" ({info.get('repo')})" if info.get("repo") else ""),
        f"{len(info.get('branches', []))} voices / metric {info.get('metric')} "
        f"/ traversal {info.get('traversal')}",
    ]
    for ref, count in (info.get("owned") or {}).items():
        share = count / max(1, info.get("commits", 1))
        lines.append(f"  {ref:<28} owns {count:>5} commits ({share:>5.1%})")
    return "\n".join(lines)


__all__ = [
    "FORMAT",
    "GraphError",
    "auto_seed_graph",
    "describe_graph",
    "export_graph",
    "graph_facts",
    "ingest_graph",
    "load_graph",
    "validate_graph",
]
