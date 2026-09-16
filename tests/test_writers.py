"""Writer and mesh tests against geometry whose answers are known exactly.

A cube is the right fixture here: its volume, triangle count and edge
topology are all known in advance, so a wrong winding, a failed weld or a
bad struct layout shows up as a number that disagrees rather than a file
that merely opens.
"""

from __future__ import annotations

import struct
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.mesh import Mesh, read_binary_stl
from sldprt.writers import MODEL_NS, write_3mf, write_stl

SIDE = 10.0


def cube(side: float = SIDE) -> Mesh:
    """An axis-aligned cube with outward-facing windings."""
    s = side
    v = [
        (0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
        (0, 0, s), (s, 0, s), (s, s, s), (0, s, s),
    ]
    faces = [
        (0, 3, 2, 1),  # bottom, -Z
        (4, 5, 6, 7),  # top, +Z
        (0, 1, 5, 4),  # front, -Y
        (2, 3, 7, 6),  # back, +Y
        (1, 2, 6, 5),  # right, +X
        (0, 4, 7, 3),  # left, -X
    ]
    mesh = Mesh()
    for a, b, c, d in faces:
        mesh.add_triangle(v[a], v[b], v[c])
        mesh.add_triangle(v[a], v[c], v[d])
    return mesh.weld()


def test_cube_topology():
    m = cube()
    assert m.triangle_count == 12
    assert len(m.vertices) == 8, "welding should collapse 36 emitted corners to 8"
    assert m.open_edges() == 0
    assert m.is_closed()


def test_cube_volume():
    m = cube()
    assert abs(m.volume_mm3() - SIDE**3) < 1e-6


def test_cube_dimensions():
    assert cube().dimensions() == (SIDE, SIDE, SIDE)


def test_weld_is_required_for_closure():
    """Unwelded, every edge is duplicated per-face and nothing is closed."""
    m = cube()
    raw = Mesh()
    for a, b, c in m.triangles:
        raw.add_triangle(m.vertices[a], m.vertices[b], m.vertices[c])
    assert raw.open_edges() > 0
    assert raw.weld().open_edges() == 0


def test_stl_layout_and_roundtrip():
    m = cube()
    data = write_stl(m, "cube")

    assert len(data) == 84 + 12 * 50, "80-byte header + uint32 count + 50 B/triangle"
    assert struct.unpack_from("<I", data, 80)[0] == 12

    back = read_binary_stl(data).weld()
    assert back.triangle_count == 12
    assert back.open_edges() == 0
    assert abs(back.volume_mm3() - SIDE**3) < 1e-3


def test_stl_normals_point_outward():
    """Each cube face normal must be an outward axis unit vector."""
    m = cube()
    data = write_stl(m)
    seen = set()
    for i in range(12):
        nx, ny, nz = struct.unpack_from("<3f", data, 84 + i * 50)
        assert abs((nx * nx + ny * ny + nz * nz) - 1.0) < 1e-5
        seen.add((round(nx), round(ny), round(nz)))
    assert seen == {
        (1, 0, 0), (-1, 0, 0),
        (0, 1, 0), (0, -1, 0),
        (0, 0, 1), (0, 0, -1),
    }


def test_3mf_package_structure():
    data = write_3mf(cube(), "cube")
    with zipfile.ZipFile(BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert names == {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"}
        assert zf.testzip() is None


def test_3mf_model_is_valid_xml_in_mm():
    data = write_3mf(cube(), "cube")
    with zipfile.ZipFile(BytesIO(data)) as zf:
        root = ElementTree.fromstring(zf.read("3D/3dmodel.model"))

    assert root.tag == f"{{{MODEL_NS}}}model"
    assert root.get("unit") == "millimeter"

    verts = root.findall(f".//{{{MODEL_NS}}}vertex")
    tris = root.findall(f".//{{{MODEL_NS}}}triangle")
    assert len(verts) == 8
    assert len(tris) == 12

    # Every index must reference a real vertex.
    for t in tris:
        for attr in ("v1", "v2", "v3"):
            assert 0 <= int(t.get(attr)) < len(verts)


def test_quad_strip_winding():
    """A grid-order quad must trianglulate without a bow-tie."""
    mesh = Mesh()
    mesh.add_quad_strip([(0, 0, 0), (0, 10, 0), (10, 0, 0), (10, 10, 0)])
    mesh.weld()
    assert mesh.triangle_count == 2
    n0 = mesh.face_normal(mesh.triangles[0])
    n1 = mesh.face_normal(mesh.triangles[1])
    assert n0 == n1, "both halves of a planar quad must face the same way"


def test_empty_mesh_is_not_closed():
    m = Mesh()
    assert not m.is_closed()
    assert write_stl(m)[80:84] == struct.pack("<I", 0)
