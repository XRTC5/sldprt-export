"""Phase 1c: brute-force every offset for raw DEFLATE streams.

scan_raw.py only found zlib-wrapped streams (0x78 magic) and left most of
the file unexplained at 7.94 entropy. Raw deflate carries no magic bytes,
so the only way to find it is to try inflating at every byte offset and
keep what succeeds.

A hit is judged real by output size and by whether the decompressor
consumed a sensible amount of input -- random bytes occasionally produce a
few valid deflate symbols, but not kilobytes of them.

Read-only.
"""

from __future__ import annotations

import argparse
import math
import re
import zlib
from collections import Counter
from pathlib import Path

PRINTABLE = re.compile(rb"[ -~]{5,}")


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# Trial decode budget. Slicing the whole remainder at every offset makes the
# scan O(n^2) -- fine on 78 KB, hours on 33 MB. Instead probe with a bounded
# window and a capped output, then re-inflate in full only for real hits.
PROBE_IN = 1 << 16
PROBE_OUT = 1 << 12


def probe(view: memoryview, offset: int, wbits: int) -> bool:
    """Cheap test: does a meaningful amount of data inflate here?"""
    obj = zlib.decompressobj(wbits)
    try:
        out = obj.decompress(view[offset : offset + PROBE_IN], PROBE_OUT)
    except zlib.error:
        return False
    return len(out) >= 256


def try_inflate(data: bytes, offset: int, wbits: int) -> tuple[int, bytes] | None:
    obj = zlib.decompressobj(wbits)
    try:
        out = obj.decompress(memoryview(data)[offset:])
        out += obj.flush()
    except zlib.error:
        return None
    if not out:
        return None
    consumed = (len(data) - offset) - len(obj.unused_data)
    return consumed, out


def scan(path: Path, min_out: int, step: int, wbits: int, limit: int) -> list[dict]:
    data = path.read_bytes()
    view = memoryview(data)
    hits: list[dict] = []
    offset = 0
    end = len(data)

    while offset < end and len(hits) < limit:
        result = try_inflate(data, offset, wbits) if probe(view, offset, wbits) else None
        if result is not None:
            consumed, out = result
            if len(out) >= min_out:
                hits.append(
                    {
                        "offset": offset,
                        "consumed": consumed,
                        "out": len(out),
                        "entropy": round(entropy(out), 2),
                        "head": out[:16],
                        "strings": [
                            m.group().decode("ascii", "ignore")
                            for m in PRINTABLE.finditer(out[:4096])
                        ][:4],
                    }
                )
                # Skip past what this stream consumed; overlapping hits on the
                # same stream are noise, not new payload.
                offset += max(consumed, 1)
                continue
        offset += step

    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--min-out", type=int, default=512, help="ignore hits smaller than this")
    parser.add_argument("--step", type=int, default=1, help="offset stride")
    parser.add_argument("--raw", action="store_true", help="raw deflate (wbits=-15) instead of zlib")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dump-dir", type=Path, help="write each hit's payload here")
    args = parser.parse_args()

    wbits = -15 if args.raw else 15
    data_len = args.file.stat().st_size
    hits = scan(args.file, args.min_out, args.step, wbits, args.limit)

    mode = "raw deflate" if args.raw else "zlib"
    print(f"=== {args.file.name} ===")
    print(f"  {data_len:,} bytes, scanning for {mode}, min output {args.min_out:,}")
    print(f"  {len(hits)} hits\n")

    covered_in = 0
    total_out = 0
    for h in hits:
        covered_in += h["consumed"]
        total_out += h["out"]
        head = " ".join(f"{b:02x}" for b in h["head"])
        print(f"  0x{h['offset']:08x}  {h['consumed']:>9,} in -> {h['out']:>10,} out  ent {h['entropy']:>4}")
        print(f"      {head}")
        if h["strings"]:
            print(f"      {h['strings']}")

    if hits:
        print(f"\n  input covered: {covered_in:,} / {data_len:,} bytes ({covered_in / data_len:.1%})")
        print(f"  total inflated: {total_out:,} bytes")

    if args.dump_dir and hits:
        args.dump_dir.mkdir(parents=True, exist_ok=True)
        raw = args.file.read_bytes()
        for h in hits:
            result = try_inflate(raw, h["offset"], wbits)
            if result:
                out_path = args.dump_dir / f"{args.file.stem}_{h['offset']:08x}.bin"
                out_path.write_bytes(result[1])
        print(f"  dumped {len(hits)} payloads to {args.dump_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
