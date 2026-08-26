"""raw CSV -> rows -> selected numeric columns -> integer/byte stream.

The only interesting decision here is normalization. A CPU-percent column lives
in 0..100, a bytes-sent column in 0..10^9; quantizing both to 8 bits with one
global scale would flatten the first into three values. So each channel is
normalized against *its own* observed range before quantizing.

That is a lossy, opinionated choice: it throws away absolute magnitude and keeps
only shape. Which is exactly what section 1 asks for -- "data stops mattering
for its meaning... what matters is its shape".
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
from pathlib import Path

from .rng import seed_from_bytes
from .stream import MAX_VOICES, Stream
from .tempo import Tempo

#: Rows hashed to derive the automatic seed (section 3.4).
SEED_ROWS = 64

_NUMERIC = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


class IngestError(ValueError):
    pass


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def read_rows(
    path: str | Path, delimiter: str | None = None
) -> tuple[list[str], list[list[str]]]:
    """Read a CSV, sniffing the delimiter and tolerating a missing header."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if not text.strip():
        raise IngestError(f"{path} is empty")

    if delimiter is None:
        sample = "\n".join(text.splitlines()[:20])
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","

    rows = [r for r in csv.reader(text.splitlines(), delimiter=delimiter) if r]
    if not rows:
        raise IngestError(f"{path} has no rows")

    first = rows[0]
    textual = sum(1 for cell in first if not _NUMERIC.fullmatch(cell.strip()))
    if textual > len(first) / 2:
        header = [c.strip() or f"col{i}" for i, c in enumerate(first)]
        return header, rows[1:]
    return [f"col{i}" for i in range(len(first))], rows


def _parse_number(cell: str) -> float | None:
    """Pull a number out of a cell, forgiving units and thousands separators."""
    cell = cell.strip()
    if not cell:
        return None
    try:
        return float(cell)
    except ValueError:
        pass
    match = _NUMERIC.search(cell.replace(",", ""))
    if match is None:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def numeric_columns(
    header: list[str], rows: list[list[str]], probe: int = 200
) -> list[int]:
    """Indices of columns that parse as numbers often enough to be usable."""
    keep = []
    sample = rows[:probe]
    for idx in range(len(header)):
        hits = total = 0
        for row in sample:
            if idx >= len(row):
                continue
            total += 1
            if _parse_number(row[idx]) is not None:
                hits += 1
        if total and hits / total >= 0.6:
            keep.append(idx)
    return keep


def monotonicity(values: list[float]) -> float:
    """Fraction of steps going the dominant direction, in [0.5, 1.0].

    A timestamp column scores 1.0: it has enormous variance and no shape
    whatsoever, so quantizing it yields a ramp -- a voice that plays a rising
    glissando once and says nothing about the data. Automatic selection skips
    those. An explicit ``--columns timestamp`` still gets you the ramp, on the
    theory that the author may want exactly that.
    """
    if len(values) < 2:
        return 1.0
    up = sum(1 for a, b in zip(values, values[1:]) if b > a)
    down = sum(1 for a, b in zip(values, values[1:]) if b < a)
    moves = up + down
    if moves == 0:
        return 1.0
    return max(up, down) / moves


# ---------------------------------------------------------------------------
# column selection
# ---------------------------------------------------------------------------
def select_columns(
    header: list[str],
    rows: list[list[str]],
    columns: list[str | int] | None,
    max_voices: int = MAX_VOICES,
) -> list[int]:
    """Resolve the author's ``columns`` request into indices.

    With no request, pick the ``max_voices`` most *variable* numeric columns: a
    column that never changes is a channel that never speaks, and there are only
    eight slots. An explicit request is honoured, however dull.
    """
    if columns:
        picked = []
        for ref in columns:
            if isinstance(ref, int) or str(ref).lstrip("-").isdigit():
                idx = int(ref)
                if not -len(header) <= idx < len(header):
                    raise IngestError(
                        f"column index {idx} out of range (0..{len(header) - 1})"
                    )
                picked.append(idx % len(header))
                continue
            name = str(ref).strip()
            lowered = [h.lower() for h in header]
            if name in header:
                picked.append(header.index(name))
            elif name.lower() in lowered:
                picked.append(lowered.index(name.lower()))
            else:
                raise IngestError(f"no column named {name!r}; have {header}")
        if len(picked) > max_voices:
            raise IngestError(
                f"{len(picked)} columns requested but the voice ceiling is "
                f"{max_voices} (section 3.2 -- raise MAX_VOICES if you mean it)"
            )
        return picked

    candidates = numeric_columns(header, rows)
    if not candidates:
        raise IngestError("no numeric columns found; pass --columns explicitly")

    scored = []
    for idx in candidates:
        values = [
            v
            for v in ((_parse_number(r[idx]) if idx < len(r) else None) for r in rows)
            if v is not None
        ]
        if len(values) < 2:
            continue
        if max(values) == min(values):
            continue  # a flat line is not a voice
        if monotonicity(values) > 0.98:
            continue  # a timestamp or row counter: high variance, zero shape
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        scored.append((var / (abs(mean) + 1.0), idx))
    if not scored:
        raise IngestError("every numeric column is constant; nothing to sonify")
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return sorted(idx for _, idx in scored[:max_voices])


