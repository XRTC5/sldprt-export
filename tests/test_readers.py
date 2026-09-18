"""Reader-level tests: which files we accept, and what an assembly needs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.readers import BREP_FORMATS, MESH_FORMATS


def test_brep_formats_are_refused_with_a_reason():
    """A .stp holds surfaces, not triangles; saying so beats a silent empty mesh."""
    assert ".stp" in BREP_FORMATS and ".stp" not in MESH_FORMATS
    assert ".sldasm" in MESH_FORMATS


def test_folder_companions_only_for_assemblies(tmp_path):
    """A part reads on its own; only an assembly needs its siblings."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from export import folder_companions

    (tmp_path / "bulkhead.SLDPRT").write_bytes(b"part")
    (tmp_path / "grain.SLDPRT").write_bytes(b"part")
    (tmp_path / "notes.txt").write_bytes(b"text")
    asm = tmp_path / "engine.SLDASM"
    asm.write_bytes(b"assembly")

    found = folder_companions(asm)
    assert sorted(found) == ["bulkhead.SLDPRT", "grain.SLDPRT"]
    assert folder_companions(tmp_path / "bulkhead.SLDPRT") == {}
