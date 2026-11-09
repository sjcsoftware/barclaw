"""The Ed25519 device identity the gateway pairs with, and the secrets kept next to it."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from ..support import data_dir, log, read_json, write_private


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class DeviceIdentity:
    """`device_id` is the SHA-256 hex fingerprint of the raw public key, matching OpenClaw's clients."""

    device_id: str
    public_key: str  # base64 of the raw 32 bytes
    private_key: str  # base64 of the raw 32 bytes
    created_at_ms: int

    @classmethod
    def generate(cls) -> "DeviceIdentity":
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        return cls(
            device_id=hashlib.sha256(public).hexdigest(),
            public_key=base64.b64encode(public).decode(),
            private_key=base64.b64encode(private).decode(),
            created_at_ms=int(time.time() * 1000),
        )

    @property
    def public_key_b64url(self) -> str:
        return b64url(base64.b64decode(self.public_key))

    def sign(self, payload: str) -> str:
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(self.private_key))
        return b64url(key.sign(payload.encode()))


def auth_payload_v2(
    device_id: str, client_id: str, client_mode: str, role: str, scopes: list[str],
    signed_at_ms: int, token: Optional[str], nonce: str,
) -> str:
    """`v2|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce`, the string a device signs."""
    return "|".join(["v2", device_id, client_id, client_mode, role, ",".join(scopes), str(signed_at_ms), token or "", nonce])


class SecureStore:
    """File-backed secrets (mode 0600), mirroring how OpenClaw stores device identity on disk."""

    _shared: Optional["SecureStore"] = None

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.path = (directory or data_dir()) / "credentials.json"
        self._contents = read_json(self.path)
        self._contents.setdefault("deviceTokens", {})

    @classmethod
    def shared(cls) -> "SecureStore":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @property
    def identity(self) -> DeviceIdentity:
        stored = self._contents.get("identity")
        if isinstance(stored, dict):
            try:
                return DeviceIdentity(**stored)
            except TypeError:
                log.error("Discarding malformed device identity")
        fresh = DeviceIdentity.generate()
        self._contents["identity"] = asdict(fresh)
        self._save()
        return fresh

    @property
    def gateway_token(self) -> str:
        return self._contents.get("gatewayToken") or ""

    @gateway_token.setter
    def gateway_token(self, value: str) -> None:
        if value:
            self._contents["gatewayToken"] = value
        else:
            self._contents.pop("gatewayToken", None)
        self._save()

    def device_token(self, gateway_key: str) -> Optional[dict]:
        entry = self._contents["deviceTokens"].get(gateway_key)
        if isinstance(entry, dict) and entry.get("deviceId") == self.identity.device_id:
            return entry
        return None

    def set_device_token(self, gateway_key: str, entry: Optional[dict]) -> None:
        if entry is None:
            self._contents["deviceTokens"].pop(gateway_key, None)
        else:
            self._contents["deviceTokens"][gateway_key] = entry
        self._save()

    def _save(self) -> None:
        try:
            write_private(self.path, json.dumps(self._contents).encode())
        except OSError as error:
            log.error("Failed to save credentials: %s", error)
