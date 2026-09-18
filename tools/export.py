"""Export a .SLDPRT or .SLDASM to STL and 3MF.

The end-to-end pipeline: named blocks -> saved display mesh -> writers, plus
the embedded PNG preview, which needs no decoding at all.

An assembly is rebuilt from its components: the meshes SolidWorks cached in
the assembly, plus the parts sitting in the same folder, which is where the
components it did not cache have to come from.

A part saved without display data has nothing to export; that is a property
of the file, not a failure here, and it says so.

Read-only with respect to the source file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.blocks import find_block, read_blocks  # noqa: E402
from sldprt.mesh import Mesh  # noqa: E402
from sldprt.readers import load_mesh  # noqa: E402
from sldprt.writers import write_3mf, write_stl  # noqa: E402

PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
COMPANION_EXT = (".sldprt", ".sldasm")


def folder_companions(path: Path) -> dict[str, bytes]:
    """The other SolidWorks files beside an assembly, keyed by file name.

    An assembly refers to its components by file name, so a sibling of the
    right name is the component. Only assemblies need this, and only for the
    components whose mesh was never cached.
    """
    if path.suffix.lower() != ".sldasm":
        return {}
    out: dict[str, bytes] = {}
    for sibling in path.parent.iterdir():
        if sibling == path or sibling.suffix.lower() not in COMPANION_EXT:
            continue
        if sibling.is_file():
            out[sibling.name] = sibling.read_bytes()
    return out


def build_mesh(path: Path) -> tuple[Mesh, bytes | None]:
    """The model's mesh, and the preview image stored with it."""
    data = path.read_bytes()
    blocks = read_blocks(data)

    preview = None
    for name in ("PreviewPNG", "Preview"):
        block = find_block(blocks, name)
        if block is not None:
            candidate = block.inflate()
            if candidate.startswith(PNG_MAGIC):
                preview = candidate
                break

    mesh = load_mesh(path.name, data, folder_companions(path))
    return mesh, preview


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--out", type=Path, default=Path("out"))
    args = parser.parse_args()

    mesh, preview = build_mesh(args.file)
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.file.stem

    report = mesh.report()
    d = report["dimensions_mm"]
    print(f"=== {args.file.name} ===")
    print(f"  vertices    {report['vertices']:,}")
    print(f"  triangles   {report['triangles']:,}")
    print(f"  dimensions  {d[0]:.2f} x {d[1]:.2f} x {d[2]:.2f} mm")
    print(f"  open edges  {report['open_edges']:,}")
    print(f"  closed mesh {'yes' if report['closed'] else 'no'}")
    if report["volume_mm3"] is not None:
        print(f"  volume      {report['volume_mm3']:,.2f} mm^3")

    if not mesh.triangles:
        print("  nothing to export")
        return 1

    stl_path = args.out / f"{stem}.stl"
    mf_path = args.out / f"{stem}.3mf"
    stl_path.write_bytes(write_stl(mesh, stem))
    mf_path.write_bytes(write_3mf(mesh, stem))
    print(f"  wrote {stl_path} ({stl_path.stat().st_size:,} B)")
    print(f"  wrote {mf_path} ({mf_path.stat().st_size:,} B)")

    if preview:
        png_path = args.out / f"{stem}.png"
        png_path.write_bytes(preview)
        print(f"  wrote {png_path} ({len(preview):,} B)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
