"""Build a throwaway repository with the shapes the git adapter cares about.

Needed because the interesting properties -- several branches, merges, a
stale branch with three commits, authors changing, bursts of activity separated
by quiet days -- do not exist in a fresh repo, and testing against whatever repo
happens to be checked out would make the tests depend on the developer's own
history.

Deterministic: same seed, same repo, same hashes. That last part matters more
than it looks, because the `hash` metric reads commit hashes directly, so a
non-reproducible fixture would make the adapter look non-deterministic.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serrin.rng import Rng  # noqa: E402

#: A fixed instant, so commit hashes are stable across runs. 2026-01-01 00:00 UTC.
EPOCH = 1767225600

AUTHORS = [
    ("Ada Lovelace", "ada@example.invalid"),
    ("Grace Hopper", "grace@example.invalid"),
    ("Alan Turing", "alan@example.invalid"),
]


def _run(repo: Path, *args: str, env: dict | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {completed.stderr.strip()}")
    return completed.stdout


def _commit(repo: Path, message: str, when: int, author: tuple[str, str], files: dict[str, str]):
    import os

    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _run(repo, "add", "-A")

    stamp = f"{when} +0000"
    env = {
        **os.environ,
        # Both dates pinned, and both identities: git will otherwise reach for
        # the machine's config and the hashes stop being reproducible.
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_NAME": author[0],
        "GIT_COMMITTER_EMAIL": author[1],
        "GIT_COMMITTER_DATE": stamp,
    }
    _run(repo, "commit", "-q", "--no-gpg-sign", "-m", message, env=env)


def build(target: str | Path, seed: int = 4242) -> Path:
    """Create the fixture repo at ``target``. Returns the path."""
    repo = Path(target)
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q")
    _run(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _run(repo, "config", "commit.gpgsign", "false")
    _run(repo, "config", "user.name", "Fixture")
    _run(repo, "config", "user.email", "fixture@example.invalid")

    rng = Rng(seed)
    clock = EPOCH

    # -- trunk: a long, busy history with bursts and quiet stretches ---------
    for i in range(24):
        # Bursts: most commits minutes apart, occasionally a gap of days. This
        # is the shape the `interval` metric exists to read.
        clock += rng.between(120, 900) if rng.chance(0.75) else rng.between(60000, 400000)
        _commit(
            repo,
            f"trunk {i}",
            clock,
            AUTHORS[i % len(AUTHORS)],
            {"main.txt": "x" * rng.between(10, 400), f"mod/{i % 4}.txt": "y" * rng.between(5, 200)},
        )

    # -- a feature branch that gets merged back ------------------------------
    _run(repo, "checkout", "-q", "-b", "feature/long")
    for i in range(9):
        clock += rng.between(300, 4000)
        _commit(repo, f"feature {i}", clock, AUTHORS[1], {"feature.txt": "f" * rng.between(20, 600)})
    _run(repo, "checkout", "-q", "main")
    clock += 3600
    import os

    stamp = f"{clock} +0000"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": AUTHORS[0][0],
        "GIT_AUTHOR_EMAIL": AUTHORS[0][1],
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_NAME": AUTHORS[0][0],
        "GIT_COMMITTER_EMAIL": AUTHORS[0][1],
        "GIT_COMMITTER_DATE": stamp,
    }
    # --no-ff so the merge commit really exists: a fast-forward would leave no
    # multi-parent commit, and merges are half of what makes a graph musical.
    _run(repo, "merge", "-q", "--no-ff", "--no-gpg-sign", "-m", "merge feature/long", "feature/long", env=env)

    # -- a stale branch: three commits and then nothing ----------------------
    _run(repo, "checkout", "-q", "-b", "spike/abandoned", f"HEAD~{6}")
    for i in range(3):
        clock += rng.between(200, 1200)
        _commit(repo, f"spike {i}", clock, AUTHORS[2], {"spike.txt": "s" * rng.between(5, 80)})

    # -- an active branch, still open ---------------------------------------
    _run(repo, "checkout", "-q", "main")
    _run(repo, "checkout", "-q", "-b", "work/current")
    for i in range(14):
        clock += rng.between(150, 2500)
        _commit(repo, f"work {i}", clock, AUTHORS[i % 2], {"work.txt": "w" * rng.between(10, 900)})

    # -- trunk keeps moving after the branches exist -------------------------
    #
    # Not decoration. If main stops here it is an *ancestor* of work/current,
    # which means it owns no commits of its own and correctly drops out as a
    # voice -- so the fixture would silently stop exercising the multi-voice
    # case it exists to test. Real trunks keep advancing while branches live.
    _run(repo, "checkout", "-q", "main")
    for i in range(7):
        clock += rng.between(400, 60000)
        _commit(repo, f"trunk late {i}", clock, AUTHORS[i % 3], {"main.txt": "z" * rng.between(30, 500)})

    return repo


if __name__ == "__main__":
    import tempfile

    destination = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="serrin-fixture-")
    built = build(destination)
    print(f"built {built}")
    print(subprocess.run(["git", "-C", str(built), "log", "--oneline", "--graph", "--all"],
                         capture_output=True, text=True).stdout[:2000])
