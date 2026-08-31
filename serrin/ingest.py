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
#: Lines that are commentary rather than data. `#` is near-universal; the others
#: turn up in instrument and simulator exports.
_COMMENT_PREFIXES = ("#", "//", ";;", "!")

#: How far into a file to look for the real header before giving up.
_HEADER_SEARCH_LINES = 200

#: How many agreeing rows are enough to call something the table.
#:
#: Not a detail. Measuring each candidate's run all the way to the end of the
#: file meant up to 200 start lines x 4 delimiters x every remaining line --
#: which turned a 140k-row export into a 39-second parse. Fifty agreeing rows
#: already beat any preamble line (one or two fields) or footer line (one), so
#: counting further buys nothing.
_ENOUGH_AGREEMENT = 50


def _split(line: str, delimiter: str) -> list[str]:
    return next(iter(csv.reader([line], delimiter=delimiter)), [])


def find_table(
    lines: list[str], delimiter: str | None = None
) -> tuple[str, int, int]:
    """Locate the delimiter, the header line and where the data starts.

    Real exports do not begin with a header row. PVGIS writes eight lines of
    tab-separated metadata and then ``time,G(i),H_sun,…``; Solargis writes
    forty-one ``#`` comment lines and then a semicolon-separated table; both put
    prose at the end. Assuming line one is the header turned the first into a
    two-column file called "Latitude (decimal degrees)" and the second into one
    column of English.

    This is parsing, not cleaning. Serrin deliberately does not repair values --
    a constant column stays constant and gets dropped, a bad number stays bad --
    but a metadata preamble is part of the file *format*, and refusing to find
    the table is failing to read the file rather than declining to fix it.

    The header is the line that starts the longest run of rows that all split
    into the same number of fields. That definition is what makes it robust: a
    preamble line splits into one or two fields and a footer line into one, so
    neither can win against a thousand rows of six.
    """
    candidates = [delimiter] if delimiter else [",", ";", "\t", "|"]
    # (agreeing rows, delimiter, header index, field count)
    best: tuple[int, str, int, int] = (0, ",", 0, 0)

    for candidate in candidates:
        index = 0
        while index < min(len(lines), _HEADER_SEARCH_LINES):
            line = lines[index]
            if not line.strip() or line.lstrip().startswith(_COMMENT_PREFIXES):
                index += 1
                continue
            width = len(_split(line, candidate))
            if width < 2:
                index += 1
                continue
            # How many following lines agree on the width? Blanks and comments in
            # the middle do not break the run; a different width does.
            run = 0
            probe = index + 1
            while probe < len(lines) and run < _ENOUGH_AGREEMENT:
                following = lines[probe]
                if not following.strip() or following.lstrip().startswith(_COMMENT_PREFIXES):
                    probe += 1
                    continue
                if len(_split(following, candidate)) != width:
                    break
                run += 1
                probe += 1
            # Ties broken by field count: a preamble of tab-separated key/value
            # pairs and a real comma table can both reach the cap, and the wider
            # one is the table.
            if (run, width) > (best[0], best[3] if len(best) > 3 else 0):
                best = (run, candidate, index, width)
            index += 1

    run, chosen, header_index, _ = best
    if run == 0:
        # Nothing looks like a table. Fall back to the first non-comment line so
        # the caller still gets a shot, and let column selection complain.
        for index, line in enumerate(lines):
            if line.strip() and not line.lstrip().startswith(_COMMENT_PREFIXES):
                return chosen, index, index + 1
        return chosen, 0, 1
    return chosen, header_index, header_index + 1


