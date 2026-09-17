"""Export an engine mesh for the web: six cut solids, and the flow inside them.

Three corrections over the first attempt, all of them visible:

  * The mesh is **cut**, not offset. Translating the vertices of a continuous
    mesh by segment stretches every triangle that straddles a boundary. Each
    segment here is a separate solid, clipped at the plane, so it moves
    rigidly.
  * **No decimation.** Vertex clustering at 0.8 mm rounded off the fillets and
    smeared the engraving on the chamber wall. Full resolution ships instead;
    it costs about a megabyte more and it is the hero of the page.
  * **No normals.** Averaging face normals across a sharp CAD edge rounds it
    off, which is what made the solid look mushed. The viewer derives a flat
    normal per fragment instead, which is both crisper and smaller.

Segments are cut at feature boundaries found in the bore profile, not at even
spacing. Flow paths follow the bore and the centreline of every cooling
channel so streamlines stay inside the passages.

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

from sldprt.cutting import cut_into_segments  # noqa: E402
from sldprt.mesh import Mesh  # noqa: E402
from sldprt.readers import load_mesh  # noqa: E402
from sldprt.slicing import slice_mesh  # noqa: E402

CHANNEL_MAX_MM = 12.0
MIN_LOOP_MM = 0.4
UINT16_MAX = 65535


def loop_stats(loop):
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
    out = []
    for s in slice_mesh(mesh, axis=0, count=count):
        rows = [loop_stats(L) for L in s.loops if len(L) >= 3]
        rows = [r for r in rows if r["extent"] >= MIN_LOOP_MM]
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: -r["extent"])
        out.append({
            "x": s.position,
            "outer": rows[0]["extent"] / 2,
            "bore": rows[1]["extent"] / 2,
            "channels": [r for r in rows if r["extent"] < CHANNEL_MAX_MM],
        })
    return out


def despike(profile):
    """Drop sections whose bore disagrees wildly with their neighbours.

    A plane can graze a surface tangentially and report a 2.5 mm bore in the
    middle of a 48 mm chamber, which once put the throat 100 mm from its real
    position.
    """
    if len(profile) < 5:
        return profile
    kept = []
    for i, p in enumerate(profile):
        window = sorted(q["bore"] for q in profile[max(0, i - 2) : i + 3])
        median = window[len(window) // 2]
        if median > 0 and abs(p["bore"] - median) <= median * 0.45:
            kept.append(p)
    return kept or profile


def find_segments(profile, lo_x: float, hi_x: float):
    # Channels run through the chamber, convergent, throat and bell and stop
    # at both ends, which makes them the right mask: without it the widest
    # section in the part is the injector bore, not the chamber.
    cooled = despike([p for p in profile if len(p["channels"]) > 20]) or despike(profile)

    throat = min(cooled, key=lambda p: p["bore"])
    upstream = [p for p in cooled if p["x"] < throat["x"]]
    chamber_bore = max((p["bore"] for p in upstream), default=throat["bore"])

    conv_start = throat["x"]
    for p in upstream:
        if p["bore"] < chamber_bore * 0.97:
            conv_start = p["x"]
            break

    ch_first, ch_last = cooled[0]["x"], cooled[-1]["x"]
    half = max((hi_x - lo_x) * 0.026, 6.0)

    raw = [
        ("Injector interface", lo_x, ch_first),
        ("Combustion chamber", ch_first, conv_start),
        ("Convergent", conv_start, throat["x"] - half),
        ("Throat", throat["x"] - half, throat["x"] + half),
        ("Divergent bell", throat["x"] + half, ch_last),
        ("Coolant manifold", ch_last, hi_x),
    ]
    segs = [{"name": n, "start": a, "end": b} for n, a, b in raw if b > a + 1e-6]
    segs[-1]["end"] = hi_x
    segs[0]["start"] = lo_x
    return segs, throat, chamber_bore


def channel_paths(profile, min_len: int = 6):
    sections = [p for p in profile if len(p["channels"]) > 20]
    if not sections:
        return []

    paths = [[{"x": sections[0]["x"], "u": c["cu"], "v": c["cv"]}] for c in sections[0]["channels"]]
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
            # A channel does not jump round the engine between sections.
            if best >= 0 and best_d < 0.12:
                taken.add(best)
                c = sec["channels"][best]
                paths[i].append({"x": sec["x"], "u": c["cu"], "v": c["cv"]})
                heads[i] = c["theta"]

    return [p for p in paths if len(p) >= min_len]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sections", type=int, default=140)
    args = ap.parse_args()

    mesh = load_mesh(args.file.name, args.file.read_bytes())
    lo, hi = mesh.bbox()
    profile = read_profile(mesh, args.sections)
    segments, throat, chamber_bore = find_segments(profile, lo[0], hi[0])

    bounds = [(s["start"], s["end"]) for s in segments]
    parts = cut_into_segments(mesh, bounds)
    for p in parts:
        p.weld(1e-5)

    over = [i for i, p in enumerate(parts) if len(p.vertices) > UINT16_MAX]
    if over:
        raise SystemExit(f"segments {over} exceed the uint16 vertex limit; add a boundary")

    centre = [(lo[i] + hi[i]) / 2 for i in range(3)]
    dims = [hi[i] - lo[i] for i in range(3)]
    span = [max(d, 1e-6) for d in dims]
    scale = 1.0 / dims[0]

    blob = bytearray()
    entries = []
    for seg, part in zip(segments, parts):
        vstart = len(blob)
        for v in part.vertices:
            for k in range(3):
                q = int(round((v[k] - lo[k]) / span[k] * UINT16_MAX))
                blob += struct.pack("<H", max(0, min(UINT16_MAX, q)))
        istart = len(blob)
        for tri in part.triangles:
            blob += struct.pack("<3H", *tri)

        entries.append({
            "name": seg["name"],
            "start": round((seg["start"] - centre[0]) * scale, 6),
            "end": round((seg["end"] - centre[0]) * scale, 6),
            "start_mm": round(seg["start"], 1),
            "end_mm": round(seg["end"], 1),
            "vertexOffset": vstart,
            "vertexCount": len(part.vertices),
            "indexOffset": istart,
            "triangleCount": part.triangle_count,
        })

    path_floats: list[float] = []
    path_index = []
    for path in channel_paths(profile):
        start = len(path_floats) // 3
        for p in path:
            path_floats.append((p["x"] - centre[0]) * scale)
            path_floats.append((p["u"] - centre[1]) * scale)
            path_floats.append((p["v"] - centre[2]) * scale)
        path_index.append({"start": start, "count": len(path)})

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "mesh.bin").write_bytes(bytes(blob))
    (args.out / "paths.bin").write_bytes(struct.pack(f"<{len(path_floats)}f", *path_floats))

    manifest = {
        "source": args.file.name,
        "dimensions_mm": [round(d, 2) for d in dims],
        "scale": scale,
        "centre_mm": [round(c, 3) for c in centre],
        "quantisation": {"lo_mm": [round(v, 4) for v in lo], "span_mm": [round(v, 4) for v in span]},
        "sourceTriangles": mesh.triangle_count,
        "triangles": sum(p.triangle_count for p in parts),
        "segments": entries,
        "channels": {"count": len(path_index), "paths": path_index},
        "bore": [
            {"x": round((p["x"] - centre[0]) * scale, 5), "r": round(p["bore"] * scale, 5)}
            for p in despike(profile)
        ],
        "channelRadius": round(
            (sum(c["extent"] for p in profile for c in p["channels"])
             / max(sum(len(p["channels"]) for p in profile), 1)) / 2 * scale,
            5,
        ),
        "throat_mm": round(throat["bore"] * 2, 2),
        "chamber_bore_mm": round(chamber_bore * 2, 2),
    }
    (args.out / "engine.json").write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")

    print(f"{args.file.name}: {mesh.triangle_count:,} -> {manifest['triangles']:,} triangles after cutting")
    print(f"  mesh.bin {(args.out/'mesh.bin').stat().st_size/1024:.0f} KB, "
          f"paths.bin {(args.out/'paths.bin').stat().st_size/1024:.0f} KB")
    print(f"  throat {manifest['throat_mm']} mm, chamber bore {manifest['chamber_bore_mm']} mm, "
          f"contraction {(manifest['chamber_bore_mm']/manifest['throat_mm'])**2:.2f}")
    print(f"  {len(path_index)} channel paths, mean channel radius "
          f"{manifest['channelRadius']/scale:.2f} mm")
    for e in entries:
        print(f"    {e['name']:<22} {e['start_mm']:>6.1f} - {e['end_mm']:>6.1f} mm  "
              f"{e['triangleCount']:>7,} tris  {e['vertexCount']:>6,} verts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
