"""Phase 2: locate tessellation inside an inflated payload.

A vertex array is the easiest thing in a binary file to find blind: it is a
long uninterrupted run of finite floating-point values that all fall inside
one small bounding box. Feature-tree data, indices and handles do not look
like that.

SolidWorks and Parasolid store lengths in METRES, so a 50 mm feature reads
as 0.05. The default range spans one micron to ten metres, which covers any
real part while rejecting the huge and denormal values that random bytes
produce.

Reports the longest runs, their bounding box in mm, and whether the run
length divides cleanly by 3 (vertices) or 9 (flat triangle triples).
"""

from __future__ import annotations

import argparse
import math
import struct
from pathlib import Path

# Metres. One micron to ten metres covers anything anyone models.
MIN_MAG = 1e-6
MAX_MAG = 10.0


def plausible(value: float) -> bool:
    if math.isnan(value) or math.isinf(value):
        return False
    mag = abs(value)
    return mag == 0.0 or (MIN_MAG <= mag <= MAX_MAG)


def find_runs(data: bytes, fmt: str, width: int, min_run: int) -> list[dict]:
    """Find maximal runs of plausible floats at every alignment."""
    runs: list[dict] = []
    for align in range(width):
        usable = len(data) - align
        usable -= usable % width
        if usable < min_run * width:
            continue
        values = struct.unpack_from(f"<{usable // width}{fmt}", data, align)

        start = None
        for i, v in enumerate(values):
            if plausible(v):
                if start is None:
                    start = i
            else:
                if start is not None and i - start >= min_run:
                    runs.append(_summarise(values, start, i, align, width))
                start = None
        if start is not None and len(values) - start >= min_run:
            runs.append(_summarise(values, start, len(values), align, width))

    runs.sort(key=lambda r: r["count"], reverse=True)
    return runs


def _summarise(values, start: int, end: int, align: int, width: int) -> dict:
    chunk = values[start:end]
    count = len(chunk)
    nonzero = [v for v in chunk if v != 0.0]

    # Interpret as XYZ triples to get a bounding box.
    triples = count // 3
    bbox = None
    if triples:
        xs = chunk[0 : triples * 3 : 3]
        ys = chunk[1 : triples * 3 : 3]
        zs = chunk[2 : triples * 3 : 3]
        bbox = (
            (max(xs) - min(xs)) * 1000.0,
            (max(ys) - min(ys)) * 1000.0,
            (max(zs) - min(zs)) * 1000.0,
        )

    return {
        "offset": align + start * width,
        "count": count,
        "bytes": count * width,
        "align": align,
        "div3": count % 3 == 0,
        "div9": count % 9 == 0,
        "bbox_mm": bbox,
        "span": (min(nonzero) if nonzero else 0.0, max(nonzero) if nonzero else 0.0),
    }


def report(path: Path, min_run: int, top: int, use_double: bool) -> None:
    data = path.read_bytes()
    fmt, width, label = ("d", 8, "float64") if use_double else ("f", 4, "float32")
    runs = find_runs(data, fmt, width, min_run)

    print(f"\n=== {path.name} ({len(data):,} bytes) as {label} ===")
    if not runs:
        print(f"  no runs of >= {min_run} plausible values")
        return

    for r in runs[:top]:
        bbox = r["bbox_mm"]
        bbox_s = (
            f"{bbox[0]:9.2f} x {bbox[1]:9.2f} x {bbox[2]:9.2f} mm" if bbox else "n/a"
        )
        flags = []
        if r["div3"]:
            flags.append(f"/3={r['count'] // 3}")
        if r["div9"]:
            flags.append(f"/9={r['count'] // 9}")
        print(
            f"  0x{r['offset']:06x}  {r['count']:>7,} vals  {r['bytes']:>8,} B  "
            f"bbox {bbox_s}  {' '.join(flags)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--min-run", type=int, default=90, help="minimum values in a run")
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--double", action="store_true", help="scan float64 instead of float32")
    args = parser.parse_args()

    for path in args.files:
        if path.exists():
            report(path, args.min_run, args.top, args.double)
        else:
            print(f"missing: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
