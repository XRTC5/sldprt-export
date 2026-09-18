"""Everything a .SLDPRT tells us, read through the named block directory.

Supersedes an earlier version that brute-forced deflate offsets and searched
the payloads for float arrays. That version reported these parts as having
no saved mesh, which was wrong: the mesh is triangle strips in
`Contents/DisplayLists`, and the blocks have readable names once the nibble
swap is undone.

Read-only.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .blocks import Block, find_block, read_file
from .diagnostics import Diagnostics, inspect as diagnose
from .mesh import Mesh
from .savedmesh import read_saved_mesh

PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])

_UTF16 = re.compile(rb"(?:[\x20-\x7e]\x00){3,}")
_MATERIAL = re.compile(r"^SOLIDWORKS Materials\|(.+?)\|", re.I)
_STANDARD = re.compile(r"^(BS|ISO|ANSI|DIN|JIS|BS EN)\b", re.I)
_COMPONENT_EXT = (".stp", ".step", ".sldprt", ".sldasm", ".igs", ".iges")

_NOISE = re.compile(
    r"^(Default|Print|texture|Scheme\d*|Plain \w+|Scene .*|.*\.p2l|.*\.png|"
    r".*\.jpg|.*\.bmp|[\d\s.,;:|/\\-]*)$",
    re.I,
)

# Blocks worth reading strings out of. Everything else is geometry or cache.
TEXT_BLOCKS = ("Contents/", "swXmlContents/", "docProps/", "_MO_VERSION")


@dataclass
class Inspection:
    name: str
    size_bytes: int
    block_count: int
    blocks_verified: int
    preview_png: bytes | None
    material: str | None
    source_path: str | None
    mesh: Mesh | None = None
    diagnostics: Diagnostics | None = None
    features: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    standards: list[str] = field(default_factory=list)
    parasolid_bytes: int = 0
    assembly: object | None = None

    @property
    def has_saved_mesh(self) -> bool:
        return self.mesh is not None and bool(self.mesh.triangles)

    def to_dict(self) -> dict:
        d = self.diagnostics
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "block_count": self.block_count,
            "blocks_verified": self.blocks_verified,
            "material": self.material,
            "source_path": self.source_path,
            "features": self.features,
            "components": self.components,
            "standards": self.standards,
            "parasolid_bytes": self.parasolid_bytes,
            "has_preview": self.preview_png is not None,
            "has_saved_mesh": self.has_saved_mesh,
            "triangles": d.triangles if d else 0,
            "vertices": d.vertices if d else 0,
            "dimensions_mm": list(self.mesh.dimensions()) if self.mesh else None,
            "open_edges": d.boundary_edges if d else None,
            "closed": d.closed if d else None,
            "shells": d.shells if d else None,
            "volume_mm3": d.volume_mm3 if d and d.closed else None,
            "assembly": (
                {
                    "summary": self.assembly.summary(),
                    "placed": self.assembly.placed,
                    "references": self.assembly.references,
                    "configuration": self.assembly.configuration,
                    "configurations": list(self.assembly.configurations),
                    "complete": self.assembly.complete,
                }
                if self.assembly is not None
                else None
            ),
        }


def _parse_feature_xml(text: str) -> list[str]:
    out: list[str] = []
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return out
    for el in root.iter():
        name = el.get("Name")
        if not name:
            continue
        kind = el.get("Type") or el.tag
        dims = [d.text for d in el.findall("Dimension") if d.text]
        label = f"{name} ({kind})" if kind != name else name
        if dims:
            label += " — " + ", ".join(f"{d} mm" for d in dims)
        out.append(label)
    return out


def _preview(blocks: list[Block]) -> bytes | None:
    for name in ("PreviewPNG", "Preview"):
        block = find_block(blocks, name)
        if block is not None:
            data = block.inflate()
            if data.startswith(PNG_MAGIC):
                return data
    return None


def inspect(path: Path) -> Inspection:
    blocks = read_file(path)

    strings: list[str] = []
    features: list[str] = []
    parasolid = 0

    for block in blocks:
        if block.name.startswith("Contents/Config-") and "Partition" in block.name:
            parasolid += block.expanded
        if not block.name.startswith(TEXT_BLOCKS):
            continue
        # DisplayLists is geometry; scanning ten megabytes of it for text is
        # pure cost.
        if block.name.endswith("DisplayLists"):
            continue

        data = block.inflate()
        if b"<Keywords" in data[:4096]:
            features.extend(_parse_feature_xml(data.decode("utf-8", "replace")))
        for m in _UTF16.finditer(data):
            s = m.group().decode("utf-16-le", "ignore").strip()
            if 3 <= len(s) <= 120:
                strings.append(s)

    material = None
    source_path = None
    components: list[str] = []
    standards: list[str] = []
    seen: set[str] = set()

    for s in strings:
        if s in seen:
            continue
        seen.add(s)

        m = _MATERIAL.match(s)
        if m:
            material = material or m.group(1)
            continue
        if re.match(r"^[A-Za-z]:[\\/]", s) and s.lower().endswith(".sldprt"):
            source_path = source_path or s
            continue
        if s.lower().endswith(_COMPONENT_EXT):
            components.append(s)
            continue
        if _STANDARD.match(s):
            standards.append(s)
            continue
        if not features and not _NOISE.match(s) and re.match(r"^[A-Za-z][\w \-]{2,39}$", s):
            features.append(s)

    mesh = read_saved_mesh(blocks)
    assembly = None

    # An assembly stores no combined mesh; it has to be rebuilt from the
    # cached component meshes and their placements.
    if mesh is None or not mesh.triangles:
        if find_block(blocks, "FaceTessellations/Directory") is not None:
            from .assembly import AssemblyError, read_assembly

            try:
                mesh, _parts, assembly = read_assembly(blocks)
            except AssemblyError:
                mesh = None

    return Inspection(
        name=path.stem,
        size_bytes=path.stat().st_size,
        block_count=len(blocks),
        blocks_verified=sum(1 for b in blocks if b.verify()),
        preview_png=_preview(blocks),
        material=material,
        source_path=source_path,
        mesh=mesh,
        diagnostics=diagnose(mesh) if mesh and mesh.triangles else None,
        features=features[:80],
        components=components[:60],
        standards=standards[:40],
        parasolid_bytes=parasolid,
        assembly=assembly,
    )
