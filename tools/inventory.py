"""Inventory every payload in one or more .SLDPRT files.

Answers the question the whole project hangs on: what is actually stored in
these files, and is a saved tessellation among it?

Read-only.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.container import coverage, extract  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--min-out", type=int, default=256)
    parser.add_argument("--out", type=Path, help="write payloads here, one file each")
    parser.add_argument("--quiet", action="store_true", help="summary only")
    args = parser.parse_args()

    for path in args.files:
        if not path.exists():
            print(f"missing: {path}")
            continue

        records = extract(path, min_out=args.min_out)
        covered, total = coverage(path, records)

        print(f"\n=== {path.name} ===")
        print(f"  {total:,} bytes -> {len(records)} payloads, {covered / total:.1%} covered")

        kinds = Counter(r.kind for r in records)
        inflated = sum(r.size for r in records if r.parent is None)
        print(f"  inflated: {inflated:,} bytes")
        print("  by kind:")
        for kind, n in kinds.most_common():
            size = sum(r.size for r in records if r.kind == kind)
            print(f"    {kind:<28} {n:>3}  {size:>12,} B")

        if not args.quiet:
            print("  payloads:")
            for r in sorted(records, key=lambda r: r.size, reverse=True)[:25]:
                print(f"    {r.label()}")

        if args.out:
            target = args.out / path.stem
            target.mkdir(parents=True, exist_ok=True)
            for r in records:
                tag = f"p{r.parent:08x}_" if r.parent is not None else ""
                safe = r.kind.replace(":", "-").replace("/", "-")
                (target / f"{tag}{r.offset:08x}_{safe}.bin").write_bytes(r.data)
            print(f"  wrote {len(records)} payloads to {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
