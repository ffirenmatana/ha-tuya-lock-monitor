"""Tuya OpenAPI coordinator — handles authentication and polling."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class TuyaLockCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls the Tuya OpenAPI for device status."""

    def __init__(
        self,
        hass: HomeAssistant,
        access_id: str,
        access_secret: str,
        device_id: str,
        endpoint: str,
    ) -> None:
        self._access_id = access_id
        self._access_secret = access_secret
        self._device_id = device_id
        self._endpoint = endpoint.rstrip("/")
        self._token: str | None = None
        self._token_expire: float = 0.0

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    # ------------------------------------------------------------------
    # Signing helpers
    # ------------------------------------------------------------------

    def _sign(
        self,
        ts: str,
        nonce: str,
        method: str,
        path: str,
        token: str = "",
        body: str = "",
    ) -> str:
        """Return an HMAC-SHA256 signature for a Tuya OpenAPI request."""
        content_sha256 = hashlib.sha256(body.encode()).hexdigest()
        str_to_sign = f"{method}\n{content_sha256}\n\n{path}"
        message = self._access_id + token + ts + nonce + str_to_sign
        _LOGGER.debug(
            "[TuyaSign] method=%s path=%s ts=%s nonce=%s token_present=%s",
            method, path, ts, nonce, bool(token),
        )
        _LOGGER.debug("[TuyaSign] str_to_sign=%r", str_to_sign)
        _LOGGER.debug("[TuyaSign] full message (no secret shown) length=%d", len(message))
        signature = hmac.new(
            self._access_secret.encode(),
            message.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest().upper()
        _LOGGER.debug("[TuyaSign] signature=%s", signature)
        return signature

    def _base_headers(self, ts: str, nonce: str, sign: str, token: str = "") -> dict:
        headers = {
            "client_id": self._access_id,
            "sign": sign,
            "sign_method": "HMAC-SHA256",
            "t": ts,
            "nonce": nonce,
        }
        if token:
            headers["access_token"] = token
        return headers

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _fetch_token(self, session: aiohttp.ClientSession) -> str:
        """Obtain a fresh access token from the Tuya API."""
        # Tuya signing spec: path for token endpoint must NOT include query string
        sign_path = "/v1.0/token"
        query = "grant_type=1"
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = self._sign(ts, nonce, "GET", f"{sign_path}?{query}")
        headers = self._base_headers(ts, nonce, sign)

        url = f"{self._endpoint}{sign_path}?{query}"
        _LOGGER.debug("[TuyaToken] Requesting token from %s", url)
        _LOGGER.debug("[TuyaToken] Headers (no secret): client_id=%s t=%s nonce=%s", self._access_id, ts, nonce)

        async with session.get(url, headers=headers) as resp:
            status = resp.status
            raw = await resp.text()
            _LOGGER.debug("[TuyaToken] HTTP status=%d response=%s", status, raw)
            if status >= 400:
                raise UpdateFailed(
                    f"Tuya token HTTP {status}: {raw}"
                )
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise UpdateFailed(f"Tuya non-JSON response: {raw[:200]}") from exc

        if not data.get("success"):
            code = data.get("code")
            msg = data.get("msg", "unknown")
            _LOGGER.error(
                "[TuyaToken] Auth failed — code=%s msg=%s | "
                "Check: Access ID=%s, Endpoint=%s, system clock sync",
                code, msg, self._access_id, self._endpoint,
            )
            raise UpdateFailed(
                f"Tuya token error code={code}: {msg}"
            )

        result = data["result"]
        self._token = result["access_token"]
        # expire 60 s early for safety
        self._token_expire = time.time() + result.get("expire_time", 7200) - 60
        _LOGGER.debug(
            "[TuyaToken] Token obtained successfully, expires in %s s",
            result.get("expire_time"),
        )
        return self._token

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        if self._token is None or time.time() >= self._token_expire:
            await self._fetch_token(session)
        return self._token  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    async def _get_device_info(
        self, session: aiohttp.ClientSession, token: str
    ) -> dict:
        path = f"/v1.0/devices/{self._device_id}"
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = self._sign(ts, nonce, "GET", path, token)
        headers = self._base_headers(ts, nonce, sign, token)
        url = self._endpoint + path
        _LOGGER.debug("[TuyaDevice] GET %s", url)

        async with session.get(url, headers=headers) as resp:
            raw = await resp.text()
            _LOGGER.debug("[TuyaDevice] status=%d response=%s", resp.status, raw)
            data = json.loads(raw)

        if not data.get("success"):
            _LOGGER.error("[TuyaDevice] info failed code=%s msg=%s", data.get("code"), data.get("msg"))
            raise UpdateFailed(
                f"Tuya device info error {data.get('code')}: {data.get('msg')}"
            )
        return data["result"]

    async def _get_device_status(
        self, session: aiohttp.ClientSession, token: str
    ) -> list[dict]:
        path = f"/v1.0/devices/{self._device_id}/status"
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = self._sign(ts, nonce, "GET", path, token)
        headers = self._base_headers(ts, nonce, sign, token)
        url = self._endpoint + path
        _LOGGER.debug("[TuyaStatus] GET %s", url)

        async with session.get(url, headers=headers) as resp:
            raw = await resp.text()
            _LOGGER.debug("[TuyaStatus] status=%d response=%s", resp.status, raw)
            data = json.loads(raw)

        if not data.get("success"):
            _LOGGER.error("[TuyaStatus] failed code=%s msg=%s", data.get("code"), data.get("msg"))
            raise UpdateFailed(
                f"Tuya device status error {data.get('code')}: {data.get('msg')}"
            )
        return data["result"]  # list of {code, value}

    # ------------------------------------------------------------------
    # DataUpdateCoordinator
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with aiohttp.ClientSession() as session:
                token = await self._get_token(session)
                info = await self._get_device_info(session, token)
                status_list = await self._get_device_status(session, token)
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Network error: {err}") from err

        status: dict[str, Any] = {item["code"]: item["value"] for item in status_list}
        return {
            "device_id": self._device_id,
            "name": info.get("name", "Tuya Lock"),
            "online": info.get("online", False),
            "product_name": info.get("product_name", ""),
            "status": status,
        }

    # ------------------------------------------------------------------
    # Command helper
    # ------------------------------------------------------------------

    async def async_send_command(self, commands: list[dict]) -> bool:
        """Send a list of {code, value} commands to the device."""
        path = f"/v1.0/devices/{self._device_id}/commands"
        body = json.dumps({"commands": commands})
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex

        try:
            async with aiohttp.ClientSession() as session:
                token = await self._get_token(session)
                sign = self._sign(ts, nonce, "POST", path, token, body)
                headers = self._base_headers(ts, nonce, sign, token)
                headers["Content-Type"] = "application/json"

                async with session.post(
                    self._endpoint + path, headers=headers, data=body
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            if not data.get("success"):
                _LOGGER.error(
                    "Tuya command failed %s: %s", data.get("code"), data.get("msg")
                )
                return False

            await self.async_request_refresh()
            return True

        except aiohttp.ClientError as err:
            _LOGGER.error("Network error sending command: %s", err)
            return False
