"""Export a .SLDASM for the web: one solid per logical part, plus its edges.

The engine exporter next door cuts a single part along its axis, because the
Egret chamber is one printed piece. An assembly is the opposite problem: the
parts already exist, there are more of them than anybody wants to look at,
and several are the same component placed eight times. So this tool groups
instances into named parts, welds each group into one solid, and writes the
result in the same quantised layout `engine.json` uses.

Three things it does that a straight dump does not:

  * **Groups by rule, not by instance.** Eight tie bolts are one part called
    "Tie bolts", not eight entries in a tree nobody will read. The rules live
    in a JSON file beside the tool so the geometry code stays general.
  * **Decimates only what needs it.** A threaded M10 stud arrives at 54,555
    triangles, all of them thread that is two pixels wide on screen. Vertex
    clustering runs per part, at the coarsest tolerance that stays inside the
    triangle budget, and parts already under it are left untouched.
  * **Ships feature edges, cut from the full-resolution mesh.** The wireframe
    comes from the dihedral angle at each edge, so the viewer draws real CAD
    edges rather than every tessellation line. It is taken *before*
    decimation and carries its own vertices, because clustering a smooth
    thread helix makes it jagged and the edge count goes **up**: the same
    eight bolts give 3,944 edges at full resolution and 37,288 after the
    clustering that the solid needs.

The dihedral angle is per component model, not global. At 70 degrees a bolt
reduces to its hex and its chamfers; at 70 degrees the nozzle, which is one
smooth revolve, has no edges at all.

Read-only with respect to the source.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.assembly import read_assembly  # noqa: E402
from sldprt.blocks import read_blocks  # noqa: E402
from sldprt.cutting import cut_into_segments  # noqa: E402
from sldprt.assembly import TRANSLATION_SCALE as TRANSLATION_MM  # noqa: E402
from sldprt.mesh import Mesh  # noqa: E402

UINT16_MAX = 65535

# Weld tolerances tried in order when a part is over budget, in mm. The first
# that brings it under wins, so a part is never decimated harder than it has
# to be.
DECIMATION_STEPS = (0.0, 0.2, 0.35, 0.6, 1.0, 1.6, 2.5, 4.0)

# An edge is drawn when its two faces disagree by more than this. 30 degrees
# keeps the facets of a tessellated cylinder quiet while holding every
# chamfer, counterbore and flat. Threaded parts override it in the rules.
FEATURE_ANGLE_DEG = 30.0

INSTANCE_SUFFIX = re.compile(r"-\d+$")


def instance_key(full_name: str) -> tuple[str, str]:
    """Split `a-1@asm/plates-3@a` into its owner and its model name.

    The instance path is the only identity a component has here, and the
    useful half is the last step: which part file it is, and which
    subassembly it sits in. Bulkhead plates at either end of the motor are
    the same part file and want to be different entries on the page, so the
    owner has to survive.
    """
    leaf = full_name.split("/")[-1]
    model, _, owner = leaf.partition("@")
    return owner, INSTANCE_SUFFIX.sub("", model)


def merge(meshes: list[Mesh]) -> Mesh:
    out = Mesh()
    for m in meshes:
        base = len(out.vertices)
        out.vertices.extend(m.vertices)
        out.triangles.extend((a + base, b + base, c + base) for a, b, c in m.triangles)
    return out


def decimate(mesh: Mesh, budget: int) -> tuple[Mesh, float]:
    """Cluster vertices until the part fits the budget, and say how hard."""
    for step in DECIMATION_STEPS:
        trial = Mesh(list(mesh.vertices), list(mesh.triangles))
        trial.weld(step if step > 0 else 1e-6)
        if trial.triangle_count <= budget and len(trial.vertices) <= UINT16_MAX:
            return trial, step
    trial.weld(DECIMATION_STEPS[-1])
    return trial, DECIMATION_STEPS[-1]


def feature_edges(mesh: Mesh, angle_deg: float = FEATURE_ANGLE_DEG) -> list[tuple[int, int]]:
    """Edges where the surface actually turns, plus every open boundary.

    Drawing all three edges of every triangle buries a machined face under
    its own tessellation; drawing only the silhouette loses the holes. The
    dihedral angle is the standard middle, and it is the one a CAD viewer
    shows.
    """
    limit = math.cos(math.radians(angle_deg))
    normals = [mesh.face_normal(t) for t in mesh.triangles]

    faces: dict[tuple[int, int], list[int]] = {}
    for i, (a, b, c) in enumerate(mesh.triangles):
        for u, v in ((a, b), (b, c), (c, a)):
            faces.setdefault((u, v) if u < v else (v, u), []).append(i)

    out = []
    for edge, owners in faces.items():
        if len(owners) == 1:
            out.append(edge)
            continue
        if len(owners) != 2:
            # A non-manifold edge is a modelling artefact, and it is exactly
            # the kind of thing worth seeing.
            out.append(edge)
            continue
        n0, n1 = normals[owners[0]], normals[owners[1]]
        if n0[0] * n1[0] + n0[1] * n1[1] + n0[2] * n1[2] < limit:
            out.append(edge)
    return out


def edges_of(meshes: dict[str, Mesh], rules: dict) -> tuple[list, list]:
    """Feature edges for a part, with their own vertex list.

    Per component model, so each keeps its own dihedral angle: a group is
    usually bolts and the nuts that go on them, and those do not want the
    same threshold.
    """
    points: list[tuple[float, float, float]] = []
    pairs: list[tuple[int, int]] = []
    for model, one in meshes.items():
        angle = rules["edgeAngles"].get(model, FEATURE_ANGLE_DEG)
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
    return points, pairs


def slug(text: str) -> str:
    """A component model name, safe to use as an id and a DOM id."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def union_box(boxes):
    lo = [min(b[0][k] for b in boxes) for k in range(3)]
    hi = [max(b[1][k] for b in boxes) for k in range(3)]
    return tuple(lo), tuple(hi)


