"""Report whether a .SLDPRT actually contains saved tessellation.

A converter that lifts the saved display mesh only works on files that have
one, and SolidWorks does not always write it. The web UI has to tell the
user that up front rather than failing halfway through, so this is the
check behind that message.

Method: account for every inflated byte by payload kind. A file with a saved
mesh is dominated by it -- a 1,392-triangle part needs ~50 KB of positions
alone, ~100 KB with normals. A file whose bytes are fully explained by
B-rep, preview, feature tree and padding has no mesh to lift.

Read-only.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.container import extract  # noqa: E402
from sldprt.tess import decode  # noqa: E402

# A reference plane is a flat quad: 4 vertices, one axis of zero extent.
PLANE_VERTS = 4
FLAT_MM = 0.5

# Below this, what we found is display scaffolding, not a part. Real saved
# meshes run to thousands of vertices; a handful of stray quads is noise and
# must not be reported as a usable mesh.
MIN_REAL_VERTS = 100


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()

    for path in args.files:
        if not path.exists():
            print(f"missing: {path}")
            continue

        records = extract(path, min_out=512)
        kinds = Counter()
        for r in records:
            kinds[r.kind.split(":")[0]] += r.size

        real_verts = 0
        plane_arrays = 0
        for r in records:
            res = decode(r.data)
            for a in res["arrays"]:
                if a.kind != "position":
                    continue
                (lo, hi) = a.bbox()
                extents = [(hi[i] - lo[i]) * 1000.0 for i in range(3)]
                flat = sum(1 for e in extents if e < FLAT_MM)
                if a.count == PLANE_VERTS and flat >= 1:
                    plane_arrays += 1
                else:
                    real_verts += a.count

        disk = path.stat().st_size
        inflated = sum(r.size for r in records if r.parent is None)
        brep = sum(v for k, v in kinds.items() if k.startswith("parasolid"))

        print(f"\n=== {path.name} ===")
        print(f"  on disk {disk:,} B -> inflated {inflated:,} B")
        print(f"  parasolid B-rep      {brep:>12,} B")
        print(f"  png preview          {kinds.get('png-preview', 0):>12,} B")
        print(f"  feature tree (sw)    {kinds.get('sw', 0):>12,} B")
        print(f"  unqlite + padding    {kinds.get('unqlite-db', 0) + kinds.get('unknown', 0):>12,} B")
        print(f"  reference-plane quads {plane_arrays:>11}")

        if real_verts >= MIN_REAL_VERTS:
            print(f"  SAVED MESH: yes - {real_verts:,} vertices")
        elif real_verts:
            print(
                f"  SAVED MESH: none - only {real_verts} stray display vertices, "
                f"below the {MIN_REAL_VERTS}-vertex floor for a real part"
            )
        else:
            print("  SAVED MESH: none - nothing but planes, B-rep and metadata")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
