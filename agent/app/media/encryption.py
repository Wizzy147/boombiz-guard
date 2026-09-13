"""Incident media encrypted at rest (Phase 3 §24) — from day one.

One random 256-bit key per Guard install, stored in <data_dir>/media.key
wrapped with Windows DPAPI (the same machine-bound protection as the CCTV
credential vault). Each snapshot/clip is AES-256-GCM:

    file = b"BGM1" | 12-byte nonce | ciphertext+tag

GCM authenticates as well as encrypts, so a tampered file fails to open
rather than playing altered footage. A copied incident folder is useless on
another PC. Plaintext media never touches the disk: clips are encoded to
memory and encrypted before the first write.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..security.vault import default_cipher

MAGIC = b"BGM1"


class MediaCryptoError(RuntimeError):
    pass


class MediaCrypto:
    def __init__(self, key_path: Path, cipher=None) -> None:  # noqa: ANN001
        self.key_path = key_path
        self._cipher = cipher or default_cipher()
        self._aes = AESGCM(self._load_or_create())

    def _load_or_create(self) -> bytes:
        if self.key_path.exists():
            try:
                return self._cipher.unprotect(self.key_path.read_bytes())
            except Exception as e:
                raise MediaCryptoError("The incident media key on this computer can't be opened.") from e
        key = AESGCM.generate_key(bit_length=256)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.key_path.with_suffix(".tmp")
        tmp.write_bytes(self._cipher.protect(key))
        os.replace(tmp, self.key_path)
        return key

    def encrypt(self, data: bytes, aad: bytes = b"") -> bytes:
        nonce = os.urandom(12)
        return MAGIC + nonce + self._aes.encrypt(nonce, data, aad or None)

    def decrypt(self, blob: bytes, aad: bytes = b"") -> bytes:
        if not blob.startswith(MAGIC) or len(blob) < 4 + 12 + 16:
            raise MediaCryptoError("Not a Guard media file.")
        try:
            return self._aes.decrypt(blob[4:16], blob[16:], aad or None)
        except Exception as e:
            raise MediaCryptoError("This incident file is damaged or was changed.") from e

    def write(self, path: Path, data: bytes, aad: bytes = b"") -> int:
        """Atomic write of the encrypted bytes; returns bytes on disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = self.encrypt(data, aad)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, path)
        return len(blob)

    def read(self, path: Path, aad: bytes = b"") -> bytes:
        return self.decrypt(path.read_bytes(), aad)
