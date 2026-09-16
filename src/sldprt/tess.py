"""Decode SolidWorks saved tessellation.

Tessellation is serialised inside the `uiUserModelEnv_c` MFC stream under
`uoTempBodyTessData_c` / `uoTempFaceTessData_c`. Each vertex or normal array
is preceded by a four-word header:

    uint32 float_count     number of float32 values that follow
    uint32 tag             100 for a vertex/normal array
    uint32 stride          2 in every sample seen so far
    uint32 vertex_count    float_count == vertex_count * 3

Arrays come in pairs: positions first, then the matching normals. A normals
array is recognised by its values -- every entry is a unit vector, so the
magnitude of each triple is 1. Positions are in METRES.

The format carries no overall vertex/triangle count and no explicit
separator, so arrays are found by scanning for headers whose internal
arithmetic is self-consistent. That check is strong: float_count must equal
vertex_count * 3, and every float that follows must be a finite, sane
coordinate.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

ARRAY_TAG = 100

# Metres. A micron to ten metres covers any real part.
MIN_MAG = 1e-7
MAX_MAG = 10.0

MIN_VERTS = 3
MAX_VERTS = 1 << 22


@dataclass
class Array:
    """One decoded vertex or normal array."""

    offset: int
    count: int
    values: tuple[float, ...]
    kind: str  # "position" | "normal"

    def triples(self):
        v = self.values
        return [(v[i], v[i + 1], v[i + 2]) for i in range(0, len(v), 3)]

    def bbox(self):
        t = self.triples()
        xs = [p[0] for p in t]
        ys = [p[1] for p in t]
        zs = [p[2] for p in t]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _sane(v: float) -> bool:
    if math.isnan(v) or math.isinf(v):
        return False
    m = abs(v)
    return m == 0.0 or (MIN_MAG <= m <= MAX_MAG)


def _is_normal_array(values: tuple[float, ...]) -> bool:
    """Normals are unit vectors; positions almost never all are."""
    n = len(values) // 3
    if n == 0:
        return False
    checked = 0
    for i in range(0, min(n, 32) * 3, 3):
        mag = math.sqrt(
            values[i] ** 2 + values[i + 1] ** 2 + values[i + 2] ** 2
        )
        if abs(mag - 1.0) > 1e-3:
            return False
        checked += 1
    return checked > 0


def iter_arrays(data: bytes, tag: int = ARRAY_TAG):
    """Yield every self-consistent vertex/normal array in a payload.

    MFC CArchive writes without alignment padding, so these headers land on
    arbitrary byte offsets -- stepping 4 at a time misses three quarters of
    them. Instead, jump straight to each occurrence of the tag word and test
    the header that would precede it.
    """
    n = len(data)
    needle = struct.pack("<I", tag)
    search = 4
    last_end = 0

    while True:
        pos = data.find(needle, search)
        if pos < 0:
            return
        search = pos + 1
        off = pos - 4
        if off < last_end or off + 16 > n:
            continue

        float_count, _tag, _stride, vert_count = struct.unpack_from("<4I", data, off)
        if (
            MIN_VERTS <= vert_count <= MAX_VERTS
            and float_count == vert_count * 3
            and off + 16 + float_count * 4 <= n
        ):
            values = struct.unpack_from(f"<{float_count}f", data, off + 16)
            if all(_sane(v) for v in values):
                kind = "normal" if _is_normal_array(values) else "position"
                yield Array(off, vert_count, values, kind)
                last_end = off + 16 + float_count * 4
                search = last_end


def decode(payload: bytes) -> dict:
    """Summarise the tessellation in one payload."""
    arrays = list(iter_arrays(payload))
    positions = [a for a in arrays if a.kind == "position"]
    normals = [a for a in arrays if a.kind == "normal"]

    total = sum(a.count for a in positions)
    if positions:
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        for a in positions:
            (a0, a1, a2), (b0, b1, b2) = a.bbox()
            lo = [min(lo[0], a0), min(lo[1], a1), min(lo[2], a2)]
            hi = [max(hi[0], b0), max(hi[1], b1), max(hi[2], b2)]
        bbox_mm = tuple((hi[i] - lo[i]) * 1000.0 for i in range(3))
    else:
        bbox_mm = None

    return {
        "arrays": arrays,
        "position_arrays": len(positions),
        "normal_arrays": len(normals),
        "vertices": total,
        "bbox_mm": bbox_mm,
    }
