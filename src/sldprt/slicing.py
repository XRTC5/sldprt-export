"""Cut a mesh into cross-section contours along an axis.

Produces the polylines you would see on a section drawing: for each plane,
the closed loops where it cuts the solid. A watertight mesh always yields
closed loops, so an unclosed chain is a signal that either the mesh or the
chaining is wrong -- it is reported rather than silently patched.

Method: intersect each triangle with the plane to get a segment, then chain
segments end to end into loops. Endpoints are quantised onto a grid before
chaining, because two triangles sharing an edge produce endpoints that agree
mathematically but differ in the last float bit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .mesh import Mesh

# Endpoints within this distance (mm) are the same point when chaining.
JOIN_TOLERANCE = 1e-4


@dataclass
class Slice:
    """One cross-section: the plane position and the loops it cuts."""

    position: float
    loops: list[list[tuple[float, float]]]
    closed: list[bool]

    @property
    def loop_count(self) -> int:
        return len(self.loops)

    @property
    def point_count(self) -> int:
        return sum(len(loop) for loop in self.loops)


def _intersect(p0, p1, p2, axis: int, at: float):
    """Segment where the plane axis=at cuts one triangle, or None."""
    pts = (p0, p1, p2)
    d = [p[axis] - at for p in pts]

    # Entirely one side, or lying in the plane (which the neighbours cover).
    if all(v > 0 for v in d) or all(v < 0 for v in d):
        return None
    if all(v == 0 for v in d):
        return None

    crossings = []
    for i in range(3):
        a, b = pts[i], pts[(i + 1) % 3]
        da, db = d[i], d[(i + 1) % 3]

        if da == 0.0:
            crossings.append(a)
            continue
        if da * db < 0:
            t = da / (da - db)
            crossings.append(
                tuple(a[k] + (b[k] - a[k]) * t for k in range(3))
            )

    # Deduplicate: a vertex exactly on the plane is found by two edges.
    unique = []
    for c in crossings:
        if not any(
            abs(c[0] - u[0]) < 1e-9 and abs(c[1] - u[1]) < 1e-9 and abs(c[2] - u[2]) < 1e-9
            for u in unique
        ):
            unique.append(c)

    return (unique[0], unique[1]) if len(unique) == 2 else None


def _flatten(point, axis: int) -> tuple[float, float]:
    """Drop the slice axis, keeping the other two in a consistent order."""
    return tuple(point[k] for k in range(3) if k != axis)


def _chain(segments, tolerance: float = JOIN_TOLERANCE):
    """Join segments end to end into loops."""
    if not segments:
        return [], []

    inv = 1.0 / tolerance

    def key(p):
        return (round(p[0] * inv), round(p[1] * inv))

    # Index segment ids by each endpoint, so walking a loop is a dict lookup
    # rather than a scan. Scanning makes chaining quadratic, which on the
    # thousands of segments a real part produces does not finish.
    at_point: dict[tuple[int, int], list[int]] = {}
    for i, (a, b) in enumerate(segments):
        at_point.setdefault(key(a), []).append(i)
        at_point.setdefault(key(b), []).append(i)

    used = [False] * len(segments)
    loops, closed_flags = [], []

    for seed in range(len(segments)):
        if used[seed]:
            continue
        used[seed] = True
        a, b = segments[seed]
        loop = [a, b]
        current = b
        closed = False

        while True:
            nxt = None
            for j in at_point.get(key(current), ()):
                if used[j]:
                    continue
                sa, sb = segments[j]
                # Step to whichever end of that segment is not where we are.
                if key(sa) == key(current):
                    nxt = sb
                elif key(sb) == key(current):
                    nxt = sa
                else:
                    continue
                used[j] = True
                break

            if nxt is None:
                break
            if key(nxt) == key(loop[0]):
                closed = True
                break
            loop.append(nxt)
            current = nxt

        loops.append(loop)
        closed_flags.append(closed)

    return loops, closed_flags


def slice_mesh(mesh: Mesh, axis: int = 0, count: int = 48, inset: float = 0.02) -> list[Slice]:
    """Cut `count` evenly spaced cross-sections along `axis`.

    `inset` pulls the first and last planes in from the bounding box by that
    fraction of the span, since a plane exactly on the extreme touches the
    surface tangentially and produces degenerate loops.
    """
    lo, hi = mesh.bbox()
    span = hi[axis] - lo[axis]
    if span <= 0 or count < 1:
        return []

    start = lo[axis] + span * inset
    end = hi[axis] - span * inset
    step = (end - start) / (count - 1) if count > 1 else 0.0

    # Bucket triangles by the axis range they span so each plane only tests
    # the triangles that can possibly cross it. Without this the cost is
    # count * triangles, which on 180k triangles and 48 planes is 8.6M tests.
    buckets: dict[int, list[int]] = {}
    for i, tri in enumerate(mesh.triangles):
        zs = [mesh.vertices[v][axis] for v in tri]
        first = 0 if step == 0 else max(0, math.floor((min(zs) - start) / step))
        last = count - 1 if step == 0 else min(count - 1, math.ceil((max(zs) - start) / step))
        for b in range(first, last + 1):
            buckets.setdefault(b, []).append(i)

    out: list[Slice] = []
    for n in range(count):
        at = start + step * n
        segments = []
        for i in buckets.get(n, ()):
            a, b, c = mesh.triangles[i]
            hit = _intersect(
                mesh.vertices[a], mesh.vertices[b], mesh.vertices[c], axis, at
            )
            if hit:
                segments.append((_flatten(hit[0], axis), _flatten(hit[1], axis)))

        loops, closed = _chain(segments)
        out.append(Slice(position=at, loops=loops, closed=closed))

    return out


def simplify(points: list[tuple[float, float]], tolerance: float, closed: bool = True):
    """Ramer-Douglas-Peucker, iterative so deep curves cannot blow the stack.

    Slice contours come out at roughly one point per triangle edge, which is
    far more than a screen needs. Dropping points that sit within `tolerance`
    of the line between their neighbours keeps the silhouette and removes
    most of the data.
    """
    n = len(points)
    if n < 3:
        return list(points)

    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue

        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)

        worst, worst_i = -1.0, -1
        for i in range(first + 1, last):
            px, py = points[i]
            if norm == 0.0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > worst:
                worst, worst_i = d, i

        if worst > tolerance:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))

    out = [points[i] for i in range(n) if keep[i]]
    # A closed loop that collapses below a triangle is noise, not geometry.
    if closed and len(out) < 3:
        return []
    return out


def simplify_slices(slices: list[Slice], tolerance: float) -> list[Slice]:
    out = []
    for s in slices:
        loops, closed = [], []
        for loop, was_closed in zip(s.loops, s.closed):
            simplified = simplify(loop, tolerance, was_closed)
            if simplified:
                loops.append(simplified)
                closed.append(was_closed)
        out.append(Slice(position=s.position, loops=loops, closed=closed))
    return out
