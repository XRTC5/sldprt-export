# sldprt-export

Read SolidWorks `.SLDPRT` parts and `.SLDASM` assemblies without SolidWorks
installed, inspect them in the browser, and export STL or 3MF.

No AI, no network calls, no licensed CAD kernel. Everything runs on the
machine it is opened on.

## What it does

- **Reads the geometry.** The display mesh SolidWorks saves in the file, as
  triangle strips, decoded to triangles. An engine part comes out at 136,131
  triangles, within 0.02 mm of the same part exported through Fusion.
- **Rebuilds assemblies.** Component placements and transforms, nested
  subassemblies included. Components whose mesh was never cached in the
  assembly are read from their own part files when those are supplied.
- **Checks what it read.** Every block is verified against the CRC-32 in its
  own header. Open edges, shells, winding, duplicate faces, watertightness
  and volume are reported per model.
- **Views it.** Shaded or wireframe, a section view on any axis, an
  orientation cube, and a measuring tool that snaps to real vertices and
  edges rather than to the tessellation.
- **Exports.** Binary STL and 3MF.

## Running it

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install fastapi uvicorn python-multipart
./.venv/Scripts/python.exe app/server.py      # http://127.0.0.1:8500
```

Or from the command line:

```bash
python tools/export.py path/to/part.SLDPRT --out out/
```

For an assembly, the parts sitting in the same folder are picked up
automatically; in the browser, drop the assembly and its parts together.

## How the format works

A modern `.SLDPRT` is **not** an OLE compound file, whatever the older
write-ups say — `olefile` rejects every real file tested here. It is a flat
sequence of blocks, each introduced by the signature `14 00 06 00 08 00` and
carrying its own header:

```
+0   signature   14 00 06 00 08 00
+6   type_id     u32
+10  crc32       u32   of the DECOMPRESSED payload
+14  comp_sz     u32
+18  uncomp_sz   u32
+22  pre_sz      u32   preamble, holding the name
+26  preamble, then the raw-DEFLATE payload
```

Two details do most of the work.

**Block names are nibble-swapped** — every byte has its halves exchanged.
That is why a string search over the file finds nothing, and why brute-force
offset probing looks like the only way in. Undo the swap and there is a plain
directory: `Contents/DisplayLists`, `Contents/Config-0-Partition`,
`PreviewPNG`, `swXmlContents/...`.

**The mesh is triangle strips**, in tables of six descriptors, not the plain
vertex arrays that a sweep for coordinate-looking floats would turn up. A
strip of n vertices is n−2 triangles. Read as consecutive triples it is
noise — which is how this data got missed here for two days, and the files
wrongly called meshless.

The CRC is worth more than it looks. It covers the decompressed payload, so
one check proves the block boundary, the payload offset and the decompression
together. A reader can be subtly wrong and still look plausible; a CRC cannot
be talked round.

## What it cannot do

Components stored as imported `.stp`/`.step` are **B-rep surfaces with no
tessellation**, so there is nothing to lift. The Parasolid XT streams are
present in every file but schema-locked (`SCH_3501251`), which needs the
Parasolid SDK. Those components are named and counted, never silently
dropped.

## Credits

The container layout is from the [cadmpeg](https://github.com/cadmpeg/cadmpeg)
project's `docs/formats/sldprt.md`, CC BY 4.0.

The strip-table structure was worked out by a colleague of the author's, in a
converter of their own. This is an independent Python implementation of the
same format facts; none of their code is used here.

## Tests

```bash
python -m pytest tests/ -q     # reader, writers, container
node tests/js/run.mjs          # the viewer's snapping index
```

The container tests build a synthetic `.SLDPRT` rather than shipping
someone's CAD, which also makes every header field deliberate: a test that
only reads real files passes just as happily when the reader is subtly wrong.

The JavaScript tests cut the module under test out of `app/static/index.html`
at its own comment markers, so what they exercise is what ships.

## Licence

MIT, in `LICENSE`. That covers the code here, which is all original.

It does not cover the things credited above: the container layout is from the
cadmpeg specification under CC BY 4.0, and the strip-table structure is
someone else's finding. Facts about a file format are not copyrightable, but
the write-ups describing them are, and they are cited rather than copied.

## Legal

Reverse engineering a file format for interoperability is permitted in the UK
and EU under the Software Directive, Article 6. No SolidWorks code, headers or
licensed libraries are used — this is clean-room, from observed bytes only.
Source files are opened read-only and never written to.
