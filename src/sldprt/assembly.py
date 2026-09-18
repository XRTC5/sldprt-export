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

from collections.abc import Callable

from .blocks import Block, find_block, read_blocks
from .mesh import Mesh
from .savedmesh import TABLE_SIGNATURE, mesh_from_tables, read_saved_mesh, read_table

MAX_COMPONENTS = 2000
MAX_FACE_TABLES = 250_000
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
    from_part_files: int = 0
    suppressed: int = 0
    nested_subassembly: int = 0
    no_cached_mesh: int = 0
    hidden_excluded: int = 0
    configuration: str = ""
    configurations: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.placed + self.suppressed + self.hidden_excluded == self.references

    def summary(self) -> str:
        bits = [f"{self.placed} of {self.references} components placed"]
        if len(self.configurations) > 1:
            bits.insert(0, f"configuration {self.configuration!r} of {len(self.configurations)}")
        if self.nested_subassembly:
            bits.append(f"{self.nested_subassembly} nested subassemblies skipped")
        if self.from_part_files:
            bits.append(f"{self.from_part_files} read from part files")
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
    # The sequence does not always start at 1 - one assembly here begins at
    # 2 - so the first entry is located by its string marker and the run
    # continues from whatever number it carries. Assuming 1 found nothing at
    # all in that file and reported the assembly as having no meshes.
    first_marker = data.find(STRING_MARKER, 12)
    if first_marker < 4:
        return []
    base = struct.unpack_from("<I", data, first_marker - 4)[0]

    entries = []
    for i in range(count):
        signature = struct.pack("<I", base + i) + STRING_MARKER
        start = data.find(signature, c.at - 4 if i else first_marker - 4)
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


def compose(child: list[float], parent: list[float]) -> list[float]:
    """Place a child transform inside its parent's.

    Both act on row vectors — a point is transformed as v x M — so applying
    the child and then the parent is the single matrix child x parent.
    """
    out = [0.0] * 16
    for i in range(4):
        for j in range(4):
            out[i * 4 + j] = sum(child[i * 4 + k] * parent[k * 4 + j] for k in range(4))
    return out


