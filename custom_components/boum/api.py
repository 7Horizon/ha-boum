"""Boum API client (no external dependencies, uses aiohttp)."""
from __future__ import annotations

import logging
from datetime import datetime

import aiohttp

from .const import API_BASE_URL, DATETIME_FORMAT

_LOGGER = logging.getLogger(__name__)


class BoumApiError(Exception):
    """General Boum API error."""


class BoumAuthError(BoumApiError):
    """Authentication failed."""


class BoumApi:
    """Async Boum REST API client."""

    def __init__(
        self, email: str, password: str, session: aiohttp.ClientSession
    ) -> None:
        self._email = email
        self._password = password
        self._session = session
        self._access_token: str | None = None
        self._refresh_token: str | None = None

    async def authenticate(self) -> None:
        """Sign in and store access/refresh tokens."""
        try:
            async with self._session.post(
                f"{API_BASE_URL}/auth/signin",
                json={"email": self._email, "password": self._password},
            ) as resp:
                if resp.status in (401, 403):
                    raise BoumAuthError("Invalid credentials")
                resp.raise_for_status()
                payload = await resp.json()
                data = payload["data"]
                self._access_token = data["accessToken"]
                self._refresh_token = data["refreshToken"]
        except aiohttp.ClientError as err:
            raise BoumApiError(f"Connection error during signin: {err}") from err

    async def _refresh_access_token(self) -> None:
        """Obtain a new access token using the stored refresh token."""
        try:
            async with self._session.post(
                f"{API_BASE_URL}/auth/token",
                json={"refreshToken": self._refresh_token},
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
                self._access_token = payload["data"]["accessToken"]
        except aiohttp.ClientError as err:
            raise BoumApiError(f"Token refresh failed: {err}") from err

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._access_token or ""}

    async def _async_reauth(self) -> None:
        """Refresh the access token, falling back to a full signin.

        The refresh token itself expires eventually; without the fallback the
        integration would stay broken until a manual reload.
        """
        try:
            await self._refresh_access_token()
        except BoumApiError:
            _LOGGER.debug("Token refresh failed; performing full signin")
            await self.authenticate()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """GET request with automatic token refresh on 401."""
        url = f"{API_BASE_URL}{path}"
        try:
            async with self._session.get(
                url, headers=self._headers, params=params
            ) as resp:
                if resp.status == 401:
                    _LOGGER.debug("Access token expired, refreshing")
                    await self._async_reauth()
                    async with self._session.get(
                        url, headers=self._headers, params=params
                    ) as retry:
                        retry.raise_for_status()
                        return await retry.json()
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise BoumApiError(f"Request to {path} failed: {err}") from err

    async def get_claimed_devices(self) -> list[dict]:
        """Return id + name for all claimed devices.

        The 'name' key is the user-defined device name from the Boum app,
        or an empty string if the API does not return one.
        """
        data = await self._get("/devices/claimed")
        return [
            {"id": d["id"], "name": d.get("name") or d.get("thingName") or ""}
            for d in data.get("data", [])
        ]

    async def get_device_state(self, device_id: str) -> dict:
        """Return the full device document (desiredState, reportedState, flags)."""
        data = await self._get(f"/devices/{device_id}")
        return data.get("data", {})

    async def get_device_log(self, device_id: str) -> list[dict]:
        """Return device log entries from the server.

        Relevant entry types: pumpStopped (contains payload.totalPumpedVolume),
        deepSleep, reset.
        """
        data = await self._get(f"/devices/{device_id}/log")
        return data.get("data", [])

    async def get_device_telemetry(
        self, device_id: str, start: datetime, end: datetime, interval: str | None = None
    ) -> dict:
        """Return telemetry for *device_id* in [start, end].

        interval: optional aggregation bucket size, e.g. "1h", "10m", "3600s".
        """
        params: dict[str, str] = {
            "timeStart": start.strftime(DATETIME_FORMAT),
            "timeEnd": end.strftime(DATETIME_FORMAT),
        }
        if interval is not None:
            params["interval"] = interval
        data = await self._get(f"/devices/{device_id}/data", params=params)
        return data.get("data", {})
