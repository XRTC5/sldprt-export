"""sldprt-export web app.

Two things this does honestly:

  * `.SLDPRT` -> everything recoverable without SolidWorks: the embedded
    preview render, material, feature tree, referenced components, and a
    straight answer about whether the part carries a usable saved mesh.
  * `.stl` / `.3mf` / `.obj` -> a real 3D viewer, plus conversion between
    STL and 3MF with a manifold check.

What it deliberately does not do is pretend it can turn a B-rep part file
into 3D. Parasolid geometry is encoded against a schema that ships with the
Parasolid SDK, so `.SLDPRT` and `.STEP` are rejected for 3D with a reason
rather than a spinner that never finishes.

Nothing is stored. Uploads live in memory for the length of one request.
No network calls, no AI, no telemetry.
"""

from __future__ import annotations

import base64
import json
import struct
import sys
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.inspect import inspect as inspect_sldprt  # noqa: E402
from sldprt.mesh import Mesh  # noqa: E402
from sldprt.readers import UnsupportedFormat, load_mesh  # noqa: E402
from sldprt.writers import write_3mf, write_stl  # noqa: E402

MAX_UPLOAD = 128 * 1024 * 1024
STATIC = Path(__file__).resolve().parent / "static"

# Geometry goes to the browser as raw float32, not JSON. At this cap JSON
# would mean ~14.4M Python float objects (~460 MB) plus a ~260 MB string for
# a single request; the same data as float32 is 57 MB and needs no parsing.
MAX_VIEWER_TRIANGLES = 800_000

app = FastAPI(title="sldprt-export", docs_url=None, redoc_url=None)


def _scratch(upload: UploadFile) -> bytes:
    data = upload.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"file exceeds {MAX_UPLOAD // (1024 * 1024)} MB")
    if not data:
        raise HTTPException(400, "empty file")
    return data


def _mesh_response(mesh: Mesh, name: str) -> Response:
    """Pack a mesh as: uint32 header length, JSON header, positions, normals.

    Both float arrays are float32 and land in the browser as Float32Array
    with no parsing, which is what keeps a large part from costing hundreds
    of megabytes on both ends.
    """
    if mesh.triangle_count > MAX_VIEWER_TRIANGLES:
        raise HTTPException(
            413,
            f"{mesh.triangle_count:,} triangles exceeds the {MAX_VIEWER_TRIANGLES:,} "
            "the viewer will load",
        )

    n = mesh.triangle_count
    positions = bytearray(n * 9 * 4)
    normals = bytearray(n * 9 * 4)
    pack = struct.pack_into

    at = 0
    for tri in mesh.triangles:
        nx, ny, nz = mesh.face_normal(tri)
        for idx in tri:
            x, y, z = mesh.vertices[idx]
            pack("<3f", positions, at, x, y, z)
            pack("<3f", normals, at, nx, ny, nz)
            at += 12

    report = mesh.report()
    header = json.dumps({
        "name": name,
        "floats": n * 9,
        "report": {
            "vertices": report["vertices"],
            "triangles": report["triangles"],
            "dimensions_mm": list(report["dimensions_mm"]),
            "open_edges": report["open_edges"],
            "closed": report["closed"],
            "volume_mm3": report["volume_mm3"],
        },
    }).encode("utf-8")

    # Pad the header so the float arrays start 4-byte aligned. Float32Array
    # refuses to view a buffer at an unaligned offset, and JSON ignores
    # trailing whitespace, so spaces are a free way to reach the boundary.
    header += b" " * (-len(header) % 4)

    body = struct.pack("<I", len(header)) + header + bytes(positions) + bytes(normals)
    return Response(content=body, media_type="application/octet-stream")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


def _companions(uploads: list[UploadFile] | None) -> dict[str, bytes]:
    """Files dropped alongside an assembly, keyed by name.

    An assembly references its components by file name, so the parts have to
    arrive with it for the uncached ones to be readable at all.
    """
    out: dict[str, bytes] = {}
    for upload in uploads or []:
        name = Path(upload.filename or "").name
        if not name:
            continue
        data = upload.file.read(MAX_UPLOAD + 1)
        if 0 < len(data) <= MAX_UPLOAD:
            out[name] = data
    return out


@app.post("/api/inspect")
async def api_inspect(
    file: UploadFile = File(...),
    companion: list[UploadFile] | None = File(None),
) -> JSONResponse:
    """Everything a .SLDPRT will give up without SolidWorks."""
    data = _scratch(file)
    name = Path(file.filename or "part.SLDPRT").name

    if not name.lower().endswith((".sldprt", ".sldasm")):
        raise HTTPException(400, "expected a .SLDPRT file")

    tmp = Path(__file__).resolve().parent / f".upload-{id(data):x}.sldprt"
    try:
        tmp.write_bytes(data)
        result = inspect_sldprt(tmp, _companions(companion))
    finally:
        tmp.unlink(missing_ok=True)

    payload = result.to_dict()
    payload["filename"] = name
    # inspect() names the part after the file it read, which here is a
    # temporary upload; the user's filename is the meaningful one.
    payload["name"] = Path(name).stem
    if result.preview_png:
        payload["preview"] = (
            "data:image/png;base64," + base64.b64encode(result.preview_png).decode()
        )
    return JSONResponse(payload)


@app.post("/api/mesh")
async def api_mesh(
    file: UploadFile = File(...),
    companion: list[UploadFile] | None = File(None),
) -> Response:
    """Load a mesh file for the 3D viewer."""
    data = _scratch(file)
    name = Path(file.filename or "model").name
    try:
        mesh = load_mesh(name, data, _companions(companion))
    except UnsupportedFormat as exc:
        raise HTTPException(415, str(exc)) from exc
    return _mesh_response(mesh, name)


@app.post("/api/convert")
async def api_convert(
    file: UploadFile = File(...),
    fmt: str = Query("stl", pattern="^(stl|3mf)$"),
    companion: list[UploadFile] | None = File(None),
) -> Response:
    """Convert between mesh formats."""
    data = _scratch(file)
    name = Path(file.filename or "model").name
    try:
        mesh = load_mesh(name, data, _companions(companion))
    except UnsupportedFormat as exc:
        raise HTTPException(415, str(exc)) from exc

    stem = Path(name).stem
    if fmt == "stl":
        body, media = write_stl(mesh, stem), "model/stl"
    else:
        body, media = write_3mf(mesh, stem), "model/3mf"

    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{fmt}"'},
    )


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8500, log_level="info")
