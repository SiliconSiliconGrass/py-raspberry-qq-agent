from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx


@dataclass(slots=True)
class KeycloakConfig:
    issuer: str = ""
    admin_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    qq_attribute: str = "qq_number"


@dataclass(slots=True)
class User:
    subject: str
    name: str
    qq: str | None


class KeycloakDirectory:
    """Read-only identity lookup. QQ bindings are managed by administrators."""

    def __init__(self, config: KeycloakConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(timeout=10)
        self._token = ""
        self._expires_at = 0.0
        self._cache: dict[str, tuple[float, User | None]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        response = await self._http.post(
            self._config.issuer.rstrip("/") + "/protocol/openid-connect/token",
            auth=(self._config.client_id, self._config.client_secret),
            data={"grant_type": "client_credentials"},
        )
        response.raise_for_status()
        data = response.json()
        self._token = data["access_token"]
        ttl = float(data.get("expires_in", 60))
        self._expires_at = time.monotonic() + ttl - min(30, ttl / 2)
        return self._token

    async def user(self, subject: str) -> User | None:
        cached = self._cache.get(subject)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        url = self._config.admin_url.rstrip("/") + "/users/" + quote(subject, safe="")
        for attempt in range(2):
            token = await self._access_token()
            response = await self._http.get(
                url, headers={"Authorization": "Bearer " + token}
            )
            if response.status_code != 401 or attempt:
                break
            self._token = ""
        if response.status_code == 404:
            user = None
        else:
            response.raise_for_status()
            data = response.json()
            if not data.get("enabled", True):
                user = None
            else:
                name = " ".join(
                    part
                    for part in [data.get("firstName"), data.get("lastName")]
                    if part
                )
                qq = (data.get("attributes") or {}).get(self._config.qq_attribute, [])
                # Keycloak attributes are arrays; ambiguous bindings are not used.
                qq = str(qq[0]).strip() if isinstance(qq, list) and len(qq) == 1 else ""
                user = User(
                    subject,
                    name or data.get("username") or subject,
                    qq if qq.isascii() and qq.isdecimal() else None,
                )
        self._cache[subject] = (time.monotonic() + (300 if user else 30), user)
        return user
