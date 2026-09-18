"""Container reader tests, against a synthetic file we build ourselves.

Building the container rather than shipping a real part keeps someone's CAD
out of the repo and, more usefully, makes every field deliberate: a test that
only reads real files passes just as happily when the reader is subtly wrong,
which is exactly what happened here.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.blocks import SIGNATURE, find_block, read_blocks

PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def nibble_swap(raw: bytes) -> bytes:
    return bytes(((b << 4) | (b >> 4)) & 0xFF for b in raw)


def make_block(name: str, payload: bytes, type_id: int = 1) -> bytes:
    """One block, laid out the way the format specifies."""
    compressed = zlib.compress(payload)[2:-4]  # raw deflate
    encoded = nibble_swap(name.encode("ascii"))
    return (
        SIGNATURE
        + struct.pack("<I", type_id)
        + struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF)
        + struct.pack("<I", len(compressed))
        + struct.pack("<I", len(payload))
        + struct.pack("<I", len(encoded))
        + encoded
        + compressed
    )


def make_cache_cell(logical: int) -> bytes:
    """A section-index grid: reuses the marker, is not a data block."""
    name = nibble_swap(b"CacheCell")
    return (
        SIGNATURE
        + struct.pack("<I", 0)
        + struct.pack("<I", logical * 2)
        + struct.pack("<I", logical // 2)
        + struct.pack("<I", logical)
        + struct.pack("<I", len(name))
        + name
    )


def make_file(*blocks: bytes) -> bytes:
    # Eight bytes of container preamble before the first block.
    return b"\x00" * 8 + b"".join(blocks)


def test_reads_names_through_the_nibble_swap():
    data = make_file(
        make_block("Contents/DisplayLists", b"mesh payload" * 40),
        make_block("PreviewPNG", PNG_MAGIC + b"body" * 60),
    )
    names = [b.name for b in read_blocks(data)]
    assert names == ["Contents/DisplayLists", "PreviewPNG"]


def test_payload_round_trips():
    payload = b"the quick brown fox " * 50
    blocks = read_blocks(make_file(make_block("Contents/Config-0", payload)))
    assert blocks[0].inflate() == payload
    assert blocks[0].expanded == len(payload)


def test_crc_confirms_a_correct_read():
    payload = b"tessellation" * 100
    block = read_blocks(make_file(make_block("Contents/DisplayLists", payload)))[0]
    assert block.verify()


def test_crc_catches_a_corrupted_payload():
    """The check has to fail when the bytes are wrong, or it proves nothing."""
    payload = b"tessellation" * 100
    data = bytearray(make_file(make_block("Contents/DisplayLists", payload)))
    # Flip a bit deep inside the compressed payload.
    data[-3] ^= 0x01
    block = read_blocks(bytes(data))[0]
    assert not block.verify()


def test_cache_cells_are_not_blocks():
    data = make_file(
        make_cache_cell(2048),
        make_block("PreviewPNG", PNG_MAGIC + b"x" * 200),
    )
    assert [b.name for b in read_blocks(data)] == ["PreviewPNG"]


def test_find_block_by_name():
    data = make_file(
        make_block("Contents/DisplayLists", b"a" * 100),
        make_block("swXmlContents/KeyWords", b"<?xml version='1.0'?><r/>"),
    )
    blocks = read_blocks(data)
    assert find_block(blocks, "swXmlContents/KeyWords") is not None
    assert find_block(blocks, "Contents/Nope") is None


def test_multiple_blocks_keep_their_own_payloads():
    first, second = b"first" * 80, b"second" * 80
    blocks = read_blocks(
        make_file(make_block("Contents/A", first), make_block("Contents/B", second))
    )
    assert [b.inflate() for b in blocks] == [first, second]
    assert all(b.verify() for b in blocks)