def read_rows(
    path: str | Path,
    delimiter: str | None = None,
    report: dict | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Read a CSV, finding the table wherever it starts.

    Returns the header and only the rows that match its width. Trailing prose --
    which PVGIS, Solargis and most simulators append -- is dropped rather than
    read as a one-column row that would then be held forward as data.

    ``report``, if given, is filled with what parsing decided: the delimiter, the
    header line number, how many lines were skipped before it and how many rows
    were dropped after. Skipping a metadata preamble is a judgement, and a
    judgement made silently is indistinguishable from a bug -- so the caller can
    show its work and the author can see immediately if it guessed wrong.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return rows_from_text(text, delimiter, report, label=str(path))


def rows_from_text(
    text: str,
    delimiter: str | None = None,
    report: dict | None = None,
    label: str = "the file",
) -> tuple[list[str], list[list[str]]]:
    """The body of :func:`read_rows`, working on contents rather than a path.

    Split out so an upload can be checked *before* it is written into a piece
    folder. A piece that points at a file nobody can parse is a worse failure
    than a refused upload: the refusal names the problem while the author still
    has the file in front of them.
    """
    if not text.strip():
        raise IngestError(f"{label} is empty")

    lines = text.splitlines()
    chosen, header_index, data_start = find_table(lines, delimiter)

    header_cells = _split(lines[header_index], chosen)
    textual = sum(1 for cell in header_cells if not _NUMERIC.fullmatch(cell.strip()))
    if textual > len(header_cells) / 2:
        header = [cell.strip() or f"col{i}" for i, cell in enumerate(header_cells)]
    else:
        # The table starts straight into numbers: no names to use.
        header = [f"col{i}" for i in range(len(header_cells))]
        data_start = header_index

    width = len(header)
    rows: list[list[str]] = []
    skipped_blank = skipped_comment = dropped_width = 0
    for line in lines[data_start:]:
        if not line.strip():
            skipped_blank += 1
            continue
        if line.lstrip().startswith(_COMMENT_PREFIXES):
            skipped_comment += 1
            continue
        cells = _split(line, chosen)
        if len(cells) != width:
            # A footer, a stray note, or a second table. Skipped rather than
            # padded: a short row padded with blanks becomes held-forward values
            # that look like real, very flat data.
            dropped_width += 1
            continue
        rows.append(cells)

    if report is not None:
        report.update(
            {
                "delimiter": chosen,
                "header_line": header_index + 1,
                "preamble_lines": header_index,
                "columns": width,
                "data_rows": len(rows),
                "dropped_rows": dropped_width,
                "skipped_blank": skipped_blank,
                "skipped_comment": skipped_comment,
                "total_lines": len(lines),
                "named_header": not all(
                    name.startswith("col") for name in header
                ),
            }
        )

    if not rows:
        raise IngestError(
            f"{label}: found a header at line {header_index + 1} "
            f"({width} columns, delimiter {chosen!r}) but no data rows under it"
        )
    return header, rows


#: `1,234` or `1,234,567.89` -- commas grouping thousands.
_THOUSANDS = re.compile(r"^[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
#: `12,5` -- a comma where the rest of the world puts a decimal point.
_DECIMAL_COMMA = re.compile(r"^[-+]?\d+,\d+$")


def _parse_number(cell: str) -> float | None:
    """Pull a number out of a cell, forgiving units and both comma conventions.

    The comma is the trap. Stripping every comma as a thousands separator turns
    the European ``12,5`` into ``125`` -- a tenfold error, silent, in a column
    that still looks perfectly plausible afterwards. So the two cases are
    distinguished by shape: commas every three digits are grouping, a single
    comma between digits is a decimal point.

    Anything else still gets the forgiving treatment -- ``45 ms`` and ``~3.2 GB``
    read as numbers, because a units suffix is a labelling habit rather than a
    different value.
    """
    cell = cell.strip()
    if not cell:
        return None
    try:
        return float(cell)
    except ValueError:
        pass

    if _THOUSANDS.match(cell):
        return float(cell.replace(",", ""))
    if _DECIMAL_COMMA.match(cell):
        return float(cell.replace(",", "."))

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


def describe_table(path: str | Path) -> str:
    """One paragraph on how a file was parsed, for `inspect` and the console."""
    report: dict = {}
    header, _ = read_rows(path, report=report)
    delimiter = "tab" if report["delimiter"] == "\t" else report["delimiter"]
    lines = [
        f"read as {report['columns']} columns x {report['data_rows']} rows, "
        f"delimiter {delimiter!r}, header on line {report['header_line']}",
    ]
    if report["preamble_lines"]:
        lines.append(
            f"  skipped {report['preamble_lines']} line(s) above it "
            "(metadata preamble)"
        )
    if report["dropped_rows"]:
        lines.append(
            f"  dropped {report['dropped_rows']} line(s) below the table that did "
            f"not have {report['columns']} fields (usually a footer)"
        )
    if report["skipped_comment"]:
        lines.append(f"  skipped {report['skipped_comment']} comment line(s) inside it")
    if not report["named_header"]:
        lines.append("  no column names in the file, so they are col0, col1 ...")
    lines.append("  columns: " + ", ".join(header))
    return "\n".join(lines)


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
    table: dict = {}
    header, rows = read_rows(path, delimiter, report=table)
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
                # What parsing decided, so a render records how it found the
                # table rather than leaving it to be re-derived by hand.
                "table": table,
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
