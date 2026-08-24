"""Application-layer encrypted channel for ``/websocket/{worker}/wss``.

Real TLS is always preferred. This exists for the case where the transport is
plain ``ws://`` -- typically a reverse proxy or tunnel that terminates TLS
elsewhere, or a lab network -- but the payload must still be confidential.

It uses only standard, vetted primitives:

* **HKDF-SHA256** derives a per-connection key from the server session secret,
  the caller's bearer token, the worker identity, and a fresh server salt. The
  token never travels in the clear beyond the initial authenticated frame, and
  two connections never share a key.
* **AES-256-GCM** encrypts each frame with a unique nonce and authenticates it,
  so tampering is detected rather than silently accepted.

It **fails closed**: if the cryptography library is unavailable the endpoint
refuses the connection instead of quietly downgrading to plaintext. It is not a
substitute for TLS -- it does not authenticate the server or prevent a
man-in-the-middle at connection time -- and the API says so.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

INFO = b"ws_collab/secure-channel/v1"
NONCE_BYTES = 12
KEY_BYTES = 32


class SecureChannelUnavailable(RuntimeError):
    """Raised when an encrypted channel was requested but cannot be provided."""


def available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except Exception:
        return False
    return True


def _derive(secret: str, token: str, worker_id: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    material = f"{secret}\x1f{token}\x1f{worker_id}".encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=INFO).derive(material)


class SecureChannel:
    """Encrypts and decrypts JSON frames for one connection."""

    def __init__(self, secret: str, token: str, worker_id: str):
        if not available():
            raise SecureChannelUnavailable(
                "encrypted channel requested but the cryptography library is not installed; "
                "install it or use the plain /ws endpoint behind real TLS"
            )
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self.salt = os.urandom(16)
        self._aead = AESGCM(_derive(secret, token, worker_id, self.salt))

    def handshake(self) -> dict[str, Any]:
        """Frame announcing the scheme and the salt the client must use."""

        return {
            "type": "secure_channel",
            "cipher": "AES-256-GCM",
            "kdf": "HKDF-SHA256",
            "salt": base64.b64encode(self.salt).decode("ascii"),
            "info": INFO.decode("ascii"),
            "note": (
                "Application-layer encryption. This protects payload "
                "confidentiality and integrity, but it does not authenticate the "
                "server; prefer real TLS (wss://) wherever possible."
            ),
        }

    def encrypt(self, payload: dict[str, Any]) -> str:
        nonce = os.urandom(NONCE_BYTES)
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        sealed = self._aead.encrypt(nonce, raw, None)
        return json.dumps({
            "n": base64.b64encode(nonce).decode("ascii"),
            "c": base64.b64encode(sealed).decode("ascii"),
        }, separators=(",", ":"))

    def decrypt(self, text: str) -> dict[str, Any]:
        try:
            envelope = json.loads(text)
            nonce = base64.b64decode(envelope["n"])
            sealed = base64.b64decode(envelope["c"])
        except Exception as error:
            raise ValueError(f"malformed encrypted frame: {error}") from error
        # Any tampering fails the GCM tag here rather than reaching the service.
        raw = self._aead.decrypt(nonce, sealed, None)
        return json.loads(raw.decode("utf-8"))
