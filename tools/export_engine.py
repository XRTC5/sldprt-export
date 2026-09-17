"""Export an engine mesh for the web: segments, solid mesh, and flow paths.

Three things a viewer needs and none of them arbitrary:

  * **Segments** cut at real feature boundaries, found by reading the bore
    profile — injector interface, chamber, convergent, throat, divergent,
    manifold. Evenly spaced cuts are wafers, not engineering.
  * **A solid mesh**, decimated by vertex clustering and quantised: positions
    to uint16 across the bounding box, normals to int8, indices to uint16,
    plus a segment id per vertex so the viewer can separate the parts without
    holding six meshes.
  * **Flow paths** through the hollow volumes: the bore, and the centreline of
    every cooling channel, chained section to section by angle. Streamlines
    then run through the passages that actually exist.

Read-only with respect to the source.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.mesh import Mesh  # noqa: E402
from sldprt.readers import load_mesh  # noqa: E402
from sldprt.slicing import slice_mesh  # noqa: E402

# A loop this small across is a cooling channel, not a wall or a bore.
CHANNEL_MAX_MM = 12.0
# Below this the "loop" is a tangential graze, not a real section.
MIN_LOOP_MM = 0.4


def loop_stats(loop, coords_none=None):
    us = [p[0] for p in loop]
    vs = [p[1] for p in loop]
    cu = sum(us) / len(us)
    cv = sum(vs) / len(vs)
    return {
        "extent": max(max(us) - min(us), max(vs) - min(vs)),
        "cu": cu,
        "cv": cv,
        "theta": math.atan2(cv, cu),
        "radius": math.hypot(cu, cv),
    }


def read_profile(mesh: Mesh, count: int):
    """Per-section outer radius, bore radius and channel loops."""
    out = []
    for s in slice_mesh(mesh, axis=0, count=count):
        rows = [loop_stats(L) for L in s.loops if len(L) >= 3]
        rows = [r for r in rows if r["extent"] >= MIN_LOOP_MM]
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: -r["extent"])
        channels = [r for r in rows if r["extent"] < CHANNEL_MAX_MM]
        out.append({
            "x": s.position,
            "outer": rows[0]["extent"] / 2,
            "bore": rows[1]["extent"] / 2,
            "channels": channels,
        })
    return out


def despike(profile):
    """Reject sections whose bore disagrees wildly with their neighbours.

    A plane can grave a surface tangentially and produce a tiny loop that
    looks like a 2.5 mm bore in the middle of a 48 mm chamber. One such
    section put the throat at 5 mm and 100 mm from its real position, so
    these are dropped rather than trusted.
    """
    if len(profile) < 5:
        return profile

    kept = []
    for i, p in enumerate(profile):
        lo = max(0, i - 2)
        window = sorted(q["bore"] for q in profile[lo : i + 3])
        median = window[len(window) // 2]
        if median > 0 and abs(p["bore"] - median) <= median * 0.45:
            kept.append(p)
    return kept or profile


def find_segments(profile):
    """Feature boundaries, read off the bore rather than chosen."""
    if not profile:
        return []

    # Channels exist through the chamber, convergent, throat and bell, and
    # stop at both the injector interface and the coolant manifold. That makes
    # them the right mask for finding the throat: without it the widest
    # section is the injector bore, not the chamber.
    cooled = despike([p for p in profile if len(p["channels"]) > 20])
    if not cooled:
        cooled = despike(profile)

    throat = min(cooled, key=lambda p: p["bore"])
    upstream = [p for p in cooled if p["x"] < throat["x"]]
    chamber_bore = max((p["bore"] for p in upstream), default=throat["bore"])

    # The chamber runs while the bore holds its full value; convergence starts
    # where it first drops meaningfully below it.
    conv_start = throat["x"]
    for p in upstream:
        if p["bore"] < chamber_bore * 0.97:
            conv_start = p["x"]
            break

    ch_first = cooled[0]["x"]
    ch_last = cooled[-1]["x"]
    div_end = ch_last

    lo = profile[0]["x"]
    hi = profile[-1]["x"]
    throat_half = max((hi - lo) * 0.026, 6.0)

    bounds = [
        ("Injector interface", lo, ch_first),
        ("Combustion chamber", ch_first, conv_start),
        ("Convergent", conv_start, throat["x"] - throat_half),
        ("Throat", throat["x"] - throat_half, throat["x"] + throat_half),
        ("Divergent bell", throat["x"] + throat_half, div_end),
        ("Coolant manifold", div_end, hi),
    ]
    return [
        {"name": n, "start": a, "end": b}
        for n, a, b in bounds
        if b > a + 1e-6
    ]


def channel_paths(profile, min_len: int = 6):
    """Chain channel centroids into one path per cooling channel."""
    sections = [p for p in profile if len(p["channels"]) > 20]
    if not sections:
        return []

    paths = [
        [{"x": sections[0]["x"], "u": c["cu"], "v": c["cv"]}]
        for c in sections[0]["channels"]
    ]
    heads = [c["theta"] for c in sections[0]["channels"]]

    for sec in sections[1:]:
        taken = set()
        for i, theta in enumerate(heads):
            best, best_d = -1, 1e9
            for j, c in enumerate(sec["channels"]):
                if j in taken:
                    continue
                d = abs(math.atan2(math.sin(c["theta"] - theta), math.cos(c["theta"] - theta)))
                if d < best_d:
                    best_d, best = d, j
            # A channel does not jump round the engine between sections; a
            # large angular step means the match is wrong, so drop it.
            if best >= 0 and best_d < 0.12:
                taken.add(best)
                c = sec["channels"][best]
                paths[i].append({"x": sec["x"], "u": c["cu"], "v": c["cv"]})
                heads[i] = c["theta"]

    return [p for p in paths if len(p) >= min_len]


def bore_path(profile):
    return [{"x": p["x"], "radius": p["bore"]} for p in profile]


def decimate(mesh: Mesh, grid_mm: float) -> Mesh:
    m = Mesh(vertices=list(mesh.vertices), triangles=list(mesh.triangles))
    return m.weld(grid_mm)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument("--grid", type=float, default=0.8, help="decimation grid, mm")
    ap.add_argument("--sections", type=int, default=140, help="sections used for analysis")
    args = ap.parse_args()

    full = load_mesh(args.file.name, args.file.read_bytes())
    profile = read_profile(full, args.sections)
    segments = find_segments(profile)
    channels = channel_paths(profile)
    bore = bore_path(profile)

    cooled = despike([p for p in profile if len(p["channels"]) > 20]) or despike(profile)
    throat_section = min(cooled, key=lambda p: p["bore"])
    throat_mm = throat_section["bore"] * 2
    chamber_mm = max(
        (p["bore"] for p in cooled if p["x"] < throat_section["x"]),
        default=throat_section["bore"],
    ) * 2

    mesh = decimate(full, args.grid)
    if len(mesh.vertices) > 65535:
        raise SystemExit(
            f"{len(mesh.vertices):,} vertices exceeds the uint16 index limit; "
            "raise --grid"
        )

    lo, hi = full.bbox()
    centre = [(lo[i] + hi[i]) / 2 for i in range(3)]
    dims = [hi[i] - lo[i] for i in range(3)]
    scale = 1.0 / dims[0]

    # Quantise into the bounding box. uint16 over 345 mm is 5 micron steps,
    # far finer than a 0.8 mm decimation grid.
    span = [max(d, 1e-6) for d in dims]

    n = len(mesh.vertices)
    pos = bytearray(n * 6)
    nrm = bytearray(n * 3)
    seg = bytearray(n)

    # Per-vertex normals, area-weighted by accumulating face normals.
    acc = [[0.0, 0.0, 0.0] for _ in range(n)]
    for tri in mesh.triangles:
        a, b, c = (mesh.vertices[i] for i in tri)
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        fx, fy, fz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        for i in tri:
            acc[i][0] += fx
            acc[i][1] += fy
            acc[i][2] += fz

    def segment_of(x: float) -> int:
        for i, s in enumerate(segments):
            if x <= s["end"]:
                return i
        return max(len(segments) - 1, 0)

    for i, v in enumerate(mesh.vertices):
        for k in range(3):
            q = int(round((v[k] - lo[k]) / span[k] * 65535))
            struct.pack_into("<H", pos, i * 6 + k * 2, max(0, min(65535, q)))
        nx, ny, nz = acc[i]
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        for k, comp in enumerate((nx, ny, nz)):
            nrm[i * 3 + k] = int(round(max(-127, min(127, comp / length * 127)))) & 0xFF
        seg[i] = segment_of(v[0])

    idx = bytearray(mesh.triangle_count * 6)
    for t, tri in enumerate(mesh.triangles):
        struct.pack_into("<3H", idx, t * 6, *tri)

    # Flow paths, normalised the same way as the mesh.
    path_floats: list[float] = []
    path_index = []
    for path in channels:
        start = len(path_floats) // 3
        for p in path:
            path_floats.append((p["x"] - centre[0]) * scale)
            path_floats.append((p["u"] - centre[1]) * scale)
            path_floats.append((p["v"] - centre[2]) * scale)
        path_index.append({"start": start, "count": len(path)})

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "mesh.bin").write_bytes(bytes(pos) + bytes(nrm) + bytes(seg) + bytes(idx))
    (args.out / "paths.bin").write_bytes(struct.pack(f"<{len(path_floats)}f", *path_floats))

    manifest = {
        "source": args.file.name,
        "dimensions_mm": [round(d, 2) for d in dims],
        "scale": scale,
        "centre_mm": [round(c, 3) for c in centre],
        "quantisation": {"lo_mm": [round(v, 3) for v in lo], "span_mm": [round(v, 3) for v in span]},
        "vertices": n,
        "triangles": mesh.triangle_count,
        "source_triangles": full.triangle_count,
        "segments": [
            {
                "name": s["name"],
                "start": round((s["start"] - centre[0]) * scale, 5),
                "end": round((s["end"] - centre[0]) * scale, 5),
                "start_mm": round(s["start"], 1),
                "end_mm": round(s["end"], 1),
            }
            for s in segments
        ],
        "channels": {"count": len(channels), "paths": path_index},
        "bore": [
            {"x": round((b["x"] - centre[0]) * scale, 5), "r": round(b["radius"] * scale, 5)}
            for b in bore
        ],
        "throat_mm": round(throat_mm, 2),
        "chamber_bore_mm": round(chamber_mm, 2),
    }
    (args.out / "engine.json").write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")

    mesh_kb = (args.out / "mesh.bin").stat().st_size / 1024
    print(f"{args.file.name}: {full.triangle_count:,} -> {mesh.triangle_count:,} triangles")
    print(f"  {n:,} vertices, mesh.bin {mesh_kb:.0f} KB")
    print(f"  {len(channels)} channel paths, paths.bin {(args.out/'paths.bin').stat().st_size/1024:.0f} KB")
    print(f"  throat {manifest['throat_mm']} mm, chamber bore {manifest['chamber_bore_mm']} mm, "
          f"contraction {(manifest['chamber_bore_mm']/manifest['throat_mm'])**2:.2f}")
    for s in manifest["segments"]:
        print(f"    {s['name']:<22} {s['start_mm']:>6.1f} - {s['end_mm']:>6.1f} mm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
