"""Inflate a zlib stream at a given offset and characterise the result.

Used to test whether a zlib hit found by scan_raw.py is real payload or a
coincidence -- random high-entropy bytes occasionally pass the header check,
but their output is short and structureless.

Read-only.
"""

from __future__ import annotations

import argparse
import math
import re
import zlib
from collections import Counter
from pathlib import Path

PRINTABLE = re.compile(rb"[ -~]{4,}")


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def hexdump(data: bytes, offset: int = 0, rows: int = 8) -> str:
    lines = []
    for r in range(rows):
        start = offset + r * 16
        chunk = data[start : start + 16]
        if not chunk:
            break
        hex_part = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {start:08x}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("offset", type=lambda s: int(s, 0))
    parser.add_argument("--save", type=Path, help="write the inflated bytes here")
    parser.add_argument("--raw", action="store_true", help="raw deflate (wbits=-15)")
    args = parser.parse_args()

    data = args.file.read_bytes()
    obj = zlib.decompressobj(-15 if args.raw else 15)
    out = obj.decompress(data[args.offset :])

    print(f"offset 0x{args.offset:08x} -> {len(out):,} bytes inflated")
    print(f"entropy of output: {entropy(out):.3f}")
    print("\nhead:")
    print(hexdump(out))

    texts = [m.group().decode("ascii", "ignore") for m in PRINTABLE.finditer(out)]
    texts = [t for t in texts if len(t) >= 5][:25]
    if texts:
        print(f"\nascii strings ({len(texts)}):")
        for t in texts:
            print(f"    {t!r}")
    else:
        print("\nascii strings: none")

    wide = re.compile(rb"(?:[ -~]\x00){4,}")
    wides = [m.group().decode("utf-16-le", "ignore") for m in wide.finditer(out)][:25]
    if wides:
        print(f"\nutf-16 strings ({len(wides)}):")
        for t in wides:
            print(f"    {t!r}")

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_bytes(out)
        print(f"\nwrote {args.save}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
