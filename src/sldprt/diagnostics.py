"""Geometry checks on a finished mesh.

`Mesh.report()` answers the printing question — is it watertight, how big is
it. This answers the harder one: if it is *not* watertight, what exactly is
wrong with it. Boundary edges, non-manifold edges, inconsistent winding,
duplicate faces and disconnected shells all look identical in an open-edge
count and need completely different fixes.

Ported from the checks in a converter written by a colleague of RTC's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .mesh import Mesh


@dataclass
class Diagnostics:
    triangles: int
    vertices: int
    area_mm2: float
    volume_mm3: float
    boundary_edges: int
    non_manifold_edges: int
    winding_errors: int
    duplicate_triangles: int
    degenerate_triangles: int
    shells: int

    @property
    def closed(self) -> bool:
        return self.boundary_edges == 0 and self.non_manifold_edges == 0

    @property
    def consistent(self) -> bool:
        return self.winding_errors == 0

    @property
    def printable(self) -> bool:
        """What a slicer actually needs: closed, consistent, positive volume."""
        return self.closed and self.consistent and self.volume_mm3 > 0

    def summary(self) -> str:
        bits = [f"{self.triangles:,} triangles", f"{self.vertices:,} vertices"]
        if self.shells > 1:
            bits.append(f"{self.shells} shells")
        if self.boundary_edges:
            bits.append(f"{self.boundary_edges:,} boundary edges")
        if self.non_manifold_edges:
            bits.append(f"{self.non_manifold_edges:,} non-manifold edges")
        if self.winding_errors:
            bits.append(f"{self.winding_errors:,} winding errors")
        if self.duplicate_triangles:
            bits.append(f"{self.duplicate_triangles:,} duplicate faces")
        if self.degenerate_triangles:
            bits.append(f"{self.degenerate_triangles:,} degenerate")
        return " · ".join(bits)


class _Union:
    """Union-find, for counting how many separate shells a mesh is in."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def inspect(mesh: Mesh) -> Diagnostics:
    verts = mesh.vertices
    union = _Union(len(verts))

    edges: dict[tuple[int, int], list[int]] = {}
    seen_faces: set[tuple[int, int, int]] = set()

    area = 0.0
    volume = 0.0
    duplicates = 0
    degenerate = 0

    for tri in mesh.triangles:
        a, b, c = (verts[i] for i in tri)

        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        magnitude = math.sqrt(nx * nx + ny * ny + nz * nz)
        if magnitude == 0.0:
            degenerate += 1
            continue
        area += magnitude / 2

        # Divergence theorem: signed volume of the tetrahedron to the origin.
        volume += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6

        key = tuple(sorted(tri))
        if key in seen_faces:
            duplicates += 1
        seen_faces.add(key)

        for u, v in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edge = (u, v) if u < v else (v, u)
            # Track direction: on a consistently wound closed surface each
            # edge is traversed once each way, so the signs cancel.
            edges.setdefault(edge, []).append(1 if u < v else -1)
            union.union(u, v)

    boundary = 0
    non_manifold = 0
    winding = 0
    for uses in edges.values():
        if len(uses) == 1:
            boundary += 1
        elif len(uses) > 2:
            non_manifold += 1
        elif sum(uses) != 0:
            winding += 1

    used = {i for tri in mesh.triangles for i in tri}
    shells = len({union.find(i) for i in used}) if used else 0

    return Diagnostics(
        triangles=len(mesh.triangles),
        vertices=len(verts),
        area_mm2=area,
        volume_mm3=abs(volume),
        boundary_edges=boundary,
        non_manifold_edges=non_manifold,
        winding_errors=winding,
        duplicate_triangles=duplicates,
        degenerate_triangles=degenerate,
        shells=shells,
    )
