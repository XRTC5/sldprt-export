"""Turn a mesh into a compact slice asset for the web.

Emits two things from one part:

  * a stack of cross-sections along the engine axis -- the slices
  * one longitudinal section through the axis -- the bell profile, which is
    what makes the shape readable once the stack pulls apart

Output is a JSON manifest plus a single float32 blob. Coordinates are
centred on the part and normalised so the long axis runs -0.5..0.5, which
means the viewer never has to know the part's real size to frame it. True
millimetre dimensions travel in the manifest for annotation.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.readers import load_mesh  # noqa: E402
from sldprt.slicing import simplify_slices, slice_mesh  # noqa: E402

# Loops smaller than this across are tessellation noise, not features.
MIN_LOOP_MM = 0.4


def _pack(slices, axis, centre, scale, kind):
    """Flatten slices into (manifest entries, float32 coordinate list)."""
    entries, coords = [], []
    drop = [i for i in range(3) if i != axis]

    for s in slices:
        loops = []
        for loop, closed in zip(s.loops, s.closed):
            xs = [p[0] for p in loop]
            ys = [p[1] for p in loop]
            if max(max(xs) - min(xs), max(ys) - min(ys)) < MIN_LOOP_MM:
                continue

            start = len(coords) // 2
            for u, v in loop:
                coords.append((u - centre[drop[0]]) * scale)
                coords.append((v - centre[drop[1]]) * scale)
            loops.append({"start": start, "count": len(loop), "closed": closed})

        if loops:
            entries.append({
                "kind": kind,
                "position": (s.position - centre[axis]) * scale,
                "position_mm": round(s.position, 3),
                "loops": loops,
            })
    return entries, coords


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="output basename, no extension")
    ap.add_argument("--slices", type=int, default=48)
    ap.add_argument("--axis", type=int, default=0, help="0=x 1=y 2=z")
    ap.add_argument("--tolerance", type=float, default=0.15, help="simplification, mm")
    args = ap.parse_args()

    mesh = load_mesh(args.file.name, args.file.read_bytes())
    lo, hi = mesh.bbox()
    centre = [(lo[i] + hi[i]) / 2 for i in range(3)]
    dims = [hi[i] - lo[i] for i in range(3)]
    scale = 1.0 / dims[args.axis]

    cross = simplify_slices(
        slice_mesh(mesh, axis=args.axis, count=args.slices), args.tolerance
    )
    entries, coords = _pack(cross, args.axis, centre, scale, "cross")

    # One cut through the axis gives the longitudinal profile. Slicing with
    # count=1 lands the single plane mid-span, which is the axis.
    long_axis = 2 if args.axis != 2 else 1
    profile = simplify_slices(
        slice_mesh(mesh, axis=long_axis, count=1, inset=0.5), args.tolerance
    )
    prof_entries, prof_coords = _pack(
        profile, long_axis, centre, scale, "profile"
    )
    offset = len(coords) // 2
    for e in prof_entries:
        for loop in e["loops"]:
            loop["start"] += offset
    coords.extend(prof_coords)

    report = mesh.report()
    manifest = {
        "source": args.file.name,
        "axis": args.axis,
        "scale": scale,
        "dimensions_mm": [round(d, 2) for d in dims],
        "triangles": report["triangles"],
        "vertices": report["vertices"],
        "open_edges": report["open_edges"],
        "closed": report["closed"],
        "volume_mm3": round(report["volume_mm3"], 1) if report["volume_mm3"] else None,
        "float_count": len(coords),
        "slices": entries,
        "profile": prof_entries,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.out.with_suffix(".json")
    bin_path = args.out.with_suffix(".bin")
    json_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    bin_path.write_bytes(struct.pack(f"<{len(coords)}f", *coords))

    loops = sum(len(e["loops"]) for e in entries)
    print(f"{args.file.name}: {report['triangles']:,} triangles")
    print(f"  {len(entries)} cross-sections, {loops} loops, {len(coords)//2:,} points")
    print(f"  {len(prof_entries)} profile section, {sum(len(e['loops']) for e in prof_entries)} loops")
    print(f"  dimensions {dims[0]:.1f} x {dims[1]:.1f} x {dims[2]:.1f} mm")
    print(f"  wrote {json_path.name} ({json_path.stat().st_size/1024:.0f} KB)")
    print(f"  wrote {bin_path.name} ({bin_path.stat().st_size/1024:.0f} KB)")

    counts = [len(e["loops"]) for e in entries]
    if counts:
        mid = sorted(counts)[len(counts) // 2]
        print(f"  median loops per section: {mid}  (outer + bore + ~{mid - 2} channels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
