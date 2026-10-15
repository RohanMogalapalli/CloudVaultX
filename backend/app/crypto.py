"""AES-256-GCM chunk encryption with per-file keys derived via HKDF."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_SIZE = 12
SALT_SIZE = 16
KEY_SIZE = 32
_HKDF_INFO = b"CloudVaultX:file-key:v1"


def generate_salt() -> bytes:
    """Random per-file HKDF salt."""
    return os.urandom(SALT_SIZE)


def salt_to_b64(salt: bytes) -> str:
    return base64.b64encode(salt).decode("ascii")


def salt_from_b64(encoded: str) -> bytes:
    return base64.b64decode(encoded.encode("ascii"))


def derive_file_key(master_key: bytes, salt: bytes) -> bytes:
    """Derive a unique 256-bit file key from the master key + per-file salt."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        info=_HKDF_INFO,
    ).derive(master_key)


def build_aad(file_id: int, chunk_index: int) -> bytes:
    """Associated data binding a ciphertext to its exact file/chunk position."""
    return f"CloudVaultX:{file_id}:{chunk_index}".encode("utf-8")


def encrypt_chunk(file_key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Encrypt one chunk. Returns nonce || ciphertext || tag."""
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(file_key).encrypt(nonce, plaintext, aad)
    return nonce + ciphertext


def decrypt_chunk(file_key: bytes, blob: bytes, aad: bytes) -> bytes:
    """Decrypt one chunk produced by :func:`encrypt_chunk`."""
    if len(blob) <= NONCE_SIZE:
        raise ValueError("Encrypted chunk is too short to be valid")
    nonce, ciphertext = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
    return AESGCM(file_key).decrypt(nonce, ciphertext, aad)
