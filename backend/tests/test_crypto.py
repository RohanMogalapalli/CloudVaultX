"""Unit tests for the AES-256-GCM encryption layer."""

from __future__ import annotations

import pytest

from app import crypto


def test_encrypt_decrypt_round_trip():
    master = bytes(range(32))
    salt = crypto.generate_salt()
    key = crypto.derive_file_key(master, salt)
    aad = crypto.build_aad(7, 3)

    plaintext = b"decentralised storage payload" * 100
    blob = crypto.encrypt_chunk(key, plaintext, aad)

    assert blob != plaintext
    assert len(blob) == len(plaintext) + crypto.NONCE_SIZE + 16  # nonce + GCM tag
    assert crypto.decrypt_chunk(key, blob, aad) == plaintext


def test_nonce_is_unique_per_chunk():
    key = crypto.derive_file_key(b"k" * 32, crypto.generate_salt())
    aad = crypto.build_aad(1, 0)
    first = crypto.encrypt_chunk(key, b"same plaintext", aad)
    second = crypto.encrypt_chunk(key, b"same plaintext", aad)
    assert first != second
    assert first[: crypto.NONCE_SIZE] != second[: crypto.NONCE_SIZE]


def test_different_salts_produce_different_keys():
    master = bytes(range(32))
    key_one = crypto.derive_file_key(master, crypto.generate_salt())
    key_two = crypto.derive_file_key(master, crypto.generate_salt())
    assert key_one != key_two
    assert len(key_one) == 32


def test_tampered_ciphertext_is_rejected():
    key = crypto.derive_file_key(b"m" * 32, crypto.generate_salt())
    aad = crypto.build_aad(2, 0)
    blob = bytearray(crypto.encrypt_chunk(key, b"important data", aad))

    blob[-1] ^= 0x01  # flip a bit in the authentication tag

    with pytest.raises(Exception):
        crypto.decrypt_chunk(key, bytes(blob), aad)


def test_wrong_associated_data_is_rejected():
    key = crypto.derive_file_key(b"w" * 32, crypto.generate_salt())
    blob = crypto.encrypt_chunk(key, b"bound to position", crypto.build_aad(5, 0))

    with pytest.raises(Exception):
        crypto.decrypt_chunk(key, blob, crypto.build_aad(5, 1))


def test_salt_encoding_round_trip():
    salt = crypto.generate_salt()
    assert crypto.salt_from_b64(crypto.salt_to_b64(salt)) == salt


def test_decrypt_rejects_truncated_input():
    key = crypto.derive_file_key(b"t" * 32, crypto.generate_salt())
    with pytest.raises(ValueError):
        crypto.decrypt_chunk(key, b"short", crypto.build_aad(1, 1))
