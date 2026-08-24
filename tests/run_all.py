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

    completed = subprocess.run(
        ["node", str(ROOT / "tests" / "test_runtime.mjs")],
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    return completed.returncode == 0


def main() -> int:
    ok = python_suite()
    ok = js_suite() and ok
    print()
    print("all suites passed" if ok else "FAILURES -- see above", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
