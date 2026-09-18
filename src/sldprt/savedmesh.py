"""Decode the saved display mesh from a .SLDPRT.

SolidWorks stores a tessellation for display in `Contents/DisplayLists`, as
a run of tables. Each table is six descriptors, each one a small header —
`element size | kind | 2 | count` — followed by its data:

    0: uint32 per-strip vertex counts
    1: float32 x3 positions      (kind 100)
    2: float32 x3 normals        (kind 100, or absent)
    3: uint32                    per-strip extras
    4: uint32 per-strip 2n-2
    5: uint8                     per-strip extras

The geometry is **triangle strips**, not a triangle soup: a strip of n
vertices is n-2 triangles, so a table's triangle count is
`total vertices - 2 * strips`. Reading the position array as consecutive
triples — which an earlier pass here did — yields nonsense, which is how
this data got missed entirely and the files were wrongly reported as having
no saved mesh.

Format facts come from a converter written by a colleague of RTC's, which
cites the cadmpeg project's `sldprt.md` (CC BY 4.0). This is an independent
Python implementation.

Read-only.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .blocks import Block, find_block
from .mesh import Mesh

# element size, kind — in order.
DESCRIPTORS = ((4, 8), (12, 100), (12, 100), (4, 8), (4, 8), (1, 8))

# The first descriptor's header doubles as the table's signature.
TABLE_SIGNATURE = struct.pack("<3I", 4, 8, 2)

MAX_STRIPS = 250_000
MAX_STRIP_VERTS = 1_000_000
MAX_DESC_COUNT = 2_000_000

MESH_BLOCKS = ("Contents/DisplayLists", "FaceTessellations")


@dataclass
class Table:
    """One strip table: where each descriptor's data starts, and how long."""

    start: int
    end: int
    descriptors: list[tuple[int, int]]
    lengths: list[int]

    @property
    def triangles(self) -> int:
        return sum(self.lengths) - 2 * len(self.lengths)

    @property
    def has_normals(self) -> bool:
        return self.descriptors[2][1] == self.descriptors[1][1]


def read_table(data: bytes, at: int) -> Table | None:
    """Parse a table at `at`, or None if the layout does not hold.

    Every check here is a rejection test: the signature appears by chance in
    compressed data, so a candidate is only a table if all six descriptors
    line up and the per-strip arithmetic agrees with itself.
    """
    pos = at
    descriptors: list[tuple[int, int]] = []

    for size, kind in DESCRIPTORS:
        if pos + 16 > len(data):
            return None
        d_size, d_kind, two, count = struct.unpack_from("<4I", data, pos)
        if d_size != size or d_kind != kind or two != 2:
            return None
        if count > MAX_DESC_COUNT or pos + 16 + count * size > len(data):
            return None
        descriptors.append((pos + 16, count))
        pos += 16 + size * count

    strips = descriptors[0][1]
    if not strips or strips > MAX_STRIPS:
        return None

    lengths: list[int] = []
    total = 0
    for j in range(strips):
        n = struct.unpack_from("<I", data, descriptors[0][0] + 4 * j)[0]
        if n < 3 or n > MAX_STRIP_VERTS:
            return None
        lengths.append(n)
        total += n

    # Positions must cover every strip vertex; normals either match or are
    # absent; and the 2n-2 array must agree strip for strip.
    if descriptors[1][1] != total:
        return None
    if descriptors[2][1] not in (0, total):
        return None
    if descriptors[4][1] != strips:
        return None
    for j in range(strips):
        if struct.unpack_from("<I", data, descriptors[4][0] + 4 * j)[0] != 2 * lengths[j] - 2:
            return None

    return Table(start=at, end=pos, descriptors=descriptors, lengths=lengths)


def iter_tables(data: bytes):
    """Every strip table in a decompressed block."""
    pos = 0
    while True:
        at = data.find(TABLE_SIGNATURE, pos)
        if at < 0:
            return
        table = read_table(data, at)
        if table is None:
            pos = at + 1
            continue
        yield table
        pos = table.end


def mesh_from_tables(data: bytes, tables: list[Table], scale: float = 1000.0) -> Mesh:
    """Build a triangle mesh from strip tables.

    Positions are in metres; `scale` converts to the millimetres everything
    downstream works in. Strip winding alternates, so every second triangle
    is reversed to keep the surface consistently oriented.
    """
    mesh = Mesh()

    for table in tables:
        base, count = table.descriptors[1]
        verts = struct.unpack_from(f"<{count * 3}f", data, base)

        at = 0
        for n in table.lengths:
            for k in range(n - 2):
                i = at + k
                a = (verts[i * 3] * scale, verts[i * 3 + 1] * scale, verts[i * 3 + 2] * scale)
                b = (verts[(i + 1) * 3] * scale, verts[(i + 1) * 3 + 1] * scale, verts[(i + 1) * 3 + 2] * scale)
                c = (verts[(i + 2) * 3] * scale, verts[(i + 2) * 3 + 1] * scale, verts[(i + 2) * 3 + 2] * scale)
                # Odd triangles in a strip wind the other way.
                if k % 2:
                    mesh.add_triangle(a, c, b)
                else:
                    mesh.add_triangle(a, b, c)
            at += n

    return mesh


def read_saved_mesh(blocks: list[Block], weld: float = 1e-4) -> Mesh | None:
    """The saved display mesh, or None when the part has none."""
    for name in MESH_BLOCKS:
        block = find_block(blocks, name)
        if block is None:
            continue
        data = block.inflate()
        tables = list(iter_tables(data))
        if not tables:
            continue
        mesh = mesh_from_tables(data, tables)
        if mesh.triangles:
            return mesh.weld(weld)
    return None