def relative(base: tuple, other: tuple) -> list[float]:
    """Where copy `other` sits, given geometry already placed as `base`.

    The mesh shipped is the base copy *in world space*, so the matrix a
    viewer needs is the one that carries world points of the base onto
    world points of the copy: Tk . T0^-1, which for rigid placements is
    R = Rb.Ra^T and t = tb - R.ta. Composing it the other way round
    (Ra^T.Rb) is the transform between their local frames, and applying
    that to world geometry puts six bolts in one place.

    SolidWorks stores the rotation column-major and the translation in
    metres while the mesh is in millimetres, which is the one place that
    scale is applied.

    Returned row-major, 3x4.
    """
    if not base or not other:
        return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]

    # a[j][k] is column j, so Ra[k][j] = a[j][k].
    a = [base[0:3], base[4:7], base[8:11]]
    b = [other[0:3], other[4:7], other[8:11]]
    ta = [base[12 + k] * TRANSLATION_MM for k in range(3)]
    tb = [other[12 + k] * TRANSLATION_MM for k in range(3)]

    r = [[sum(b[j][i] * a[j][k] for j in range(3)) for k in range(3)] for i in range(3)]
    t = [tb[i] - sum(r[i][k] * ta[k] for k in range(3)) for i in range(3)]

    out: list[float] = []
    for i in range(3):
        out.extend([r[i][0], r[i][1], r[i][2], t[i]])
    return out


def finish(slot: dict, ident: str, name: str, meshes: dict[str, Mesh],
           instances: int, rules: dict, decimate_it: bool = True) -> dict:
    """One exportable part from one or more full-resolution meshes."""
    points, pairs = edges_of(meshes, rules)
    if len(points) > UINT16_MAX:
        raise SystemExit(f"{ident} has {len(points)} edge vertices; raise its angle in the rules")

    mesh = merge(list(meshes.values()))
    mesh.weld(1e-5)
    before = mesh.triangle_count
    # Taken before clustering: decimation pulls vertices onto a grid and
    # would report the part as very slightly the wrong size.
    box = mesh.bbox()
    tolerance = 0.0
    if decimate_it:
        mesh, tolerance = decimate(mesh, slot["budget"])
    if len(mesh.vertices) > UINT16_MAX:
        raise SystemExit(f"{ident} still has {len(mesh.vertices)} vertices after decimation")

    return {
        "id": ident, "name": name, "note": slot["note"], "instances": instances,
        "mesh": mesh, "before": before, "tolerance": tolerance, "box": box,
        "edgePoints": points, "edgePairs": pairs, "transforms": [],
    }


