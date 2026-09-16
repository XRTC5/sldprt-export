"""Phase 1b: identify an unknown container by its internal signals.

dump_container.py assumed OLE/CFBF and was wrong for these files, so this
tool assumes nothing. It looks for the fingerprints any real container
leaves behind: embedded compression, known magic numbers, printable name
tables, and the entropy profile across the file.

Read-only.
"""

from __future__ import annotations

import argparse
import math
import re
import zlib
from collections import Counter
from pathlib import Path

MAGICS = {
    b"\x89PNG\r\n\x1a\n": "PNG",
    b"PK\x03\x04": "ZIP local header",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1": "OLE/CFBF",
    b"\xff\xd8\xff": "JPEG",
    b"BM": "BMP",
    b"\x1f\x8b\x08": "gzip",
    b"SQLite format 3\x00": "SQLite",
    b"**TRANSMIT**": "Parasolid transmit",
    b"ISO-10303-21": "STEP",
}

ZLIB_HEADERS = [b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda"]

PRINTABLE = re.compile(rb"[ -~]{6,}")


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def find_magics(data: bytes) -> list[tuple[int, str]]:
    hits = []
    for magic, label in MAGICS.items():
        start = 0
        while True:
            idx = data.find(magic, start)
            if idx < 0:
                break
            hits.append((idx, label))
            start = idx + 1
            if len(hits) > 200:
                return hits
    return sorted(hits)


def find_zlib(data: bytes, limit: int = 40) -> list[tuple[int, int, int]]:
    """Locate offsets where a real zlib stream decompresses.

    A two-byte header match is cheap and noisy, so every candidate is
    verified by actually inflating it. Returns (offset, in_size, out_size).
    """
    found = []
    for header in ZLIB_HEADERS:
        start = 0
        while len(found) < limit:
            idx = data.find(header, start)
            if idx < 0:
                break
            start = idx + 1
            obj = zlib.decompressobj()
            try:
                out = obj.decompress(data[idx : idx + (1 << 22)])
            except zlib.error:
                continue
            if len(out) >= 64:
                consumed = len(data[idx : idx + (1 << 22)]) - len(obj.unused_data)
                found.append((idx, consumed, len(out)))
    return sorted(found)


def entropy_profile(data: bytes, buckets: int = 16) -> list[float]:
    step = max(1, len(data) // buckets)
    return [round(entropy(data[i : i + min(step, 65536)]), 2) for i in range(0, len(data), step)][:buckets]


def strings(data: bytes, min_len: int = 6, limit: int = 40) -> list[str]:
    seen: list[str] = []
    for match in PRINTABLE.finditer(data):
        text = match.group().decode("ascii", "ignore")
        if len(text) >= min_len and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def utf16_strings(data: bytes, limit: int = 30) -> list[str]:
    pattern = re.compile(rb"(?:[ -~]\x00){5,}")
    seen: list[str] = []
    for match in pattern.finditer(data):
        text = match.group().decode("utf-16-le", "ignore")
        if text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def report(path: Path, deep: bool) -> None:
    data = path.read_bytes()
    print(f"\n=== {path.name} ===")
    print(f"  {len(data):,} bytes   whole-file entropy {entropy(data[: 1 << 20]):.3f}")
    print(f"  head: {' '.join(f'{b:02x}' for b in data[:16])}")
    print(f"  tail: {' '.join(f'{b:02x}' for b in data[-16:])}")

    print(f"  entropy profile: {entropy_profile(data)}")

    magics = find_magics(data)
    if magics:
        print(f"  magic hits ({len(magics)}):")
        for off, label in magics[:15]:
            print(f"    0x{off:08x}  {label}")
    else:
        print("  magic hits: none")

    if deep:
        zl = find_zlib(data)
        if zl:
            total = sum(o for _, _, o in zl)
            print(f"  verified zlib streams: {len(zl)}  (inflates to {total:,} bytes)")
            for off, cin, cout in zl[:10]:
                print(f"    0x{off:08x}  {cin:>9,} in -> {cout:>10,} out")
        else:
            print("  verified zlib streams: none")

    ascii_hits = strings(data)
    if ascii_hits:
        print(f"  ascii strings ({len(ascii_hits)} shown):")
        for s in ascii_hits[:20]:
            print(f"    {s!r}")

    wide = utf16_strings(data)
    if wide:
        print(f"  utf-16 strings ({len(wide)} shown):")
        for s in wide[:20]:
            print(f"    {s!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--deep", action="store_true", help="verify zlib candidates by inflating")
    args = parser.parse_args()

    for path in args.files:
        if path.exists():
            report(path, args.deep)
        else:
            print(f"missing: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
