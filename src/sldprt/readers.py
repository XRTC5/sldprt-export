"""Read mesh files into a Mesh.

`.SLDPRT` is included: SolidWorks saves a display tessellation in
`Contents/DisplayLists` and `savedmesh` decodes it, so a part converts
without SolidWorks and without a round trip through other CAD. Parts saved
without display data still cannot, and say so.

STEP and IGES stay out. They are B-rep like Parasolid and would need real
surface tessellation, so they are rejected with a reason rather than
half-supported.
"""

from __future__ import annotations

import re
import struct
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

from .blocks import read_blocks
from .mesh import Mesh
from .savedmesh import read_saved_mesh

BREP_FORMATS = {".step", ".stp", ".iges", ".igs", ".x_t", ".x_b"}
MESH_FORMATS = {".stl", ".3mf", ".obj", ".sldprt", ".sldasm"}


class UnsupportedFormat(Exception):
    pass


def read_stl(data: bytes) -> Mesh:
    """Binary or ASCII STL.

    The 84 + 50n size identity is the only reliable discriminator: an ASCII
    file can begin with "solid" and so can a binary one whose header happens
    to start that way.
    """
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + count * 50:
            mesh = Mesh()
            off = 84
            for _ in range(count):
                v = struct.unpack_from("<12f", data, off)
                mesh.add_triangle(v[3:6], v[6:9], v[9:12])
                off += 50
            return mesh.weld()

    text = data.decode("ascii", "replace")
    coords = [
        tuple(float(x) for x in m.groups())
        for m in re.finditer(
            r"vertex\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)", text
        )
    ]
    if not coords:
        raise UnsupportedFormat("not a readable STL")

    mesh = Mesh()
    for i in range(0, len(coords) - 2, 3):
        mesh.add_triangle(coords[i], coords[i + 1], coords[i + 2])
    return mesh.weld()


def read_3mf(data: bytes) -> Mesh:
    with zipfile.ZipFile(BytesIO(data)) as zf:
        model = next(
            (n for n in zf.namelist() if n.lower().endswith("3dmodel.model")), None
        )
        if model is None:
            raise UnsupportedFormat("3MF has no 3dmodel.model part")
        root = ElementTree.fromstring(zf.read(model))

    ns = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    q = (lambda t: f"{{{ns}}}{t}") if ns else (lambda t: t)

    # 3MF declares its unit; everything downstream is millimetres.
    scale = {
        "micron": 0.001, "millimeter": 1.0, "centimeter": 10.0,
        "inch": 25.4, "foot": 304.8, "meter": 1000.0,
    }.get(root.get("unit", "millimeter"), 1.0)

    mesh = Mesh()
    for obj in root.iter(q("object")):
        verts = [
            (float(v.get("x")) * scale, float(v.get("y")) * scale, float(v.get("z")) * scale)
            for v in obj.iter(q("vertex"))
        ]
        if not verts:
            continue
        for t in obj.iter(q("triangle")):
            a, b, c = (int(t.get(k)) for k in ("v1", "v2", "v3"))
            if max(a, b, c) < len(verts):
                mesh.add_triangle(verts[a], verts[b], verts[c])
    if not mesh.triangles:
        raise UnsupportedFormat("3MF contained no triangles")
    return mesh.weld()


def read_obj(data: bytes) -> Mesh:
    verts: list[tuple[float, float, float]] = []
    mesh = Mesh()

    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            verts.append(tuple(float(x) for x in parts[1:4]))
        elif parts[0] == "f" and len(parts) >= 4:
            # Faces may be n-gons and indices may be 1-based or negative,
            # with optional /vt/vn suffixes. Fan-triangulate.
            idx = []
            for token in parts[1:]:
                raw = int(token.split("/")[0])
                idx.append(raw - 1 if raw > 0 else len(verts) + raw)
            for i in range(1, len(idx) - 1):
                try:
                    mesh.add_triangle(verts[idx[0]], verts[idx[i]], verts[idx[i + 1]])
                except IndexError:
                    continue

    if not mesh.triangles:
        raise UnsupportedFormat("OBJ contained no faces")
    return mesh.weld()


def read_sldasm(data: bytes) -> Mesh:
    """An assembly, rebuilt from its cached component meshes."""
    from .assembly import AssemblyError, read_assembly

    try:
        mesh, _components, report = read_assembly(read_blocks(data))
    except AssemblyError as exc:
        raise UnsupportedFormat(str(exc)) from exc
    if not mesh.triangles:
        raise UnsupportedFormat("this assembly has no cached component meshes")
    mesh.report_note = report
    return mesh


def read_sldprt(data: bytes) -> Mesh:
    """The saved display mesh from a SolidWorks part."""
    mesh = read_saved_mesh(read_blocks(data))
    if mesh is None or not mesh.triangles:
        raise UnsupportedFormat(
            "This part has no saved display mesh - it stores only Parasolid "
            "B-rep, which needs a surface tessellator this does not have. "
            "Re-save it from SolidWorks with display data, or export a mesh."
        )
    return mesh


def load_mesh(filename: str, data: bytes) -> Mesh:
    ext = Path(filename).suffix.lower()

    if ext in BREP_FORMATS:
        raise UnsupportedFormat(
            f"{ext} stores B-rep surfaces, not triangles. Converting it needs a "
            "surface tessellator, which this app does not have. Export to STL, "
            "3MF or OBJ first."
        )
    if ext == ".sldprt":
        return read_sldprt(data)
    if ext == ".sldasm":
        return read_sldasm(data)
    if ext == ".stl":
        return read_stl(data)
    if ext == ".3mf":
        return read_3mf(data)
    if ext == ".obj":
        return read_obj(data)

    raise UnsupportedFormat(f"unsupported format: {ext or 'no extension'}")
