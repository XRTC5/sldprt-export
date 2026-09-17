"""Cut a mesh into separate solids at plane boundaries.

Translating the vertices of a continuous mesh does not separate it: any
triangle straddling a boundary keeps one vertex in each part and stretches
across the gap. To take an assembly apart the geometry has to actually be
cut, so every triangle ends up wholly inside one piece.

Each triangle is clipped against the slab of each segment by
Sutherland-Hodgman, which for a convex slab yields one convex polygon, then
fan-triangulated. Triangles entirely inside a slab are passed through
untouched, which is almost all of them.

The cut faces are left open. Capping them would need polygon triangulation
with forty-three holes per face, and leaving them open is what shows the
cooling channels in cross-section — which is the point of opening it.
"""

from __future__ import annotations

from .mesh import Mesh

EPS = 1e-9


def _clip_to_slab(poly, axis: int, lo: float, hi: float):
    """Clip a polygon to lo <= p[axis] <= hi."""
    for sign, bound in ((1, lo), (-1, hi)):
        if not poly:
            return []
        out = []
        n = len(poly)
        for i in range(n):
            a = poly[i]
            b = poly[(i + 1) % n]
            # Positive means inside for this half-space.
            da = sign * (a[axis] - bound)
            db = sign * (b[axis] - bound)

            if da >= -EPS:
                out.append(a)
            if (da > EPS and db < -EPS) or (da < -EPS and db > EPS):
                t = da / (da - db)
                out.append(tuple(a[k] + (b[k] - a[k]) * t for k in range(3)))
        poly = out
    return poly


def cut_into_segments(mesh: Mesh, bounds: list[tuple[float, float]], axis: int = 0):
    """Split a mesh into one Mesh per [lo, hi) slab along `axis`.

    Returns a list the same length as `bounds`. Triangles are assigned by
    clipping, so a triangle crossing a boundary contributes real geometry to
    both sides instead of being stretched between them.
    """
    parts = [Mesh() for _ in bounds]

    for tri in mesh.triangles:
        p0, p1, p2 = (mesh.vertices[i] for i in tri)
        tri_lo = min(p0[axis], p1[axis], p2[axis])
        tri_hi = max(p0[axis], p1[axis], p2[axis])

        for s, (lo, hi) in enumerate(bounds):
            if tri_hi < lo - EPS or tri_lo > hi + EPS:
                continue

            if tri_lo >= lo - EPS and tri_hi <= hi + EPS:
                # Wholly inside: the common case, and no work to do.
                parts[s].add_triangle(p0, p1, p2)
                continue

            poly = _clip_to_slab([p0, p1, p2], axis, lo, hi)
            for k in range(1, len(poly) - 1):
                parts[s].add_triangle(poly[0], poly[k], poly[k + 1])

    return parts
