"""Export one small solid for a selection screen: the whole model, decimated.

The full exports next door are hero assets — a megabyte or two each, cut into
parts, carrying edges and flow paths. A picker that shows several at once
cannot pay that several times, and does not need to: at card size the model
is a silhouette that turns.

For an assembly, pass the same `--parts` rules the hero export uses. Without
them the threaded fasteners are three quarters of the triangles and most of
the edges, a single global decimation spends its whole budget on thread, and
the model comes out welded at 4 mm — which is 1% of the motor's length and
looks it.

Rules may also mark a part `"preview": false`. Fasteners are the case: they
carry no silhouette at card size, and leaving them in forces the decimator
to spend the budget on thread and tear holes through the plates instead.

Read-only with respect to the source.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_assembly import (  # noqa: E402
    UINT16_MAX,
    classify,
    decimate,
    feature_edges,
    instance_key,
    load_rules,
    merge,
)
from sldprt.assembly import read_assembly  # noqa: E402
from sldprt.blocks import read_blocks  # noqa: E402
from sldprt.readers import load_mesh  # noqa: E402

# A ceiling, not a target. Thirty thousand line segments is nothing for a
# GPU; the reason to have a limit at all is that past some density the
# wireframe stops describing a shape. Models under it are shipped whole.
EDGE_CAP = 40000

# Low enough to catch the facets of a revolved surface, which meet at
# about 360/N degrees and are invisible at any feature angle worth using
# on a machined face.
SMOOTH_ANGLE = 5.0

# Angles tried, in order, when a model is over the cap.
ANGLE_STEPS = (35.0, 45.0, 55.0, 65.0, 75.0)


def buckets_for(path: Path, rules: dict) -> dict[str, list]:
    """Component meshes keyed by model name, so each keeps its own angle."""
    if path.suffix.lower() != ".sldasm":
        return {path.stem: [load_mesh(path.name, path.read_bytes())]}

    folder = path.parent
    by_lower = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}

    def resolve(name: str) -> bytes | None:
        found = by_lower.get(name.lower())
        return found.read_bytes() if found else None

    blocks = read_blocks(path.read_bytes())
    try:
        # 'Default', not the most recent: these files were last saved in an
        # Exploded View, and a picker model of an already-exploded motor is
        # a cloud of parts.
        _whole, components, _report = read_assembly(
            blocks, resolve=resolve, configuration="Default"
        )
    except Exception:
        _whole, components, _report = read_assembly(blocks, resolve=resolve)
    out: dict[str, list] = {}
    for comp in components:
        if comp.hidden:
            continue
        owner, model = instance_key(comp.name)
        rule = classify(owner, model, rules) if rules["parts"] else {"budget": None}
        if rule is None or rule.get("preview") is False:
            continue
        out.setdefault(model, []).append((comp.mesh, rule))
    return {k: v for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--parts", type=Path, help="the hero export's grouping rules")
    ap.add_argument("--tris", type=int, default=60000)
    ap.add_argument("--angle", type=float, default=45.0,
                    help="dihedral angle; coarser than the hero exports because a "
                         "card cannot resolve a chamfer")
    ap.add_argument("--edge-cap", type=int, default=EDGE_CAP)
    args = ap.parse_args()

    rules = load_rules(args.parts)
    raw = buckets_for(args.file, rules)

    def build(smooth: float):
        """Every edge in the model, at `smooth` for revolves and the base
        angle for everything else. Returns the points, the pairs, and the
        source triangle count."""
        points: list[tuple[float, float, float]] = []
        pairs: list[tuple[int, int]] = []
        total = 0
        for model, items in raw.items():
            one = merge([m if not isinstance(m, tuple) else m[0] for m in items])
            one.weld(1e-5)
            total += one.triangle_count

            angle = rules["edgeAngles"].get(model, args.angle)
            if model not in rules["edgeAngles"]:
                """A smooth revolve needs a much lower angle or it vanishes.

                Facets around a tessellated cylinder meet at roughly 360/N
                degrees, so at any angle worth using on a machined face a
                tube gives its two end rings and nothing in between. On the
                picker that reads as a gap: the MACAW previews came out as
                two bulkheads floating with no grain between them.
                """
                if len(feature_edges(one, angle)) < one.triangle_count * 0.6:
                    angle = smooth

            index: dict[int, int] = {}
            for a, b in feature_edges(one, angle):
                ends = []
                for v in (a, b):
                    got = index.get(v)
                    if got is None:
                        got = len(points)
                        index[v] = got
                        points.append(one.vertices[v])
                    ends.append(got)
                pairs.append((ends[0], ends[1]))
        return points, pairs, total

    cap = args.edge_cap
    """Find the finest smooth angle that fits the budget.

    Searching upward from the finest, and never past the base angle, is
    the whole point. An earlier version only tried angles *coarser* than
    the base when it went over, so Egret asked for 5 degrees, produced
    105,321 edges, overshot the cap and was handed 45 degrees and 22,224
    edges: fewer than the 25,074 it would have had by doing nothing at
    all. Chasing detail cost it detail.
    """
    points: list[tuple[float, float, float]] = []
    pairs: list[tuple[int, int]] = []
    source = 0
    for smooth in (SMOOTH_ANGLE, 8.0, 12.0, 16.0, 20.0, 25.0, 30.0, args.angle):
        if smooth > args.angle:
            break
        points, pairs, source = build(smooth)
        if len(pairs) <= cap:
            break

    # Still over with nothing but the base angle left: now it is fair to
    # go coarser, because the alternative is a model nobody can read.
    if len(pairs) > cap:
        for angle in ANGLE_STEPS:
            if angle <= args.angle:
                continue
            args.angle = angle
            points, pairs, source = build(angle)
            if len(pairs) <= cap:
                break

    solids: list = []
    for model, items in raw.items():
        meshes = [m if not isinstance(m, tuple) else m[0] for m in items]
        rule = items[0][1] if isinstance(items[0], tuple) else {}
        one = merge(meshes)
        one.weld(1e-5)
        # An equal share of the budget per part, not weighted by triangle
        # count: weighting by size hands the budget back to the thread.
        share = max(300, args.tris // max(len(raw), 1))
        reduced, _ = decimate(one, min(share, rule.get("budget") or args.tris))
        solids.append(reduced)

    solid = merge(solids)
    solid.weld(1e-5)
    lo, hi = solid.bbox()
    if solid.triangle_count > args.tris or len(solid.vertices) > UINT16_MAX:
        solid, tolerance = decimate(solid, args.tris)
    else:
        tolerance = 0.0

    keep = sorted({v for pair in pairs for v in pair})
    remap = {v: i for i, v in enumerate(keep)}
    points = [points[v] for v in keep]
    pairs = [(remap[a], remap[b]) for a, b in pairs]
    if len(points) > UINT16_MAX:
        raise SystemExit(f"{len(points)} edge vertices; lower the cap or raise the angle")

    centre = [(lo[k] + hi[k]) / 2 for k in range(3)]
    dims = [hi[k] - lo[k] for k in range(3)]
    span = [max(d, 1e-6) for d in dims]

    def quantise(point) -> bytes:
        out = b""
        for k in range(3):
            q = int(round((point[k] - lo[k]) / span[k] * UINT16_MAX))
            out += struct.pack("<H", max(0, min(UINT16_MAX, q)))
        return out

    blob = bytearray()
    for v in solid.vertices:
        blob += quantise(v)
    index_offset = len(blob)
    for tri in solid.triangles:
        blob += struct.pack("<3H", *tri)
    edge_vertex_offset = len(blob)
    for v in points:
        blob += quantise(v)
    edge_offset = len(blob)
    for a, b in pairs:
        blob += struct.pack("<2H", a, b)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "preview.bin").write_bytes(bytes(blob))
    (args.out / "preview.json").write_text(json.dumps({
        "source": args.file.name,
        "dimensions_mm": [round(d, 2) for d in dims],
        # Longest side to one unit, so cards of different models match.
        "scale": 1.0 / max(dims),
        "centre_mm": [round(c, 3) for c in centre],
        "quantisation": {"lo_mm": [round(v, 4) for v in lo], "span_mm": [round(v, 4) for v in span]},
        "sourceTriangles": source,
        "vertexCount": len(solid.vertices),
        "indexOffset": index_offset,
        "triangleCount": solid.triangle_count,
        "edgeVertexOffset": edge_vertex_offset,
        "edgeVertexCount": len(points),
        "edgeOffset": edge_offset,
        "edgeCount": len(pairs),
    }, separators=(",", ":")), encoding="utf-8")

    print(f"{args.file.name}: {source:,} -> {solid.triangle_count:,} triangles "
          f"(final weld {tolerance:.2f} mm), {len(pairs):,} edges, "
          f"{(args.out / 'preview.bin').stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
