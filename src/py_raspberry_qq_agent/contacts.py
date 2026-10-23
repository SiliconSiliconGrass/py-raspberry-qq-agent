from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .storage import atomic_write_json, read_json

ContactKind = Literal["user", "group"]


class ContactVault:
    """Stable encrypted aliases for QQ users and groups.

    The agent only receives aliases such as ``qq-user:<token>``. The mapping to the
    encrypted backend ID is persisted outside the agent's writable knowledge tools.
    """

    def __init__(self, path: Path, secret: str) -> None:
        if not secret.strip():
            raise ValueError("contact encryption key cannot be empty")
        self._path = path
        master = hashlib.sha256(secret.encode("utf-8")).digest()
        self._alias_key = hmac.new(master, b"contact-alias", hashlib.sha256).digest()
        encryption_key = hmac.new(master, b"contact-encryption", hashlib.sha256).digest()
        self._aes = AESGCM(encryption_key)
        raw = read_json(path, {"format": 1, "contacts": {}})
        if not isinstance(raw, dict) or not isinstance(raw.get("contacts", {}), dict):
            raise ValueError(f"invalid contact vault: {path}")  # noqa: TRY004
        self._contacts: dict[str, dict[str, str]] = raw.get("contacts", {})

    def _alias(self, kind: ContactKind, raw_id: str) -> str:
        digest = hmac.new(
            self._alias_key,
            f"alias:{kind}:{raw_id}".encode(),
            hashlib.sha256,
        ).digest()[:18]
        token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"qq-{kind}:{token}"

    def encrypt(self, kind: ContactKind, raw_id: str | int) -> str:
        raw = str(raw_id).strip()
        if not raw:
            raise ValueError("contact ID cannot be empty")
        alias = self._alias(kind, raw)
        if alias not in self._contacts:
            nonce = os.urandom(12)
            ciphertext = self._aes.encrypt(
                nonce, raw.encode("utf-8"), alias.encode("ascii")
            )
            encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
            self._contacts[alias] = {"kind": kind, "ciphertext": encoded}
            self._save()
        return alias

    def resolve(self, alias: str) -> tuple[ContactKind, str]:
        record = self._contacts.get(alias)
        if not record:
            raise KeyError(f"unknown encrypted contact alias: {alias}")
        blob = base64.urlsafe_b64decode(record["ciphertext"].encode("ascii"))
        plaintext = self._aes.decrypt(
            blob[:12], blob[12:], alias.encode("ascii")
        ).decode("utf-8")
        return record["kind"], plaintext

    def kind_of(self, alias: str) -> ContactKind:
        record = self._contacts.get(alias)
        if not record:
            raise KeyError(f"unknown encrypted contact alias: {alias}")
        return record["kind"]  # type: ignore[return-value]

    def _save(self) -> None:
        atomic_write_json(
            self._path,
            {"format": 1, "contacts": self._contacts},
        )


class ContactDirectory:
    """Agent-editable contact profiles, separate from immutable authorization roles."""

    def __init__(self, path: Path) -> None:
        self._path = path
        raw = read_json(path, {"format": 1, "profiles": {}})
        if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
            raise ValueError(f"invalid contact profile store: {path}")  # noqa: TRY004
        self._profiles: dict[str, dict[str, object]] = raw["profiles"]

    def get(self, encrypted_id: str) -> dict[str, object]:
        profile = self._profiles.get(encrypted_id, {})
        return {
            "encrypted_id": encrypted_id,
            "display_name": profile.get("display_name", ""),
            "notes": profile.get("notes", ""),
            "updated_at": profile.get("updated_at", ""),
        }

    def observe_nickname(self, encrypted_id: str, nickname: str) -> None:
        if not nickname.strip():
            return
        profile = self._profiles.setdefault(encrypted_id, {})
        if not profile.get("display_name"):
            profile["display_name"] = nickname.strip()[:128]
            profile["updated_at"] = datetime.now(UTC).isoformat()
            self._save()

    def update(
        self,
        encrypted_id: str,
        display_name: str | None,
        notes: str | None,
    ) -> dict[str, object]:
        profile = self._profiles.setdefault(encrypted_id, {})
        if display_name is not None:
            profile["display_name"] = display_name.strip()[:128]
        if notes is not None:
            profile["notes"] = notes.strip()[:4000]
        profile["updated_at"] = datetime.now(UTC).isoformat()
        self._save()
        return self.get(encrypted_id)

    def _save(self) -> None:
        atomic_write_json(
            self._path,
            {"format": 1, "profiles": self._profiles},
        )


class RoleStore:
    """Authorization roles. Only the operator CLI may modify this file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        raw = read_json(path, {"format": 1, "roles": {}})
        if not isinstance(raw, dict) or not isinstance(raw.get("roles"), dict):
            raise ValueError(f"invalid role store: {path}")  # noqa: TRY004
        self._roles: dict[str, dict[str, str]] = raw["roles"]

    def role_of(self, encrypted_id: str) -> str:
        record = self._roles.get(encrypted_id)
        return str(record.get("role", "user")) if record else "user"

    def is_admin(self, encrypted_id: str) -> bool:
        return self.role_of(encrypted_id) == "admin"

    def set_admin(self, encrypted_id: str, enabled: bool) -> None:
        if enabled:
            self._roles[encrypted_id] = {
                "role": "admin",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        else:
            self._roles.pop(encrypted_id, None)
        self._save()

    def list_roles(self) -> list[dict[str, str]]:
        return [
            {
                "encrypted_id": encrypted_id,
                "role": str(record.get("role", "user")),
                "updated_at": str(record.get("updated_at", "")),
            }
            for encrypted_id, record in sorted(self._roles.items())
        ]

    def _save(self) -> None:
        atomic_write_json(self._path, {"format": 1, "roles": self._roles})
