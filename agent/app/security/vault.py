"""CCTV credential vault — Windows DPAPI at rest.

Phase 1 §6: credentials never sit in the devices/cameras tables and never
leave this machine. Each device's username/password pair is serialised,
encrypted with DPAPI (CryptProtectData) and stored as a blob in its own
table. DPAPI ties the ciphertext to this Windows machine/user context, so a
copied database file is useless elsewhere.

Uses ctypes directly rather than pywin32: one fewer native dependency to
package with PyInstaller, and nothing here needs more than two calls.

`LOCAL_MACHINE` scope is used because the agent runs as a Windows Service
(LocalService/LocalSystem), and the setup UI talks to that same process —
there is no second user context that needs to decrypt.

Non-Windows (CI, developer Macs): falls back to an in-process key so the
test-suite runs, and says so loudly. It is NOT secure and is refused unless
GUARD_ALLOW_INSECURE_VAULT=1.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
from ctypes import wintypes
from typing import Protocol

log = logging.getLogger(__name__)

_ENTROPY = b"boombiz-guard/cctv-credentials/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04


class VaultError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _from_blob(blob: _Blob) -> bytes:
    out = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(blob.pbData)
    return out


def dpapi_protect(plain: bytes) -> bytes:
    data_in, _keep1 = _to_blob(plain)
    entropy, _keep2 = _to_blob(_ENTROPY)
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(data_in), "boombiz-guard", ctypes.byref(entropy), None, None,
        CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out),
    )
    if not ok:
        raise VaultError("Windows could not encrypt the CCTV credentials.")
    return _from_blob(out)


def dpapi_unprotect(cipher: bytes) -> bytes:
    data_in, _keep1 = _to_blob(cipher)
    entropy, _keep2 = _to_blob(_ENTROPY)
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(data_in), None, ctypes.byref(entropy), None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise VaultError("Saved CCTV credentials could not be read on this computer. Enter them again.")
    return _from_blob(out)


class _Cipher(Protocol):
    def protect(self, plain: bytes) -> bytes: ...
    def unprotect(self, cipher: bytes) -> bytes: ...


class _DpapiCipher:
    def protect(self, plain: bytes) -> bytes:
        return dpapi_protect(plain)

    def unprotect(self, cipher: bytes) -> bytes:
        return dpapi_unprotect(cipher)


class _InsecureDevCipher:
    """XOR with a per-process key. Only for non-Windows test runs."""

    def __init__(self) -> None:
        self._key = os.urandom(32)

    def _xor(self, data: bytes) -> bytes:
        return bytes(b ^ self._key[i % len(self._key)] for i, b in enumerate(data))

    protect = _xor
    unprotect = _xor


def default_cipher() -> _Cipher:
    if sys.platform == "win32":
        return _DpapiCipher()
    if os.environ.get("GUARD_ALLOW_INSECURE_VAULT") == "1":
        log.warning("Credential vault is running WITHOUT encryption (non-Windows dev mode).")
        return _InsecureDevCipher()
    raise VaultError("The credential vault needs Windows.")


class CredentialVault:
    """save / get / delete by device id. Storage is injected (see database.repo)."""

    def __init__(self, store: "CredentialStore", cipher: _Cipher | None = None) -> None:
        self._store = store
        self._cipher = cipher or default_cipher()

    def save_credentials(self, device_id: str, username: str, password: str) -> None:
        payload = json.dumps({"u": username, "p": password}).encode("utf-8")
        blob = base64.b64encode(self._cipher.protect(payload)).decode("ascii")
        self._store.put_secret(device_id, blob)

    def get_credentials(self, device_id: str) -> tuple[str, str] | None:
        blob = self._store.get_secret(device_id)
        if blob is None:
            return None
        data = json.loads(self._cipher.unprotect(base64.b64decode(blob)).decode("utf-8"))
        return data["u"], data["p"]

    def delete_credentials(self, device_id: str) -> None:
        self._store.delete_secret(device_id)


class CredentialStore(Protocol):
    def put_secret(self, device_id: str, blob: str) -> None: ...
    def get_secret(self, device_id: str) -> str | None: ...
    def delete_secret(self, device_id: str) -> None: ...
