"""Unit tests for the chunking helpers."""

from __future__ import annotations

import io

from app.chunking import (
    expected_chunk_count,
    iter_chunk_paths,
    join_chunks,
    sha256_hex,
    split_bytes,
)


def test_split_and_join_round_trip():
    data = bytes(range(256)) * 10
    chunks = split_bytes(data, 100)
    assert len(chunks) == 26
    assert join_chunks(chunks) == data


def test_iter_chunk_paths_streams_and_numbers_chunks():
    data = b"abcdefghij"
    stream = io.BytesIO(data)
    pairs = list(iter_chunk_paths(stream, 4))
    assert [index for index, _ in pairs] == [0, 1, 2]
    assert b"".join(block for _, block in pairs) == data


def test_iter_chunk_paths_handles_exact_multiple():
    stream = io.BytesIO(b"12345678")
    pairs = list(iter_chunk_paths(stream, 4))
    assert len(pairs) == 2
    assert pairs[1] == (1, b"5678")


def test_iter_chunk_paths_rejects_bad_chunk_size():
    try:
        list(iter_chunk_paths(io.BytesIO(b"abc"), 0))
    except ValueError as exc:
        assert "chunk_size" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_expected_chunk_count():
    assert expected_chunk_count(0, 100) == 1
    assert expected_chunk_count(100, 100) == 1
    assert expected_chunk_count(101, 100) == 2
    assert expected_chunk_count(1000, 256) == 4


def test_split_bytes_produces_at_least_one_chunk():
    assert split_bytes(b"", 16) == [b""]


def test_sha256_matches_hashlib():
    import hashlib

    payload = b"integrity matters"
    assert sha256_hex(payload) == hashlib.sha256(payload).hexdigest()