IDENTITY = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
]


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
                continue
            # The marker has to identify one record. A second hit means the
            # wrong bytes could be read as this component's geometry.
            if data.find(marker, start + 1) >= 0:
                continue
            count = struct.unpack_from("<I", data, start + 12)[0]
            if not count or count > MAX_FACE_TABLES:
                continue
            records.append((start, count, entry))

        records.sort(key=lambda r: r[0])
        for i, (start, count, entry) in enumerate(records):
            end = records[i + 1][0] if i + 1 < len(records) else len(data)

            # A record declares how many face tables it owns, and that count
            # is the boundary that matters. Reading every table up to the next
            # record instead let one component swallow the tables of
            # everything between them - a 50 mm bolt came out 130 mm long and
            # a hex nut came out the size of a bulkhead plate.
            tables = []
            pos = start + 16
            while len(tables) < count:
                at = data.find(TABLE_SIGNATURE, pos)
                if at < 0 or at >= end:
                    break
                table = read_table(data, at)
                if table is None or table.end > end:
                    pos = at + 1
                    continue
                tables.append(table)
                pos = table.end

            if len(tables) != count:
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
    blocks: list[Block],
    include_hidden: bool = False,
    configuration: str | None = None,
    resolve: "Callable[[str], bytes | None] | None" = None,
) -> tuple[Mesh, list[Component], AssemblySummary]:
    """Rebuild an assembly from its cached component meshes.

    SolidWorks caches a mesh for some components and not others, so an
    assembly on its own is usually partial. `resolve` is given a component's
    file name and may return that file's bytes - from the folder beside the
    assembly, or from a set of files dropped together - and its own saved
    mesh is used instead. Without it the missing components stay missing and
    are counted.
    """
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
    if not configurations:
        raise AssemblyError("the assembly has no saved configuration")

    # Multiple configurations are ordinary - a chamber variant, a pressure
    # vessel variant - and refusing the file over it was wrong. Take the one
    # the assembly was last saved in unless a specific one is asked for.
    names = tuple(c.get("swName") or f"#{i}" for i, c in enumerate(configurations))
    chosen = None
    if configuration is not None:
        chosen = next((c for c, n in zip(configurations, names) if n == configuration), None)
        if chosen is None:
            raise AssemblyError(
                f"no configuration named {configuration!r}; this file has {', '.join(names)}"
            )
    if chosen is None:
        chosen = next(
            (c for c in configurations if c.get("swMostRecentConfiguration") == "YES"),
            configurations[0],
        )

    active = chosen.get("swName") or "#0"
    root = models.get(chosen.get("swModelRef"))
    if root is None:
        raise AssemblyError("the assembly configuration could not be resolved")
    root_file = files.get(root.get("swFileRef"))
    if root_file is None or root_file.get("swDocType") != "ASSEMBLY":
        raise AssemblyError("the root model is not an assembly")

    entries = read_directory(dir_block.inflate())
    by_name = {e.name: e for e in entries}
    by_id = {e.id: e for e in entries}
    stored = _component_meshes(blocks, entries)

    combined = Mesh()
    components: list[Component] = []
    report = AssemblySummary(
        references=0, placed=0, configuration=active, configurations=names
    )

    part_cache: dict[str, Mesh | None] = {}

    def from_part_file(model) -> Mesh | None:
        """The component's own saved mesh, read from its part file."""
        if resolve is None:
            return None
        record = files.get(model.get("swFileRef"))
        if record is None:
            return None
        path = record.get("swPath") or ""
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        if not name:
            return None
        if name in part_cache:
            mesh = part_cache[name]
            return Mesh(vertices=list(mesh.vertices), triangles=list(mesh.triangles)) if mesh else None

        data = resolve(name)
        mesh = None
        if data:
            try:
                mesh = read_saved_mesh(read_blocks(data))
            except Exception:
                mesh = None
        part_cache[name] = mesh
        if mesh is None:
            return None
        return Mesh(vertices=list(mesh.vertices), triangles=list(mesh.triangles))

    def walk(model, path: str, matrix: list[float], depth: int) -> None:
        """Place every part under `model`, descending into subassemblies.

        The mesh cache is keyed by the full instance path — for example
        `nozzle_bulkhead-1@macaw_II/plates-1@nozzle_bulkhead` — so a nested
        component is only findable by building that path as we descend. An
        earlier version matched top-level names only and reported assemblies
        whose every cached mesh sits one level down as having none at all.
        """
        if depth > 8:
            return
        model_name = model.get("swName")

        for reference in [r for r in model if _local(r.tag) == "swReference"]:
            a = reference.attrib
            report.references += 1

            if a.get("swSuppressed") == "YES":
                report.suppressed += 1
                continue

            child = models.get(a.get("swModelRef"))
            if child is None:
                report.no_cached_mesh += 1
                continue

            step = f"{a.get('swName')}-{a.get('swReferenceNumber')}@{model_name}"
            full = f"{path}/{step}" if path else step

            try:
                placement, mirrored = rigid_transform(a.get("swTransform"))
            except AssemblyError:
                report.no_cached_mesh += 1
                continue
            world = compose(placement, matrix)

            child_file = files.get(child.get("swFileRef"))
            if child_file is not None and child_file.get("swDocType") == "ASSEMBLY":
                walk(child, full, world, depth + 1)
                continue

            entry = by_name.get(full)
            mesh = None

            if entry is None:
                mesh = from_part_file(child)
                if mesh is None:
                    report.no_cached_mesh += 1
                    continue
                report.from_part_files += 1

            if mesh is None:
                # Repeated components alias to one stored mesh.
                target = entry
                seen: set[str] = set()
                while not target.chunk:
                    if target.id in seen:
                        break
                    seen.add(target.id)
                    nxt = by_id.get(target.alias)
                    if nxt is None:
                        break
                    target = nxt

                mesh = stored.get(target.id)

            if mesh is None or not mesh.triangles:
                mesh = from_part_file(child)
                if mesh is None:
                    report.no_cached_mesh += 1
                    continue
                report.from_part_files += 1

            placed = _apply(mesh, world)
            hidden = a.get("swHidden") == "YES"
            components.append(
                Component(name=full, hidden=hidden, mesh=placed, mirrored=mirrored)
            )

            if hidden and not include_hidden:
                report.hidden_excluded += 1
                continue

            report.placed += 1
            offset = len(combined.vertices)
            combined.vertices.extend(placed.vertices)
            combined.triangles.extend(
                (t[0] + offset, t[1] + offset, t[2] + offset) for t in placed.triangles
            )

    walk(root, "", IDENTITY, 0)

    if not combined.triangles:
        raise AssemblyError("the assembly has no visible component meshes")

    return combined, components, report
