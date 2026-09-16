"""STL and 3MF writers.

Both take a Mesh in millimetres and emit bytes. Neither knows or cares which
decoder produced the mesh.

STL is a fixed-width binary format: an 80-byte header, a uint32 triangle
count, then 50 bytes per triangle. It carries no units, and the whole
ecosystem assumes millimetres.

3MF is an OPC package -- a ZIP holding `[Content_Types].xml`, `_rels/.rels`
and `3D/3dmodel.model`. Unlike STL it states its unit, indexes vertices
instead of repeating them, and requires a right-handed winding whose normals
point outward.
"""

from __future__ import annotations

import struct
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

from .mesh import Mesh

STL_HEADER_SIZE = 80
STL_TRIANGLE_SIZE = 50

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rel0" Target="/3D/3dmodel.model" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

MODEL_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def write_stl(mesh: Mesh, name: str = "sldprt-export") -> bytes:
    """Serialise a mesh as binary STL."""
    out = BytesIO()

    header = f"{name} - sldprt-export".encode("ascii", "replace")[:STL_HEADER_SIZE]
    out.write(header.ljust(STL_HEADER_SIZE, b"\x00"))
    out.write(struct.pack("<I", mesh.triangle_count))

    for tri in mesh.triangles:
        nx, ny, nz = mesh.face_normal(tri)
        a, b, c = (mesh.vertices[i] for i in tri)
        out.write(
            struct.pack(
                "<12fH",
                nx, ny, nz,
                a[0], a[1], a[2],
                b[0], b[1], b[2],
                c[0], c[1], c[2],
                0,
            )
        )

    return out.getvalue()


def build_3dmodel_xml(mesh: Mesh, name: str = "sldprt-export") -> str:
    """The 3D/3dmodel.model part of a 3MF package."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{MODEL_NS}">',
        "  <metadata name=\"Application\">sldprt-export</metadata>",
        f'  <metadata name="Title">{escape(name)}</metadata>',
        "  <resources>",
        '    <object id="1" type="model">',
        "      <mesh>",
        "        <vertices>",
    ]

    for x, y, z in mesh.vertices:
        parts.append(f'          <vertex x="{x:.6g}" y="{y:.6g}" z="{z:.6g}"/>')

    parts.append("        </vertices>")
    parts.append("        <triangles>")

    for a, b, c in mesh.triangles:
        parts.append(f'          <triangle v1="{a}" v2="{b}" v3="{c}"/>')

    parts.extend(
        [
            "        </triangles>",
            "      </mesh>",
            "    </object>",
            "  </resources>",
            "  <build>",
            '    <item objectid="1"/>',
            "  </build>",
            "</model>",
        ]
    )
    return "\n".join(parts) + "\n"


def write_3mf(mesh: Mesh, name: str = "sldprt-export") -> bytes:
    """Serialise a mesh as a 3MF package."""
    buf = BytesIO()
    # Deterministic timestamps keep output byte-identical between runs, which
    # makes the writers testable by hash.
    fixed_date = (2026, 1, 1, 0, 0, 0)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in (
            ("[Content_Types].xml", CONTENT_TYPES),
            ("_rels/.rels", RELS),
            ("3D/3dmodel.model", build_3dmodel_xml(mesh, name)),
        ):
            info = zipfile.ZipInfo(path, date_time=fixed_date)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text)

    return buf.getvalue()
