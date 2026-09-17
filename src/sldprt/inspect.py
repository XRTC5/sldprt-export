"""Everything a .SLDPRT will tell us without SolidWorks.

The B-rep is locked behind Parasolid's external schema and most parts carry
no saved mesh, so 3D from the part file alone is out of reach. What is fully
recoverable is the preview render, the feature tree, the material, and the
list of referenced components -- enough to audit a part you cannot open.

Read-only.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .container import extract
from .tess import decode

# Below this, decoded tessellation is display scaffolding, not a part.
MIN_REAL_VERTS = 100

# A reference plane: a flat quad.
PLANE_VERTS = 4
FLAT_MM = 0.5

_UTF16 = re.compile(rb"(?:[ -~]\x00){3,}")

_MATERIAL = re.compile(r"^SOLIDWORKS Materials\|(.+?)\|", re.I)

# Things worth surfacing, by the shape of the string rather than a fixed list.
_COMPONENT_EXT = (".stp", ".step", ".sldprt", ".sldasm", ".igs", ".iges")
_STANDARD = re.compile(r"^(BS|ISO|ANSI|DIN|JIS|BS EN)\b", re.I)

_NOISE = re.compile(
    r"^(Default|Print|texture|Scheme\d*|Plain \w+|Scene .*|.*\.p2l|.*\.png|"
    r".*\.jpg|.*\.bmp|[\d\s.,;:|/\\-]*)$",
    re.I,
)


@dataclass
class Inspection:
    name: str
    size_bytes: int
    inflated_bytes: int
    payload_kinds: dict[str, int]
    preview_png: bytes | None
    material: str | None
    source_path: str | None
    features: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    standards: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    saved_mesh_vertices: int = 0
    reference_planes: int = 0
    parasolid_bytes: int = 0

    @property
    def has_saved_mesh(self) -> bool:
        return self.saved_mesh_vertices >= MIN_REAL_VERTS

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "inflated_bytes": self.inflated_bytes,
            "payload_kinds": self.payload_kinds,
            "material": self.material,
            "source_path": self.source_path,
            "features": self.features,
            "components": self.components,
            "standards": self.standards,
            "properties": self.properties,
            "has_saved_mesh": self.has_saved_mesh,
            "saved_mesh_vertices": self.saved_mesh_vertices,
            "reference_planes": self.reference_planes,
            "parasolid_bytes": self.parasolid_bytes,
            "has_preview": self.preview_png is not None,
        }


def _parse_feature_xml(text: str) -> tuple[list[str], dict[str, str]]:
    """Read the plaintext feature tree some parts carry."""
    features: list[str] = []
    props: dict[str, str] = {}
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return features, props

    for el in root.iter():
        name = el.get("Name")
        if not name:
            continue
        kind = el.get("Type") or el.tag
        dims = [d.text for d in el.findall("Dimension") if d.text]
        label = f"{name} ({kind})" if kind != name else name
        if dims:
            label += " — " + ", ".join(f"{d} mm" for d in dims)
        features.append(label)

    if root.get("Name"):
        props["Part"] = root.get("Name")
    return features, props


# Kinds the inspector reads in full. Everything else is classified from its
# probe head and counted, which turns a 26 MB part from 25 s into under a
# second.
WANTED = {"png-preview", "sw:", "xml", "parasolid"}


def inspect(path: Path) -> Inspection:
    records = extract(path, min_out=256, want=WANTED)

    kinds: Counter[str] = Counter()
    preview = None
    strings: list[str] = []
    features: list[str] = []
    props: dict[str, str] = {}
    real_verts = 0
    planes = 0

    for rec in records:
        kinds[rec.kind.split(":")[0]] += rec.size

        if rec.kind == "png-preview" and preview is None:
            preview = rec.data

        if rec.kind == "xml-feature-tree":
            f, p = _parse_feature_xml(rec.data.decode("utf-8", "replace"))
            features.extend(f)
            props.update(p)

        if rec.kind.startswith("sw:"):
            for m in _UTF16.finditer(rec.data):
                s = m.group().decode("utf-16-le", "ignore").strip()
                if 3 <= len(s) <= 120:
                    strings.append(s)

        for a in decode(rec.data)["arrays"]:
            if a.kind != "position":
                continue
            lo, hi = a.bbox()
            extents = [(hi[i] - lo[i]) * 1000.0 for i in range(3)]
            if a.count == PLANE_VERTS and any(e < FLAT_MM for e in extents):
                planes += 1
            else:
                real_verts += a.count

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
        if not _NOISE.match(s) and " " not in s[:1]:
            # Candidate feature or body name; the tree XML wins when present.
            if not features and re.match(r"^[A-Za-z][\w \-]{2,39}$", s):
                components.append(s) if "." in s else features.append(s)

    return Inspection(
        name=path.stem,
        size_bytes=path.stat().st_size,
        inflated_bytes=sum(r.size for r in records if r.parent is None),
        payload_kinds=dict(kinds),
        preview_png=preview,
        material=material,
        source_path=source_path,
        features=features[:80],
        components=components[:60],
        standards=standards[:40],
        properties=props,
        saved_mesh_vertices=real_verts,
        reference_planes=planes,
        parasolid_bytes=sum(v for k, v in kinds.items() if k.startswith("parasolid")),
    )
