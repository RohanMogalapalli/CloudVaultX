"""File chunking, streaming and checksum helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import BinaryIO

DEFAULT_CHUNK_SIZE = 256 * 1024


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 digest of a byte string."""
    return hashlib.sha256(data).hexdigest()


def iter_chunk_paths(fp: BinaryIO, chunk_size: int) -> Iterator[tuple[int, bytes]]:
    """Yield ``(index, plaintext_bytes)`` pairs from an open binary stream."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    index = 0
    while True:
        block = fp.read(chunk_size)
        if not block:
            break
        yield index, block
        index += 1


def split_bytes(data: bytes, chunk_size: int) -> list[bytes]:
    """Split an in-memory buffer into fixed-size chunks."""
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]


def join_chunks(chunks: list[bytes]) -> bytes:
    """Reassemble chunks into the original buffer."""
    return b"".join(chunks)


def expected_chunk_count(size_bytes: int, chunk_size: int) -> int:
    """Number of chunks a file of ``size_bytes`` will produce (min 1)."""
    if size_bytes <= 0:
        return 1
    return (size_bytes + chunk_size - 1) // chunk_size
