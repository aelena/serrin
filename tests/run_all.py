"""Run both test suites, and make them check each other.

The Python pipeline and the JS runtime independently implement two things that
must agree: the voice-activation curve (section 5.1.1) and the envelope
interpolation. If they drift apart, an offline preview stops predicting what the
browser actually plays -- and nothing would fail, the piece would just be subtly
different in the two places. So the Python numbers are computed here and handed
to the Node suite to assert against.

    python tests/run_all.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from serrin.envelope import active_voice_count  # noqa: E402
from serrin.tempo import NOTE_FRACTIONS, Tempo  # noqa: E402


def banner(title: str) -> None:
    """Flushed, because the node subprocess writes past Python's buffer."""
    rule = "=" * 70
    print(f"\n{rule}\n{title}\n{rule}", flush=True)


def python_suite() -> bool:
    banner("python pipeline")
    loader = unittest.TestLoader()
    # No top_level_dir: tests/ is not a package, and passing one makes
    # unittest insist that it is.
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return result.wasSuccessful()


def js_suite() -> bool:
    banner("js runtime")

    if shutil.which("node") is None:
        print("node not found on PATH -- skipping the runtime suite", flush=True)
        return True

    audio = ROOT / "out" / "stream_audio.json"
    if not audio.exists():
        print("out/stream_audio.json is missing. Render it first:", flush=True)
        print("  python -m serrin render -i data/monitoring.csv -c presets/gritty_01.json")
        return False

    # The cross-check payload, computed by Python for the Node suite to assert
    # against: voice activation, and the tempo grid including swung onsets.
    gates = [[round(i / 20, 3), active_voice_count(i / 20, 8)] for i in range(21)]
    grids = []
    for bpm, subdivision, swing, beats in (
        (120, 16, 0.0, 4),
        (96, 8, 0.25, 4),
        (140, 16, 1.0, 4),
        (60, 4, 0.5, 3),
    ):
        tempo = Tempo(bpm=bpm, subdivision=subdivision, swing=swing, beats_per_bar=beats)
        grids.append(
            {
                "bpm": bpm,
                "subdivision": subdivision,
                "swing": swing,
                "beats_per_bar": beats,
                "rate": tempo.rate,
                "onsets": [tempo.onset(i) for i in range(16)],
                "notes": {note: tempo.note_seconds(note) for note in NOTE_FRACTIONS},
            }
        )
    env = {
        **os.environ,
        "SERRIN_PY_GATES": json.dumps(gates),
        "SERRIN_PY_TEMPO": json.dumps(grids),
        "SERRIN_PY_NOTES": json.dumps(NOTE_FRACTIONS),
    }

    # Every .mjs in tests/ is a suite. Discovered rather than listed so a new
    # one cannot be added and then quietly never run.
    suites = sorted((ROOT / "tests").glob("test_*.mjs"))
    if not suites:
        print("no node suites found", flush=True)
        return True

    ok = True
    for suite in suites:
        print(f"\n-- {suite.name}", flush=True)
        completed = subprocess.run(
            ["node", str(suite)], cwd=str(ROOT), env=env, check=False
        )
        ok = completed.returncode == 0 and ok
    return ok


def cross_language_check() -> bool:
    """The seam: the browser writes a session, Python re-renders from it.

    Each suite tests its own half of the format thoroughly, which leaves the
    boundary between them untested -- and a format goes wrong precisely there.
    A field the browser writes under one name and Python reads under another
    fails no unit test and produces a piece that is nearly, but not quite, the
    one that was saved.
    """
    banner("cross-language session round trip")

    if shutil.which("node") is None:
        print("node not found on PATH -- skipping", flush=True)
        return True
    if not (ROOT / "out" / "stream_audio.json").exists():
        print("no render in out/ -- skipping", flush=True)
        return True

    from serrin.ingest import fingerprint, ingest_csv  # noqa: PLC0415
    from serrin.session import Session  # noqa: PLC0415

    target = ROOT / "out" / "roundtrip.session.json"
    emit = subprocess.run(
        ["node", str(ROOT / "tests" / "session_fixture.mjs"), str(ROOT), str(target)],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    if emit.returncode != 0:
        print("the fixture failed to write:", flush=True)
        print(emit.stderr, flush=True)
        return False

    try:
        session = Session.load(target)
    except Exception as exc:  # noqa: BLE001 -- the point is to report anything
        print(f"python could not read the browser's session: {exc}", flush=True)
        return False

    checks: list[tuple[str, bool, str]] = []

    grid = session.tempo()
    checks.append(
        ("the tempo set by ear survived", grid is not None and abs(grid.bpm - 104) < 1e-9, f"{grid}")
    )
    checks.append(("swing survived", grid is not None and abs(grid.swing - 0.22) < 1e-9, f"{grid}"))
    checks.append(("the chain loads", bool(session.chain().slots), session.preset.get("name", "")))
    checks.append(
        (
            "the hand-drawn envelope survived",
            len((session.preset.get("envelope") or {}).get("points") or []) > 8,
            "envelope points",
        )
    )
    checks.append(
        (
            "the runtime block came through whole",
            {"audio", "visual", "keyboard", "transport", "voices", "envelope"}
            <= set(session.runtime),
            f"{sorted(session.runtime)}",
        )
    )

    # And the property the format exists for.
    source = ROOT / session.path
    if not source.exists():
        source = Path(session.path)
    if source.exists():
        stream = ingest_csv(source, **session.ingest_kwargs())
        rendered = fingerprint(session.chain().apply(stream, source=source))
        checks.append(
            (
                "re-rendering reproduces the saved fingerprint",
                rendered == session.fingerprint,
                f"{rendered} vs {session.fingerprint}",
            )
        )

    ok = True
    for name, result, detail in checks:
        print(f"  {'ok  ' if result else 'FAIL'} {name}" + ("" if result else f"  [{detail}]"), flush=True)
        ok = ok and result
    return ok


def main() -> int:
    ok = python_suite()
    ok = js_suite() and ok
    ok = cross_language_check() and ok
    print()
    print("all suites passed" if ok else "FAILURES -- see above", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