def build_parts(slot: dict, rules: dict) -> list[dict]:
    """A group becomes one part, one per instance, or one per half.

    Three shapes, because three things are being looked at. Fasteners are a
    set and want to be one entry. A bulkhead is a *stack* — four plates that
    each do a different job — and hiding that behind one label is hiding the
    only interesting thing about it. And a grain is a tube whose inside is
    the point, which no amount of separating it from its neighbours will
    show: that one has to be cut.
    """
    full = {}
    for model, meshes in slot["buckets"].items():
        one = merge(meshes)
        one.weld(1e-5)
        full[model] = one

    if slot.get("split"):
        # Ordered along the axis, so "plate 1" is the one at the end you
        # would take off first rather than whichever the file listed first.
        singles = [m for meshes in slot["buckets"].values() for m in meshes]
        singles.sort(key=lambda m: (m.bbox()[0][0] + m.bbox()[1][0]) / 2)
        model = next(iter(slot["buckets"]))
        out = []
        for i, one in enumerate(singles):
            piece = Mesh(list(one.vertices), list(one.triangles))
            piece.weld(1e-5)
            out.append(finish(slot, f"{slot['id']}-{i + 1}",
                              f"{slot['name']} {i + 1}", {model: piece}, 1, rules))
        return out

    if slot.get("instance"):
        """One mesh, many placements.

        A threaded M10 stud is 66,034 triangles and, at full resolution,
        exactly 519 feature edges — the hex, the chamfers, and nothing
        else. Merge six of them and clamp the result to a triangle budget
        and both numbers go the wrong way: the shaft comes out faceted and
        scalloped, and the edge count *rises* to 2,050 as the clustered
        helix turns jagged. Shipped once with six transforms, the geometry
        is the CAD's own and nothing is clustered at all.
        """
        out = []
        for model, items in slot["placed"].items():
            base = items[0]
            local = Mesh(list(base.mesh.vertices), list(base.mesh.triangles))
            local.weld(1e-5)
            named = slot["names"].get(model)
            ident = slot["id"] if len(slot["placed"]) == 1 else f"{slot['id']}-{slug(model)}"
            part = finish(slot, ident, named or slot["name"], {model: local},
                          len(items), rules, decimate_it=False)
            # Every other placement, relative to the one that was shipped.
            part["transforms"] = [relative(base.transform, other.transform) for other in items]
            part["box"] = union_box([i.mesh.bbox() for i in items])
            out.append(part)
        return out

    axis_name = slot.get("halve")
    if axis_name:
        axis = {"x": 0, "y": 1, "z": 2}[axis_name]
        mesh = merge(list(full.values()))
        mesh.weld(1e-5)
        lo, hi = mesh.bbox()
        mid = (lo[axis] + hi[axis]) / 2
        model = next(iter(full))
        halves = cut_into_segments(mesh, [(mid, hi[axis]), (lo[axis], mid)], axis=axis)
        labels = ("upper", "lower") if axis == 1 else ("far", "near")
        out = []
        for half, label in zip(halves, labels):
            half.weld(1e-5)
            # The cut face is left open on purpose: an open boundary is drawn
            # as an edge, so the section outline draws itself and the bore
            # is what you see when the half swings away.
            out.append(finish(slot, f"{slot['id']}-{label}",
                              f"{slot['name']} ({label})", {model: half},
                              slot["instances"], rules))
        return out

    return [finish(slot, slot["id"], slot["name"], full, slot["instances"], rules)]


def load_rules(path: Path | None) -> dict:
    if path is None:
        return {"parts": [], "budget": 12000, "edgeAngles": {}}
    rules = json.loads(path.read_text(encoding="utf-8"))
    rules.setdefault("parts", [])
    rules.setdefault("budget", 12000)
    rules.setdefault("edgeAngles", {})
    return rules