# ---------------------------------------------------------------------------
# quantization
# ---------------------------------------------------------------------------
def quantize(values: list[float], bit_depth: int, log_scale: bool = False) -> list[int]:
    """Per-channel min/max normalization into ``0 .. 2**bit_depth - 1``."""
    ceiling = (1 << bit_depth) - 1
    if not values:
        return []
    if log_scale:
        floor = min(values)
        shift = 1.0 - floor if floor <= 0 else 0.0
        values = [math.log1p(v + shift) for v in values]
    lo, hi = min(values), max(values)
    if hi == lo:
        return [ceiling // 2] * len(values)
    span = hi - lo
    return [max(0, min(ceiling, round((v - lo) / span * ceiling))) for v in values]


def aggregate(values: list[float], window: int, how: str = "mean") -> list[float]:
    """Collapse every ``window`` rows into one value (the ``granularity`` param)."""
    if window <= 1:
        return values
    reducers = {
        "mean": lambda w: sum(w) / len(w),
        "max": max,
        "min": min,
        "sum": sum,
        "first": lambda w: w[0],
        "last": lambda w: w[-1],
        "range": lambda w: max(w) - min(w),
    }
    if how not in reducers:
        raise IngestError(f"unknown aggregation {how!r}; use one of {sorted(reducers)}")
    reduce = reducers[how]
    return [reduce(values[i : i + window]) for i in range(0, len(values), window)]


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------
def ingest_csv(
    path: str | Path,
    columns: list[str | int] | None = None,
    bit_depth: int = 8,
    granularity: int = 1,
    aggregation: str = "mean",
    rate: float | None = None,
    tempo: Tempo | str | dict | None = None,
    log_scale: bool = False,
    limit: int | None = None,
    delimiter: str | None = None,
    trace=None,
) -> Stream:
    """Read a CSV into a Stream.

    ``tempo`` and ``rate`` are two ways of saying the same thing; an explicit
    tempo wins, since it carries swing and metre that a bare rate cannot.

    ``trace``, if given, records the cell-to-byte conversion for each column --
    the raw text, the parsed number, the range it was normalized against, and
    the byte that came out. That chain of four is the single most opaque step in
    the pipeline and the one people most need to see.
    """
    grid = Tempo.parse(tempo) if tempo is not None else Tempo.from_rate(rate or 8.0)
    header, rows = read_rows(path, delimiter)
    if limit:
        rows = rows[:limit]
    if not rows:
        raise IngestError(f"{path} has a header but no data rows")

    indices = select_columns(header, rows, columns)

    channels: list[list[int]] = []
    names: list[str] = []
    conversions: list[dict] = []
    window = getattr(trace, "window", 0) or 0

    for idx in indices:
        raw: list[float] = []
        gaps = 0
        last = 0.0
        for row in rows:
            value = _parse_number(row[idx]) if idx < len(row) else None
            if value is None:
                value = last  # hold the previous sample; a gap is not a zero
                gaps += 1
            last = value
            raw.append(value)

        parsed = list(raw)
        aggregated = aggregate(raw, granularity, aggregation)
        bytes_out = quantize(aggregated, bit_depth, log_scale)
        channels.append(bytes_out)
        names.append(header[idx])

        if window:
            # Recorded per column rather than per frame: the question is "what
            # happened to this column", and a frame-major table would bury it.
            lo, hi = (min(aggregated), max(aggregated)) if aggregated else (0.0, 0.0)
            conversions.append(
                {
                    "name": header[idx],
                    "column_index": idx,
                    "cells": [
                        (row[idx] if idx < len(row) else "") for row in rows[:window]
                    ],
                    "parsed": [round(v, 6) for v in parsed[:window]],
                    "aggregated": [round(v, 6) for v in aggregated[:window]],
                    "bytes": bytes_out[:window],
                    "range": {"low": lo, "high": hi, "log_scale": bool(log_scale)},
                    "unparseable_cells": gaps,
                    "note": (
                        "each channel is normalized against its own range, so "
                        "magnitude is discarded and only shape survives"
                    ),
                }
            )

    stream = Stream(
        names=names,
        data=channels,
        bit_depth=bit_depth,
        tempo=grid,
        meta={
            "source": str(path),
            "source_rows": len(rows),
            "columns": [header[i] for i in indices],
            "column_indices": indices,
            "granularity": granularity,
            "tempo": grid.to_json(),
            "aggregation": aggregation if granularity > 1 else None,
            "log_scale": log_scale,
        },
    )

    if trace is not None:
        stage = trace.add(
            "ingest",
            f"read {Path(path).name}",
            stream,
            params={
                "columns": names,
                "bit_depth": bit_depth,
                "granularity": granularity,
                "aggregation": aggregation if granularity > 1 else None,
                "log_scale": log_scale,
            },
            detail={
                "rows_read": len(rows),
                "columns_available": header,
                "columns_chosen": names,
                "columns_dropped": [
                    name for i, name in enumerate(header) if i not in indices
                ],
                "conversions": conversions,
            },
            note=(
                "cell text -> parsed number -> aggregated -> byte. Columns that "
                "are constant or monotonic are dropped by automatic selection: a "
                "flat line is not a voice, and a timestamp has variance but no shape."
            ),
        )
        del stage

    return stream


def auto_seed(path: str | Path, rows: int = SEED_ROWS) -> int:
    """Seed hashed from the first N rows (section 3.4, ``seed_mode: auto``).

    Header included: two files with identical numbers but different column names
    are different sources, and should sound different.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        head = "".join(line for _, line in zip(range(rows), handle))
    return seed_from_bytes((head or path.name).encode("utf-8"))


def fingerprint(stream: Stream) -> str:
    """Short content hash of a transformed stream -- for labelling renders."""
    digest = hashlib.blake2b(digest_size=8)
    for name, channel in zip(stream.names, stream.data):
        digest.update(name.encode("utf-8"))
        digest.update(bytes(v & 0xFF for v in channel))
    return digest.hexdigest()
