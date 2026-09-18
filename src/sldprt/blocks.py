"""Named block directory of a modern .SLDPRT.

The container is a sequence of blocks, each introduced by the six-byte
signature `14 00 06 00 08 00` and carrying its own header: a checksum, the
compressed and expanded sizes, and a name.

The names are **nibble-swapped** — every byte has its high and low halves
exchanged — which is why a plain string search over the file finds nothing
and an earlier pass here resorted to brute-forcing deflate offsets. Undo the
swap and the directory reads plainly: `Contents/DisplayLists`,
`Contents/Config-0-Partition`, `PreviewPNG`, `docProps/...`.

Container layout is from the cadmpeg project's published specification,
`docs/formats/sldprt.md` (CC BY 4.0):
https://github.com/cadmpeg/cadmpeg/blob/main/docs/formats/sldprt.md

  signature[6]  14 00 06 00 08 00
  +6   type_id      u32 LE
  +10  crc32        u32 LE   CRC-32 of the DECOMPRESSED payload
  +14  comp_sz      u32 LE
  +18  uncomp_sz    u32 LE
  +22  pre_sz       u32 LE   preamble, holding the nibble-swapped name
  +26  preamble, then the compressed payload

The six-descriptor strip tables in `savedmesh.py` are not in that document;
they were worked out by a colleague of RTC's, credited there.

Read-only.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

SIGNATURE = bytes([20, 0, 6, 0, 8, 0])

# The header runs from the signature to the preamble: see the layout above.
HEADER = 26

MAX_NAME = 1024
_PRINTABLE = re.compile(rb"[\x20-\x7e]+")


@dataclass
class Block:
    name: str
    offset: int
    data_offset: int
    compressed: int
    expanded: int
    type_id: int = 0
    crc: int = 0
    _source: bytes = field(repr=False, default=b"")

    def raw(self) -> bytes:
        return self._source[self.data_offset : self.data_offset + self.compressed]

    def inflate(self) -> bytes:
        """Expand the block, or return it as-is when it is stored."""
        raw = self.raw()
        for wbits in (-15, 15):
            try:
                out = zlib.decompressobj(wbits).decompress(raw)
            except zlib.error:
                continue
            if out:
                return out
        return raw

    def verify(self) -> bool:
        """Check the payload against the CRC-32 in its own header.

        The header carries a CRC-32 of the *decompressed* payload, so this
        proves the block boundary, the payload offset and the decompression
        were all correct. Without it a reader can be subtly wrong — off by a
        field, or inflating from the wrong offset — and still look plausible,
        which is how this file format was misread here for two days.
        """
        data = self.inflate()
        return len(data) == self.expanded and zlib.crc32(data) & 0xFFFFFFFF == self.crc


def _unswap(raw: bytes) -> bytes:
    """Undo the nibble swap applied to block names."""
    return bytes(((b << 4) | (b >> 4)) & 0xFF for b in raw)


def read_blocks(data: bytes) -> list[Block]:
    """Every named block in the container, in file order."""
    out: list[Block] = []
    pos = 8

    while True:
        at = data.find(SIGNATURE, pos)
        if at < 0:
            break
        pos = at + 1
        if at + HEADER > len(data):
            break

        # Layout from the cadmpeg spec: type_id at +6, CRC-32 of the
        # decompressed payload at +10, then compressed size, expanded size,
        # and the preamble length that the nibble-swapped name sits in.
        type_id = struct.unpack_from("<I", data, at + 6)[0]
        checksum, compressed, expanded, name_len = struct.unpack_from("<4I", data, at + 10)

        # Not a data block: a cache-cell section-index grid, which reuses the
        # marker and encodes one logical value L across the three size fields
        # as 2L, L//2 and L. Named in the spec; previously skipped here by the
        # same identity without knowing what it was.
        if checksum == expanded * 2 and compressed == expanded // 2:
            continue
        if not compressed or not 1 <= name_len <= MAX_NAME:
            continue
        if at + HEADER + name_len + compressed > len(data):
            continue

        name_bytes = _unswap(data[at + HEADER : at + HEADER + name_len])
        if not _PRINTABLE.fullmatch(name_bytes):
            continue

        out.append(
            Block(
                name=name_bytes.decode("ascii"),
                offset=at,
                data_offset=at + HEADER + name_len,
                compressed=compressed,
                expanded=expanded,
                type_id=type_id,
                crc=checksum,
                _source=data,
            )
        )

    return out


def read_file(path: Path) -> list[Block]:
    return read_blocks(path.read_bytes())


def find_block(blocks: list[Block], name: str) -> Block | None:
    for b in blocks:
        if b.name == name:
            return b
    return None