def classify(owner: str, model: str, rules: dict) -> dict | None:
    """First matching rule wins, so order in the file is priority."""
    for rule in rules["parts"]:
        for pattern in rule["match"]:
            want_owner, _, want_model = pattern.rpartition("/")
            if want_model not in (model, "*"):
                continue
            if want_owner and want_owner != owner:
                continue
            return rule
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--parts", type=Path, help="grouping rules, JSON")
    ap.add_argument("--budget", type=int, help="triangles per part; overrides the rules file")
    ap.add_argument("--configuration", default="Default",
                    help="saved configuration to read. The default is 'Default' on purpose: "
                         "these assemblies were last saved in an Exploded View, and the "
                         "reader otherwise picks the most recent one and exports a motor "
                         "that is already in pieces")
    args = ap.parse_args()

    folder = args.file.parent
    by_lower = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}

    def resolve(name: str) -> bytes | None:
        found = by_lower.get(name.lower())
        return found.read_bytes() if found else None

    blocks = read_blocks(args.file.read_bytes())
    try:
        _whole, components, report = read_assembly(
            blocks, resolve=resolve, configuration=args.configuration
        )
    except Exception:
        # A file with only one saved configuration may not call it "Default".
        _whole, components, report = read_assembly(blocks, resolve=resolve)
    if not components:
        raise SystemExit("no components could be placed")

    rules = load_rules(args.parts)
    budget = args.budget or rules["budget"]

    groups: dict[str, dict] = {}
    unmatched: list[str] = []
    for comp in components:
        if comp.hidden:
            continue
        owner, model = instance_key(comp.name)
        rule = classify(owner, model, rules)
        if rule is None:
            unmatched.append(f"{owner}/{model}")
            continue
        slot = groups.setdefault(
            rule["id"],
            {"id": rule["id"], "name": rule["name"], "note": rule.get("note", ""),
             "budget": rule.get("budget", budget), "split": rule.get("split", False),
             "halve": rule.get("halve"), "instance": rule.get("instance", False),
             "names": rule.get("names", {}),
             "buckets": {}, "placed": {}, "instances": 0},
        )
        # Kept per component model so each keeps its own edge angle: a group
        # is usually bolts and the nuts that go on them, and they do not want
        # the same threshold.
        slot["buckets"].setdefault(model, []).append(comp.mesh)
        slot["placed"].setdefault(model, []).append(comp)
        slot["instances"] += 1

    if unmatched:
        missing = sorted(set(unmatched))
        raise SystemExit(
            "no rule matches these components, so they would be silently dropped:\n  "
            + "\n  ".join(missing)
        )

    parts = []
    for slot in groups.values():
        for built in build_parts(slot, rules):
            parts.append(built)

    # Along the axis, which is how the assembly comes apart on the page.
    for p in parts:
        lo, hi = p["box"]
        p["lo_x"], p["hi_x"] = lo[0], hi[0]
        p["centre_x"] = (lo[0] + hi[0]) / 2
    parts.sort(key=lambda p: p["centre_x"])

    lo = [min(p["box"][0][k] for p in parts) for k in range(3)]
    hi = [max(p["box"][1][k] for p in parts) for k in range(3)]
    centre = [(lo[k] + hi[k]) / 2 for k in range(3)]
    dims = [hi[k] - lo[k] for k in range(3)]
    span = [max(d, 1e-6) for d in dims]
    scale = 1.0 / dims[0]

    def quantise(point) -> bytes:
        out = b""
        for k in range(3):
            q = int(round((point[k] - lo[k]) / span[k] * UINT16_MAX))
            out += struct.pack("<H", max(0, min(UINT16_MAX, q)))
        return out

    blob = bytearray()
    entries = []
    for part in parts:
        mesh = part["mesh"]
        vstart = len(blob)
        for v in mesh.vertices:
            blob += quantise(v)
        istart = len(blob)
        for tri in mesh.triangles:
            blob += struct.pack("<3H", *tri)
        pstart = len(blob)
        for v in part["edgePoints"]:
            blob += quantise(v)
        estart = len(blob)
        for a, b in part["edgePairs"]:
            blob += struct.pack("<2H", a, b)

        entries.append({
            "id": part["id"],
            "name": part["name"],
            "note": part["note"],
            "instances": part["instances"],
            "start": round((part["lo_x"] - centre[0]) * scale, 6),
            "end": round((part["hi_x"] - centre[0]) * scale, 6),
            "start_mm": round(part["lo_x"], 1),
            "end_mm": round(part["hi_x"], 1),
            "vertexOffset": vstart,
            "vertexCount": len(mesh.vertices),
            "indexOffset": istart,
            "triangleCount": mesh.triangle_count,
            "edgeVertexOffset": pstart,
            "edgeVertexCount": len(part["edgePoints"]),
            "edgeOffset": estart,
            "edgeCount": len(part["edgePairs"]),
            # Empty for an ordinary part. For an instanced one, the placement
            # of each copy relative to the copy that was shipped, as a 3x4
            # row-major matrix in model units.
            "transforms": [[round(v, 6) for v in m] for m in part["transforms"]],
        })

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "mesh.bin").write_bytes(bytes(blob))

    manifest = {
        "source": args.file.name,
        "dimensions_mm": [round(d, 2) for d in dims],
        "scale": scale,
        "centre_mm": [round(c, 3) for c in centre],
        "quantisation": {"lo_mm": [round(v, 4) for v in lo], "span_mm": [round(v, 4) for v in span]},
        "sourceTriangles": sum(p["before"] for p in parts),
        "triangles": sum(p["mesh"].triangle_count for p in parts),
        "placed": report.placed,
        "unreadable": report.no_cached_mesh,
        "suppressed": report.suppressed,
        "configuration": report.configuration,
        "parts": entries,
    }
    (args.out / "assembly.json").write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )

    print(f"{args.file.name}: {report.placed} instances -> {len(entries)} parts, "
          f"{manifest['sourceTriangles']:,} -> {manifest['triangles']:,} triangles")
    print(f"  mesh.bin {(args.out / 'mesh.bin').stat().st_size / 1024:.0f} KB, "
          f"{report.no_cached_mesh} components unreadable, {report.suppressed} suppressed")
    for e, p in zip(entries, parts):
        cut = f"weld {p['tolerance']:.2f} mm" if p["tolerance"] else "full"
        if e["transforms"]:
            cut = f"full, x{len(e['transforms'])} instanced"
        print(f"    {e['name']:<26} x{e['instances']:<2} {e['start_mm']:>7.1f} - {e['end_mm']:>6.1f} mm  "
              f"{e['triangleCount']:>6,} tris  {e['edgeCount']:>6,} edges  {cut}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
