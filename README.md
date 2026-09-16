# sldprt-export

Convert SolidWorks `.SLDPRT` parts to STL and 3MF without SolidWorks
installed. Zero AI, no network calls, no licensed CAD kernel.

**Status: phase 2 of 6.** The container is fully decoded. The mesh decoder
is not written yet. Nothing exports yet.

## What we found

The public write-ups say `.SLDPRT` is an OLE compound file. **For modern
SolidWorks that is wrong** — `olefile` rejects all five test files. The real
layout is a flat sequence of **raw DEFLATE blocks** with no magic bytes and
no index, so blocks are found by probing every offset.

Inflating them recovers:

| Payload | What it is |
|---|---|
| `parasolid` / `-partition` / `-deltas` | Parasolid XT transmit files — the B-rep geometry. Schema `SCH_3501251` |
| `unqlite-db` | UnQLite embedded key/value database |
| `sw:*_c` streams | MFC `CArchive` serialisation of the feature tree |
| `png-preview` | The thumbnail SolidWorks and Explorer display |
| `xml-feature-tree` | Plaintext feature tree: every feature, sketch and dimension |
| `xml-metadata` | OPC/OOXML custom and config properties |

Some payloads hold further zlib-wrapped streams, so extraction recurses.

### Tessellation — the test files have none

`uoTempBodyTessData_c` / `uoTempFaceTessData_c` are decoded (`src/sldprt/tess.py`).
The record is a four-word header — `float_count | tag 100 | stride | vertex_count`,
where `float_count == vertex_count * 3` — followed by float32 positions in
metres, then a matching normals array. MFC `CArchive` writes these unaligned,
so they must be found by searching the tag word, not by stepping 4 bytes.

But what it decodes are **reference planes, not parts**. MLG's quads measure
exactly 200 x 100 x 200 mm and Axle's exactly 1000 x 1000 x 500 mm — round
numbers with one flat axis. Display helpers.

`tools/tess_report.py` accounts for every inflated byte by kind. On all five
parts the total is fully explained by B-rep, preview, feature tree and
padding, with nothing left over. For scale, a 1,392-triangle part needs
~50 KB of positions alone and ~100 KB with normals; MLG's entire 78 KB file
inflates to 249 KB of which 23 KB is Parasolid and 20 KB is the PNG.

**These parts were saved without display data.** Lifting a saved mesh cannot
work on them — not a decoder bug, an absent input. Confirming that the
approach works needs files that have one.

The B-rep is unaffected: Parasolid XT is present in every file and has a
published Siemens spec.

## Verified results

Five parts, 78 KB to 33 MB:

| File | Size | Payloads | Coverage |
|---|---|---|---|
| MLG MOUNTING AXLE | 78 KB | 24 | 86.6% |
| Axle Mounting System | 80 KB | 24 | 86.8% |
| Main Wheels | 143 KB | 23 | 92.3% |
| engine_chamber_nosplit | 12 MB | 3,471 | 99.9% |
| engine_chamber_split2 | 33 MB | 10,000 | 100.0% |

All four small-to-large files scan in 9.6 s total. A naive scan is O(n²) and
would take hours on the 33 MB part; a bounded probe rejects non-blocks
before any full inflate.

## Usage

```bash
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install olefile
./.venv/Scripts/python.exe tools/inventory.py path/to/part.SLDPRT --quiet
```

`--out DIR` writes every payload to disk for inspection.

## Layout

```
src/sldprt/container.py   the reader: block scan, recursion, classification
tools/inventory.py        what is inside a file, by payload kind
tools/hunt_mesh.py        sweep payloads for coordinate arrays
tools/scan_deflate.py     raw DEFLATE brute-force scan
tools/scan_raw.py         identify an unknown container
tools/inflate_at.py       inflate and characterise one offset
tools/dump_container.py   OLE reader, kept as the record of a dead end
```

All tools are read-only and never write to the source file.

## Roadmap

1. ~~Container decode~~ — done
2. Mesh decode — **current**, the project's real risk
3. STL writer (binary, 84-byte header + 50 B/triangle)
4. 3MF writer (ZIP: `3D/3dmodel.model`, `[Content_Types].xml`, `_rels/.rels`)
5. Validation: manifold check, open-edge count, bbox against stored dimensions
6. Web UI on `:8500` — drop zone, three.js viewer, ZIP download

## Legal

Reverse engineering a file format for interoperability is permitted in the
UK and EU under the Software Directive, Article 6. No SolidWorks code,
headers or licensed libraries are used. Clean-room, from observed bytes only.
