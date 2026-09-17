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
    partial: bool = False   # data is only the probe head, not the full stream

    @property
    def size(self) -> int:
        return len(self.data)

    def label(self) -> str:
        parent = "" if self.parent is None else f" (in 0x{self.parent:x})"
        mark = "+" if self.partial else " "
        return f"0x{self.offset:08x}{parent}  {self.kind:<22} {self.size:>10,} B{mark}"


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
    return _probe_head(view, offset, wbits) is not None


def _probe_head(view: memoryview, offset: int, wbits: int) -> bytes | None:
    """Inflate just the first few KB, enough to classify without committing.

    Full inflation of every candidate is what made a 26 MB part take 45 s:
    12,011 real but tiny nested streams were each decompressed in full and
    then mostly discarded. Every payload kind we care about announces itself
    in its opening bytes, so classification never needs the whole thing.
    """
    obj = zlib.decompressobj(wbits)
    try:
        out = obj.decompress(view[offset : offset + PROBE_IN], PROBE_OUT)
    except zlib.error:
        return None
    return out if len(out) >= PROBE_MIN else None


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


def iter_nested_zlib(payload: bytes, min_out: int = 256, want: set[str] | None = None):
    """Yield (offset, payload, partial) for zlib streams embedded in a payload.

    A two-byte magic match is mostly noise: 0x78 followed by one of four
    bytes occurs constantly in binary data. Inflating the whole remainder to
    test each hit is O(n^2) and costs 45 s on a 26 MB part, against 0.2 s for
    the top-level scan. Probe with a bounded window first, exactly as
    iter_deflate_blocks does.
    """
    view = memoryview(payload)
    for magic in ZLIB_MAGIC:
        start = 0
        while True:
            idx = payload.find(magic, start)
            if idx < 0:
                break
            start = idx + 1

            head = _probe_head(view, idx, ZLIB_WRAPPED)
            if head is None:
                continue

            if not _wanted(classify(head), want):
                # Classified from the head alone; skip the full inflate.
                yield idx, head, True
                continue

            result = _inflate(payload, idx, ZLIB_WRAPPED)
            if result is not None and len(result[1]) >= min_out:
                yield idx, result[1], False
                start = idx + max(result[0], 1)


def _wanted(kind: str, want: set[str] | None) -> bool:
    if want is None:
        return True
    return any(kind == w or kind.startswith(w) for w in want)


def extract(
    path: Path,
    recurse: bool = True,
    min_out: int = 256,
    want: set[str] | None = None,
) -> list[Record]:
    """Recover payloads from a .SLDPRT file.

    `want` names the payload kinds to materialise in full, by exact match or
    prefix (`"sw:"` takes every class stream). Anything else is returned with
    only its probe head and `partial=True`, which is enough to count and
    classify it. Leaving `want` as None inflates everything.
    """
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
            for inner_off, inner, partial in iter_nested_zlib(payload, min_out, want):
                records.append(
                    Record(
                        offset=inner_off,
                        kind=classify(inner),
                        data=inner,
                        parent=offset,
                        partial=partial,
                    )
                )

    return records


def coverage(path: Path, records: list[Record]) -> tuple[int, int]:
    """Bytes of the source file accounted for, and the file's total size."""
    total = path.stat().st_size
    covered = sum(r.consumed for r in records if r.parent is None)
    return covered, total
