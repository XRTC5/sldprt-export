"""Phase 2: sweep every payload in a .SLDPRT for saved tessellation.

The project hinges on one fact: does SolidWorks save a display mesh we can
lift, or only Parasolid B-rep that would have to be tessellated from
scratch? This answers it across the whole file rather than one blob at a
time.

A vertex array is a long run of finite floats that share one tight bounding
box. Feature-tree data, matrices and handles do not look like that. Runs are
scored by length and by whether their bounding box is a believable part
size, then ranked.

SolidWorks stores metres, so 50 mm reads as 0.05.

Read-only.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.container import extract  # noqa: E402

MIN_MAG = 1e-6
MAX_MAG = 10.0

# A believable part: bigger than a grain of rice, smaller than a bus.
BBOX_MIN_MM = 0.5
BBOX_MAX_MM = 5000.0


def plausible(v: float) -> bool:
    if v != v or v in (float("inf"), float("-inf")):
        return False
    m = abs(v)
    return m == 0.0 or (MIN_MAG <= m <= MAX_MAG)


def scan_runs(data: bytes, fmt: str, width: int, min_run: int):
    """Yield (byte_offset, values) for maximal runs of plausible floats."""
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
                    yield align + start * width, values[start:i]
                start = None
        if start is not None and len(values) - start >= min_run:
            yield align + start * width, values[start:]


def bbox_mm(chunk) -> tuple[float, float, float] | None:
    n = len(chunk) // 3
    if n < 4:
        return None
    xs = chunk[0 : n * 3 : 3]
    ys = chunk[1 : n * 3 : 3]
    zs = chunk[2 : n * 3 : 3]
    return (
        (max(xs) - min(xs)) * 1000.0,
        (max(ys) - min(ys)) * 1000.0,
        (max(zs) - min(zs)) * 1000.0,
    )


def believable(box) -> bool:
    """A real part has three non-degenerate dimensions of sane magnitude."""
    if box is None:
        return False
    nonzero = [d for d in box if d > 1e-6]
    if len(nonzero) < 2:
        return False
    return all(BBOX_MIN_MM <= d <= BBOX_MAX_MM for d in nonzero)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--min-run", type=int, default=300)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--double", action="store_true")
    parser.add_argument("--min-payload", type=int, default=1024)
    args = parser.parse_args()

    fmt, width = ("d", 8) if args.double else ("f", 4)
    records = extract(args.file, min_out=args.min_payload)
    print(f"=== {args.file.name} — {len(records)} payloads, scanning {'float64' if args.double else 'float32'} ===\n")

    candidates = []
    for rec in records:
        for off, chunk in scan_runs(rec.data, fmt, width, args.min_run):
            box = bbox_mm(chunk)
            candidates.append(
                {
                    "rec": rec,
                    "offset": off,
                    "count": len(chunk),
                    "bbox": box,
                    "good": believable(box),
                }
            )

    candidates.sort(key=lambda c: (c["good"], c["count"]), reverse=True)

    if not candidates:
        print("  no float runs found at this threshold")
        return 0

    good = sum(1 for c in candidates if c["good"])
    print(f"  {len(candidates)} runs, {good} with a believable part bounding box\n")

    for c in candidates[: args.top]:
        box = c["bbox"]
        box_s = f"{box[0]:8.2f} x {box[1]:8.2f} x {box[2]:8.2f} mm" if box else "n/a"
        mark = "**" if c["good"] else "  "
        tris = c["count"] // 9
        print(
            f"  {mark} {c['rec'].kind:<24} payload 0x{c['rec'].offset:08x} +0x{c['offset']:06x}"
            f"  {c['count']:>8,} vals  bbox {box_s}"
            + (f"  -> {tris:,} tris if flat" if c["count"] % 9 == 0 else "")
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
