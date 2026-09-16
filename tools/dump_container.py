"""Phase 1: dump the OLE container structure of .SLDPRT files.

Evidence before architecture. This tool makes no assumptions about what is
inside a SolidWorks part file -- it lists every stream, measures it, and
reports the signals that tell us how to decode it later:

  * entropy      -- ~8.0 means compressed or encrypted, low means structured
  * zlib magic   -- 0x78 0x01/0x9C/0xDA at offset 0 means we are through
  * float ratio  -- fraction of float32 words that look like plausible
                    mm-scale model coordinates, our tessellation smoking gun

Read-only. Never writes to the source directory.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from collections import Counter
from pathlib import Path

import olefile

ZLIB_HEADERS = {b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda"}

# A plausible model coordinate in mm: not zero, not astronomically large.
# Real parts live between microns and a few metres.
COORD_MIN = 1e-4
COORD_MAX = 1e4


def shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def float_ratio(data: bytes) -> float:
    """Fraction of 4-byte words that decode as plausible model coordinates.

    Tessellation vertex arrays are dense runs of float32, so a stream holding
    one scores high here while a stream of text or indices scores near zero.
    """
    usable = len(data) - (len(data) % 4)
    if usable < 64:
        return 0.0
    sample = data[:usable]
    plausible = 0
    total = usable // 4
    for (value,) in struct.iter_unpack("<f", sample):
        if value == 0.0:
            plausible += 1  # zeros are common and legitimate in vertex data
            continue
        mag = abs(value)
        if COORD_MIN <= mag <= COORD_MAX and not math.isnan(value):
            plausible += 1
    return plausible / total


def hexdump(data: bytes, length: int = 48) -> str:
    chunk = data[:length]
    hex_part = " ".join(f"{b:02x}" for b in chunk)
    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    return f"{hex_part}  |{ascii_part}|"


def describe_stream(ole: olefile.OleFileIO, path: list[str]) -> dict:
    name = "/".join(path)
    size = ole.get_size(name)
    with ole.openstream(name) as fh:
        # Cap the read: a 33 MB file can hold a stream we do not want resident.
        head = fh.read(min(size, 1 << 20))

    return {
        "name": name,
        "size": size,
        "entropy": round(shannon_entropy(head[:65536]), 3),
        "zlib": head[:2] in ZLIB_HEADERS,
        "float_ratio": round(float_ratio(head[:65536]), 3),
        "head": hexdump(head),
    }


def dump(path: Path) -> dict:
    if not olefile.isOleFile(str(path)):
        return {"file": path.name, "error": "not an OLE compound file"}

    ole = olefile.OleFileIO(str(path))
    try:
        streams = [describe_stream(ole, entry) for entry in ole.listdir(streams=True)]
    finally:
        ole.close()

    streams.sort(key=lambda s: s["size"], reverse=True)
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "stream_count": len(streams),
        "streams": streams,
    }


def render(report: dict) -> str:
    if "error" in report:
        return f"\n=== {report['file']} ===\n  ERROR: {report['error']}\n"

    lines = [
        f"\n=== {report['file']} ===",
        f"  {report['bytes']:,} bytes - {report['stream_count']} streams",
        f"  {'SIZE':>12}  {'ENT':>5}  {'FLT':>5}  ZLIB  NAME",
    ]
    for s in report["streams"]:
        lines.append(
            f"  {s['size']:>12,}  {s['entropy']:>5.2f}  {s['float_ratio']:>5.2f}"
            f"  {'yes ' if s['zlib'] else '  - '}  {s['name']}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--detail", action="store_true", help="show stream head bytes")
    args = parser.parse_args()

    reports = []
    for path in args.files:
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            continue
        report = dump(path)
        reports.append(report)
        print(render(report))
        if args.detail and "streams" in report:
            print()
            for s in report["streams"]:
                print(f"  {s['name']}")
                print(f"    {s['head']}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
