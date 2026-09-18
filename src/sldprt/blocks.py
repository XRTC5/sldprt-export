"""Named block directory of a modern .SLDPRT.

The container is a sequence of blocks, each introduced by the six-byte
signature `14 00 06 00 08 00` and carrying its own header: a checksum, the
compressed and expanded sizes, and a name.

The names are **nibble-swapped** — every byte has its high and low halves
exchanged — which is why a plain string search over the file finds nothing
and an earlier pass here resorted to brute-forcing deflate offsets. Undo the
swap and the directory reads plainly: `Contents/DisplayLists`,
`Contents/Config-0-Partition`, `PreviewPNG`, `docProps/...`.

Format facts (block header layout, the nibble swap, and the six-descriptor
mesh tables in `mesh.py`) came from a converter written by a colleague of
RTC's, which in turn cites the cadmpeg project's `sldprt.md` (CC BY 4.0):
https://github.com/cadmpeg/cadmpeg/blob/main/docs/formats/sldprt.md
This is an independent Python implementation of those facts.

Read-only.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

SIGNATURE = bytes([20, 0, 6, 0, 8, 0])

# A block header is 26 bytes from the signature: 4 unknown, checksum,
# compressed size, expanded size, name length.
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

        checksum, compressed, expanded, name_len = struct.unpack_from("<4I", data, at + 10)

        # A run of plausible-looking bytes that is really padding: the
        # converter this came from rejects it by the same identity.
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
