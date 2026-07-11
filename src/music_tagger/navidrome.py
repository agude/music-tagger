"""Navidrome / Subsonic API client."""

from __future__ import annotations

import hashlib
import os
import secrets

import httpx

SUBSONIC_API_VERSION = "1.16.1"
CLIENT_NAME = "music-tagger"


def _get_config() -> tuple[str, str, str]:
    url = os.environ.get("NAVIDROME_URL", "")
    user = os.environ.get("NAVIDROME_USER", "")
    password = os.environ.get("NAVIDROME_PASSWORD", "")
    if not url or not user or not password:
        raise RuntimeError(
            "Set NAVIDROME_URL, NAVIDROME_USER, and NAVIDROME_PASSWORD environment variables."
        )
    return url, user, password


def _auth_params(user: str, password: str) -> dict[str, str]:
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return {
        "u": user,
        "t": token,
        "s": salt,
        "v": SUBSONIC_API_VERSION,
        "c": CLIENT_NAME,
        "f": "json",
    }


class NavidromeClient:
    def __init__(
        self,
        url: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        if url and user and password:
            self._url = url.rstrip("/")
            self._user = user
            self._password = password
        else:
            self._url, self._user, self._password = _get_config()
        self._client = httpx.Client(timeout=30.0)

    def start_scan(self) -> dict:
        params = _auth_params(self._user, self._password)
        resp = self._client.get(
            f"{self._url}/rest/startScan",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        subsonic = data.get("subsonic-response", {})
        if subsonic.get("status") != "ok":
            error = subsonic.get("error", {})
            raise RuntimeError(
                f"Subsonic error {error.get('code', '?')}: {error.get('message', 'unknown')}"
            )
        return subsonic.get("scanStatus", {})

    def close(self) -> None:
        self._client.close()
