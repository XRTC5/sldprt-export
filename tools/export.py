"""Export a .SLDPRT to STL and 3MF.

The end-to-end pipeline: container -> payloads -> tessellation -> mesh ->
writers. Also drops the embedded PNG preview, which needs no decoding at
all.

Caveat, until the decoder catches up: the only tessellation these test files
carry is reference-plane display quads, so that is what comes out. The
pipeline is real; the geometry is thin. `tools/tess_report.py` says whether
a given file has anything better.

Read-only with respect to the source file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.container import extract  # noqa: E402
from sldprt.mesh import M_TO_MM, Mesh  # noqa: E402
from sldprt.tess import decode  # noqa: E402
from sldprt.writers import write_3mf, write_stl  # noqa: E402


def build_mesh(path: Path) -> tuple[Mesh, bytes | None]:
    mesh = Mesh()
    preview = None

    for rec in extract(path, min_out=512):
        if rec.kind == "png-preview" and preview is None:
            preview = rec.data

        for array in decode(rec.data)["arrays"]:
            if array.kind != "position":
                continue
            pts = [tuple(c * M_TO_MM for c in p) for p in array.triples()]
            if len(pts) == 4:
                mesh.add_quad_strip(pts)
            elif len(pts) == 3:
                mesh.add_triangle(*pts)
            else:
                # Unknown fan/strip layout: emit as a strip so nothing is
                # silently dropped, and let the validator flag it.
                for i in range(len(pts) - 2):
                    mesh.add_triangle(pts[i], pts[i + 1], pts[i + 2])

    return mesh.weld(), preview


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
