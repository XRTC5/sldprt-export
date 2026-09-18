"""Assemble a .SLDASM from its cached component meshes.

An assembly stores no combined mesh. It stores each component's tessellation
once, in `FaceTessellations/<chunk>` blocks, and three things that put them
back together:

  * `swXmlContents/COMPINSTANCETREE` — the instance tree: which model each
    component is, its placement as a 4x4 transform, and whether it is
    suppressed or hidden.
  * `FaceTessellations/Directory` — a binary directory mapping each instance
    name to the chunk and record holding its mesh. Repeated components alias
    to a single stored mesh rather than duplicating it.
  * The chunks themselves, holding the same strip tables a part file uses.

Format facts come from a converter written by a colleague of RTC's, which
cites the cadmpeg project's `sldprt.md` (CC BY 4.0). This is an independent
Python implementation; the XML is parsed with the standard library rather
than by hand, since Python has a parser and a browser does not.

Read-only.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from xml.etree import ElementTree

from .blocks import Block, find_block
from .mesh import Mesh
from .savedmesh import TABLE_SIGNATURE, mesh_from_tables, read_table

MAX_COMPONENTS = 2000
MAX_DIRECTORY_CAPACITY = 100_000

# Transforms carry translation in metres while the meshes are in millimetres.
TRANSLATION_SCALE = 1000.0

# A transform must be a rigid motion: orthonormal rotation, unit determinant.
ORTHONORMAL_TOLERANCE = 1e-6

# Every string in the directory is introduced by this marker.
STRING_MARKER = bytes([0xFF, 0xFE, 0xFF])


class AssemblyError(Exception):
    pass


@dataclass
class Component:
    name: str
    hidden: bool
    mesh: Mesh
    mirrored: bool


@dataclass
class AssemblySummary:
    """What went in and, more importantly, what did not.

    A partial assembly that reports itself as complete is worse than a
    refusal: the file opens, looks plausible, and is missing parts. Every
    component that could not be placed is counted with its reason.
    """

    references: int
    placed: int
    suppressed: int = 0
    nested_subassembly: int = 0
    no_cached_mesh: int = 0
    hidden_excluded: int = 0

    @property
    def complete(self) -> bool:
        return self.placed + self.suppressed + self.hidden_excluded == self.references

    def summary(self) -> str:
        bits = [f"{self.placed} of {self.references} components placed"]
        if self.nested_subassembly:
            bits.append(f"{self.nested_subassembly} nested subassemblies skipped")
        if self.no_cached_mesh:
            bits.append(f"{self.no_cached_mesh} without a cached mesh")
        if self.suppressed:
            bits.append(f"{self.suppressed} suppressed")
        if self.hidden_excluded:
            bits.append(f"{self.hidden_excluded} hidden")
        return " · ".join(bits)


@dataclass
class DirectoryEntry:
    name: str
    stamp: int
    config: int
    alias: str
    id: str
    chunk: str
    index: int


class _Cursor:
    """Little-endian reader over the directory blob."""

    def __init__(self, data: bytes):
        self.data = data
        self.at = 0

    def take(self, n: int) -> int:
        if self.at + n > len(self.data):
            raise AssemblyError("the component mesh directory is truncated")
        start = self.at
        self.at += n
        return start

    def u32(self) -> int:
        return struct.unpack_from("<I", self.data, self.take(4))[0]

    def key(self) -> str:
        start = self.take(8)
        return self.data[start : start + 8].hex()

    def at_string(self) -> bool:
        return self.data[self.at : self.at + 3] == STRING_MARKER

    def align_to_string(self, limit: int = 4) -> None:
        """Step over any key padding until the next string begins.

        Some writers pad each key out to sixteen bytes and some do not.
        Looking ahead one group to decide is not enough — the marker eight
        bytes away may belong to the *next* field, which makes the reader
        swallow a key it needed.
        """
        for _ in range(limit):
            if self.at_string():
                return
            if self.at + 8 > len(self.data):
                return
            self.at += 8

    def string(self) -> str:
        """A length-prefixed UTF-16LE string behind an FF FE FF marker.

        The length is one byte, escalating to a uint16 and then a uint32 when
        it saturates — the usual MFC CString-on-disk encoding.
        """
        q = self.take(3)
        if self.data[q : q + 3] != STRING_MARKER:
            raise AssemblyError("unsupported string layout in the mesh directory")
        n = self.data[self.take(1)]
        if n == 0xFF:
            n = struct.unpack_from("<H", self.data, self.take(2))[0]
            if n == 0xFFFF:
                n = self.u32()
        if n > 4096:
            raise AssemblyError("a component mesh name is too long")
        start = self.take(2 * n)
        return self.data[start : start + 2 * n].decode("utf-16-le")


def read_directory(data: bytes) -> list[DirectoryEntry]:
    c = _Cursor(data)
    if c.u32() != 2:
        raise AssemblyError("unsupported assembly mesh directory version")

    capacity, count = c.u32(), c.u32()
    if not capacity or capacity > MAX_DIRECTORY_CAPACITY or not count or count > MAX_COMPONENTS:
        raise AssemblyError("assembly exceeds the component directory limits")

    # Entries are located by their own signature rather than walked field by
    # field. Each begins with its sequence number immediately followed by a
    # string marker, which is unambiguous, and it means the trailing fields
    # after the chunk name - whose count varies between writers - never have
    # to be guessed at. Walking them blind desynchronised the whole directory
    # after the first entry.
    entries = []
    for i in range(count):
        signature = struct.pack("<I", i + 1) + STRING_MARKER
        start = data.find(signature, c.at - 4 if i else 0)
        if start < 0:
            break
        c.at = start + 4

        name = c.string()
        stamp, config = c.u32(), c.u32()
        c.u32()  # reserved
        alias = c.key()
        ident = c.key()
        # Whatever sits between the keys and the chunk name is padding whose
        # width varies by writer, so step to the string rather than assume it.
        c.align_to_string()
        chunk = c.string()
        index = c.u32()
        entries.append(
            DirectoryEntry(name, stamp, config, alias, ident, chunk, index)
        )
    return entries


def rigid_transform(text: str | None) -> tuple[list[float], bool]:
    """Validate a 4x4 placement and report whether it mirrors.

    Anything that is not a rigid motion — a scale, a shear — would silently
    distort the component, so it is rejected rather than applied.
    """
    values = [float(v) for v in (text or "").split()] if text else []
    if len(values) != 16 or any(not math.isfinite(v) for v in values):
        raise AssemblyError("a component has an invalid saved transform")
    if any(abs(values[i]) > 1e-12 for i in (3, 7, 11)) or abs(values[15] - 1) > 1e-12:
        raise AssemblyError("a component transform is not affine")

    rows = [values[0:3], values[4:7], values[8:11]]

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    cross = [
        rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1],
        rows[1][2] * rows[2][0] - rows[1][0] * rows[2][2],
        rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0],
    ]
    det = dot(rows[0], cross)

    if (
        any(abs(dot(r, r) - 1) > ORTHONORMAL_TOLERANCE for r in rows)
        or abs(dot(rows[0], rows[1])) > ORTHONORMAL_TOLERANCE
        or abs(dot(rows[0], rows[2])) > ORTHONORMAL_TOLERANCE
        or abs(dot(rows[1], rows[2])) > ORTHONORMAL_TOLERANCE
        or abs(abs(det) - 1) > ORTHONORMAL_TOLERANCE
    ):
        raise AssemblyError("only rigid component transforms are supported")

    return values, det < 0


def _apply(mesh: Mesh, m: list[float]) -> Mesh:
    out = Mesh()
    placed = [
        tuple(
            v[0] * m[k] + v[1] * m[4 + k] + v[2] * m[8 + k] + TRANSLATION_SCALE * m[12 + k]
            for k in range(3)
        )
        for v in mesh.vertices
    ]
    out.vertices = placed
    out.triangles = list(mesh.triangles)
    return out


def _component_meshes(blocks: list[Block], entries: list[DirectoryEntry]) -> dict[str, Mesh]:
    """Decode each stored component mesh, one chunk at a time."""
    by_chunk: dict[str, list[DirectoryEntry]] = {}
    for e in entries:
        if e.chunk:
            by_chunk.setdefault(e.chunk, []).append(e)

    meshes: dict[str, Mesh] = {}
    for chunk, group in by_chunk.items():
        block = find_block(blocks, f"FaceTessellations/{chunk}")
        if block is None:
            raise AssemblyError(f"component mesh chunk {chunk} is missing")
        data = block.inflate()

        # Each component's record is found by a marker of its index plus the
        # first eight bytes of its id.
        records = []
        for entry in group:
            marker = struct.pack("<I", entry.index) + bytes.fromhex(entry.id)
            start = data.find(marker)
            if start < 0 or start + 16 > len(data):
                raise AssemblyError(f"component {entry.name} has no mesh record")
            count = struct.unpack_from("<I", data, start + 12)[0]
            records.append((start, count, entry))

        records.sort(key=lambda r: r[0])
        for i, (start, count, entry) in enumerate(records):
            end = records[i + 1][0] if i + 1 < len(records) else len(data)
            tables = []
            pos = start + 16
            while True:
                at = data.find(TABLE_SIGNATURE, pos)
                if at < 0 or at >= end:
                    break
                table = read_table(data, at)
                if table is None or table.end > end:
                    pos = at + 1
                    continue
                tables.append(table)
                pos = table.end
            if not tables:
                continue
            # Weld only numerical round-off, never across components.
            meshes[entry.id] = mesh_from_tables(data, tables).weld(1e-9)

    return meshes


def _local(tag: str) -> str:
    """Tag without its namespace.

    The instance tree is namespaced (`sw2003/schema`). A hand-written parser
    that only matches local names never notices; ElementTree keeps the
    namespace on every tag, so comparisons against bare names all fail.
    """
    return tag.rsplit("}", 1)[-1]


def read_assembly(
    blocks: list[Block], include_hidden: bool = False
) -> tuple[Mesh, list[Component], AssemblySummary]:
    """Rebuild the assembly from its cached component meshes."""
    tree_block = find_block(blocks, "swXmlContents/COMPINSTANCETREE")
    dir_block = find_block(blocks, "FaceTessellations/Directory")
    if tree_block is None or dir_block is None:
        raise AssemblyError("this file has no cached assembly component meshes")

    root_xml = ElementTree.fromstring(tree_block.inflate().decode("utf-8", "replace"))

    def one(parent, tag):
        found = [c for c in parent if _local(c.tag) == tag]
        if len(found) != 1:
            raise AssemblyError(f"the assembly has no unambiguous {tag} record")
        return found[0]

    # Direct children only. iter() walks the whole tree and picks up the
    # per-model configuration records nested inside swModel, which look
    # identical and make every assembly appear to have dozens of them.
    files = {f.get("id"): f for f in one(root_xml, "swHeader") if _local(f.tag) == "swFile"}
    models = {m.get("id"): m for m in one(root_xml, "swModelList") if _local(m.tag) == "swModel"}
    configurations = [
        c for c in one(root_xml, "swConfigurationList") if _local(c.tag) == "swConfiguration"
    ]
    if len(configurations) != 1:
        raise AssemblyError("assembly conversion requires exactly one saved configuration")

    configuration = configurations[0]
    root = models.get(configuration.get("swModelRef"))
    if root is None:
        raise AssemblyError("the assembly configuration could not be resolved")
    root_file = files.get(root.get("swFileRef"))
    if root_file is None or root_file.get("swDocType") != "ASSEMBLY":
        raise AssemblyError("the root model is not an assembly")

    references = [r for r in root if _local(r.tag) == "swReference"]
    if not references or len(references) > MAX_COMPONENTS:
        raise AssemblyError("the assembly has no supported component instances")

    entries = read_directory(dir_block.inflate())
    by_name = {e.name: e for e in entries}
    by_id = {e.id: e for e in entries}
    stored = _component_meshes(blocks, entries)

    combined = Mesh()
    components: list[Component] = []
    root_name = root.get("swName")
    report = AssemblySummary(references=len(references), placed=0)

    for reference in references:
        a = reference.attrib
        if a.get("swSuppressed") == "YES":
            report.suppressed += 1
            continue
        model = models.get(a.get("swModelRef"))
        if model is None:
            report.no_cached_mesh += 1
            continue
        model_file = files.get(model.get("swFileRef"))
        if model_file is None or model_file.get("swDocType") != "PART":
            # A nested subassembly has its own instance tree and its own
            # cached meshes; recursing into it is not implemented.
            report.nested_subassembly += 1
            continue

        name = f"{a.get('swName')}-{a.get('swReferenceNumber')}"
        entry = by_name.get(f"{name}@{root_name}")
        if entry is None:
            report.no_cached_mesh += 1
            continue

        # Repeated components alias to one stored mesh.
        target = entry
        seen: set[str] = set()
        while not target.chunk:
            if target.id in seen:
                raise AssemblyError("circular component reference in the mesh cache")
            seen.add(target.id)
            nxt = by_id.get(target.alias)
            if nxt is None:
                break
            target = nxt

        mesh = stored.get(target.id)
        if mesh is None or not mesh.triangles:
            report.no_cached_mesh += 1
            continue

        matrix, mirrored = rigid_transform(a.get("swTransform"))
        placed = _apply(mesh, matrix)
        hidden = a.get("swHidden") == "YES"

        components.append(Component(name=name, hidden=hidden, mesh=placed, mirrored=mirrored))
        if hidden and not include_hidden:
            report.hidden_excluded += 1
            continue
        report.placed += 1

        offset = len(combined.vertices)
        combined.vertices.extend(placed.vertices)
        combined.triangles.extend(
            (t[0] + offset, t[1] + offset, t[2] + offset) for t in placed.triangles
        )

    if not combined.triangles:
        raise AssemblyError("the assembly has no visible component meshes")

    return combined, components, report
