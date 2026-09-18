"""Write a real model's triangle positions as raw float32, for the JS tests.

The cube in `cube.js` proves the snapping index is correct. It cannot prove it
is fast enough, or that it behaves on real tessellation, so `real.js` runs
against an actual part. Source CAD stays out of the repo, so the fixture is
generated locally and gitignored.

    python tests/js/dump_fixture.py macaw "C:/.../macaw_II.SLDASM"
    node tests/js/run.mjs macaw
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sldprt.readers import load_mesh  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COMPANION_EXT = (".sldprt", ".sldasm")


def companions(path: Path) -> dict[str, bytes]:
    """An assembly needs the parts beside it; a part needs nothing."""
    if path.suffix.lower() != ".sldasm":
        return {}
    return {
        f.name: f.read_bytes()
        for f in path.parent.iterdir()
        if f.is_file() and f.suffix.lower() in COMPANION_EXT and f != path
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    label, source = sys.argv[1], Path(sys.argv[2])
    mesh = load_mesh(source.name, source.read_bytes(), companions(source))
    if not mesh.triangles:
        print(f"{source.name} has no mesh to write")
        return 1

    flat: list[float] = []
    for tri in mesh.triangles:
        for index in tri:
            flat.extend(mesh.vertices[index])

    FIXTURES.mkdir(exist_ok=True)
    out = FIXTURES / f"{label}.f32"
    out.write_bytes(struct.pack(f"<{len(flat)}f", *flat))
    print(f"{label}: {len(mesh.triangles):,} triangles -> {out} ({out.stat().st_size:,} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
