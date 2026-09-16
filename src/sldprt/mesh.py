"""Triangle mesh: assembly, welding and validation.

Sits between any decoder and any writer. The saved-mesh path and the
Parasolid path produce different inputs, but both end here, so the writers
never need to know which one ran.

SolidWorks and Parasolid store metres. Everything downstream -- STL by
convention, 3MF by declaration -- works in millimetres, so conversion
happens once, on the way in.
"""

from __future__ import annotations

import math
import struct
from collections import Counter
from dataclasses import dataclass, field

M_TO_MM = 1000.0

# Vertices closer than this are the same point. Tessellators emit each shared
# corner once per face, so without welding every edge looks open.
WELD_TOLERANCE_MM = 1e-6


@dataclass
class Mesh:
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    triangles: list[tuple[int, int, int]] = field(default_factory=list)

    # ---- construction ----------------------------------------------------

    def add_triangle(self, a, b, c) -> None:
        base = len(self.vertices)
        self.vertices.extend([a, b, c])
        self.triangles.append((base, base + 1, base + 2))

    def add_quad_strip(self, quad) -> None:
        """Add a 4-vertex face stored as a triangle strip.

        SolidWorks emits planar quads in grid order -- (0,1,2,3) walks two
        rows -- so the triangles are (0,1,2) and (2,1,3), not a fan. Getting
        this wrong produces a bow-tie and flips one normal.
        """
        p0, p1, p2, p3 = quad
        self.add_triangle(p0, p1, p2)
        self.add_triangle(p2, p1, p3)

    @classmethod
    def from_metres(cls, triangles_m) -> "Mesh":
        mesh = cls()
        for tri in triangles_m:
            mesh.add_triangle(*[tuple(c * M_TO_MM for c in p) for p in tri])
        return mesh

    # ---- cleanup ---------------------------------------------------------

    def weld(self, tolerance: float = WELD_TOLERANCE_MM) -> "Mesh":
        """Merge coincident vertices and drop degenerate triangles.

        Quantising to a grid is O(n) where pairwise comparison is O(n^2);
        at these tolerances the difference in result is nil and the
        difference in runtime is minutes.
        """
        if tolerance <= 0:
            return self

        inv = 1.0 / tolerance
        index: dict[tuple[int, int, int], int] = {}
        remap: list[int] = []
        merged: list[tuple[float, float, float]] = []

        for v in self.vertices:
            key = (round(v[0] * inv), round(v[1] * inv), round(v[2] * inv))
            got = index.get(key)
            if got is None:
                got = len(merged)
                index[key] = got
                merged.append(v)
            remap.append(got)

        tris = []
        for a, b, c in self.triangles:
            ra, rb, rc = remap[a], remap[b], remap[c]
            if ra != rb and rb != rc and ra != rc:
                tris.append((ra, rb, rc))

        self.vertices = merged
        self.triangles = tris
        return self

    # ---- measurement -----------------------------------------------------

    @property
    def triangle_count(self) -> int:
        return len(self.triangles)

    def bbox(self):
        if not self.vertices:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        xs = [v[0] for v in self.vertices]
        ys = [v[1] for v in self.vertices]
        zs = [v[2] for v in self.vertices]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def dimensions(self):
        lo, hi = self.bbox()
        return tuple(hi[i] - lo[i] for i in range(3))

    def open_edges(self) -> int:
        """Edges not shared by exactly two triangles.

        Zero means a closed, watertight surface -- the "Closed mesh · 0 open
        edges" a slicer needs. Any other number means the mesh has holes.
        """
        edges = Counter()
        for a, b, c in self.triangles:
            for u, v in ((a, b), (b, c), (c, a)):
                edges[(u, v) if u < v else (v, u)] += 1
        return sum(1 for n in edges.values() if n != 2)

    def is_closed(self) -> bool:
        return bool(self.triangles) and self.open_edges() == 0

    def volume_mm3(self) -> float:
        """Signed volume by the divergence theorem; meaningless if not closed."""
        total = 0.0
        for a, b, c in self.triangles:
            p, q, r = self.vertices[a], self.vertices[b], self.vertices[c]
            total += (
                p[0] * (q[1] * r[2] - q[2] * r[1])
                - p[1] * (q[0] * r[2] - q[2] * r[0])
                + p[2] * (q[0] * r[1] - q[1] * r[0])
            )
        return abs(total) / 6.0

    def face_normal(self, tri) -> tuple[float, float, float]:
        a, b, c = (self.vertices[i] for i in tri)
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length == 0.0:
            return (0.0, 0.0, 0.0)
        return (nx / length, ny / length, nz / length)

    def report(self) -> dict:
        d = self.dimensions()
        open_count = self.open_edges()
        return {
            "vertices": len(self.vertices),
            "triangles": len(self.triangles),
            "dimensions_mm": d,
            "open_edges": open_count,
            "closed": open_count == 0 and bool(self.triangles),
            "volume_mm3": self.volume_mm3() if open_count == 0 else None,
        }


def read_binary_stl(data: bytes) -> Mesh:
    """Parse binary STL. Used to verify our own writer round-trips."""
    count = struct.unpack_from("<I", data, 80)[0]
    mesh = Mesh()
    off = 84
    for _ in range(count):
        vals = struct.unpack_from("<12f", data, off)
        mesh.add_triangle(vals[3:6], vals[6:9], vals[9:12])
        off += 50
    return mesh
