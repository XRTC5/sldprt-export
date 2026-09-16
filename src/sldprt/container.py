"""SolidWorks .SLDPRT container reader.

Modern .SLDPRT files (SolidWorks ~2015 onward) are NOT OLE compound files,
which is what most of the public write-ups still claim. They are a flat
sequence of raw DEFLATE blocks. Inflating them yields, among other things:

  * an UnQLite embedded key/value database
  * Parasolid XT transmit files -- the actual B-rep geometry
  * SolidWorks class serialisation streams (moPart_c, moHeader_c, ...)
  * a PNG preview thumbnail
  * OPC/OOXML metadata parts and a plaintext feature-tree XML

Some payloads contain further zlib-wrapped streams, so extraction recurses.

There is no published index for this format, so blocks are found by probing
every offset. A cheap bounded probe rejects almost everything immediately;
only survivors get a full inflate. A 12 MB part scans in under a second.

Read-only. This module never writes to the source file.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

RAW_DEFLATE = -15
ZLIB_WRAPPED = 15

# Probe budget. Without a bound, slicing the remainder at every offset makes
# the scan quadratic: fine on 78 KB, hours on 33 MB.
PROBE_IN = 1 << 16
PROBE_OUT = 1 << 12
PROBE_MIN = 256

ZLIB_MAGIC = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")

PARASOLID_MAGIC = b"PS\x00\x00\x00"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UNQLITE_MAGIC = b"unqlite"

_SW_CLASS = re.compile(rb"\xff\xff\x01\x00.\x00(mo|ui|vis|gc|su)[A-Za-z_]+_c")
_XML_HEAD = re.compile(rb"^\s*<\?xml", re.DOTALL)


@dataclass
class Record:
    """One payload recovered from the container."""

    offset: int
    kind: str
    data: bytes = field(repr=False)
    parent: int | None = None
    consumed: int = 0

    @property
    def size(self) -> int:
        return len(self.data)

    def label(self) -> str:
        parent = "" if self.parent is None else f" (in 0x{self.parent:x})"
        return f"0x{self.offset:08x}{parent}  {self.kind:<22} {self.size:>10,} B"


def classify(payload: bytes) -> str:
    """Name a payload from its leading bytes and contents."""
    if payload.startswith(PNG_MAGIC):
        return "png-preview"
    if payload.startswith(PARASOLID_MAGIC):
        # The transmit header says which flavour: partition, deltas, or a
        # full model. It sits in the first line as readable ASCII.
        head = payload[:96]
        for flavour in (b"partition", b"deltas", b"schema"):
            if flavour in head:
                return f"parasolid-{flavour.decode()}"
        return "parasolid"
    if payload.startswith(UNQLITE_MAGIC):
        return "unqlite-db"
    if _XML_HEAD.match(payload[:64]):
        if b"<Keywords" in payload[:4096]:
            return "xml-feature-tree"
        if b"content-types" in payload[:512].lower():
            return "xml-content-types"
        return "xml-metadata"
    if _SW_CLASS.search(payload[:4096]):
        match = _SW_CLASS.search(payload[:4096])
        name = match.group(0)[6:].decode("ascii", "ignore")
        return f"sw:{name}"
    return "unknown"


def _probe(view: memoryview, offset: int, wbits: int) -> bool:
    obj = zlib.decompressobj(wbits)
    try:
        out = obj.decompress(view[offset : offset + PROBE_IN], PROBE_OUT)
    except zlib.error:
        return False
    return len(out) >= PROBE_MIN


def _inflate(data: bytes, offset: int, wbits: int) -> tuple[int, bytes] | None:
    obj = zlib.decompressobj(wbits)
    try:
        out = obj.decompress(memoryview(data)[offset:])
        out += obj.flush()
    except zlib.error:
        return None
    if not out:
        return None
    consumed = (len(data) - offset) - len(obj.unused_data)
    return consumed, out


def iter_deflate_blocks(data: bytes, min_out: int = 256):
    """Yield (offset, consumed, payload) for each raw DEFLATE block."""
    view = memoryview(data)
    offset = 0
    end = len(data)

    while offset < end:
        if _probe(view, offset, RAW_DEFLATE):
            result = _inflate(data, offset, RAW_DEFLATE)
            if result is not None and len(result[1]) >= min_out:
                consumed, payload = result
                yield offset, consumed, payload
                # Overlapping hits inside a stream we already decoded are
                # noise, so skip past what it consumed.
                offset += max(consumed, 1)
                continue
        offset += 1


def iter_nested_zlib(payload: bytes, min_out: int = 256):
    """Yield (offset, payload) for zlib streams embedded in a payload."""
    for magic in ZLIB_MAGIC:
        start = 0
        while True:
            idx = payload.find(magic, start)
            if idx < 0:
                break
            start = idx + 1
            result = _inflate(payload, idx, ZLIB_WRAPPED)
            if result is not None and len(result[1]) >= min_out:
                yield idx, result[1]


def extract(path: Path, recurse: bool = True, min_out: int = 256) -> list[Record]:
    """Recover every payload from a .SLDPRT file."""
    data = path.read_bytes()
    records: list[Record] = []

    for offset, consumed, payload in iter_deflate_blocks(data, min_out):
        records.append(
            Record(
                offset=offset,
                kind=classify(payload),
                data=payload,
                consumed=consumed,
            )
        )
        if recurse:
            for inner_off, inner in iter_nested_zlib(payload, min_out):
                records.append(
                    Record(
                        offset=inner_off,
                        kind=classify(inner),
                        data=inner,
                        parent=offset,
                    )
                )

    return records


def coverage(path: Path, records: list[Record]) -> tuple[int, int]:
    """Bytes of the source file accounted for, and the file's total size."""
    total = path.stat().st_size
    covered = sum(r.consumed for r in records if r.parent is None)
    return covered, total
