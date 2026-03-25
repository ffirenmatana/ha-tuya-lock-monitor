"""Tuya OpenAPI coordinator — handles authentication, local polling, and cloud fallback."""
from __future__ import annotations

import asyncio
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

from .const import (
    CLOUD_META_REFRESH,
    CODE_TO_DPS,
    DOMAIN,
    DPS_TO_CODE,
    LOCAL_FAIL_THRESHOLD,
    LOCAL_UPDATE_INTERVAL,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class TuyaLockCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator — uses local LAN when available, falls back to Tuya cloud API."""

    def __init__(
        self,
        hass: HomeAssistant,
        access_id: str,
        access_secret: str,
        device_id: str,
        endpoint: str,
        local_ip: str | None = None,
        local_version: str = "3.4",
    ) -> None:
        self._access_id = access_id
        self._access_secret = access_secret
        self._device_id = device_id
        self._endpoint = endpoint.rstrip("/")
        self._local_ip = local_ip or None
        self._local_version = float(local_version)
        self._local_key: str | None = None
        self._local_fail_count = 0
        self._cached_meta: dict[str, Any] = {}
        self._last_meta_refresh: float = 0.0
        self._token: str | None = None
        self._token_expire: float = 0.0

        interval = LOCAL_UPDATE_INTERVAL if self._local_ip else UPDATE_INTERVAL
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
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
                raise UpdateFailed(f"Tuya token HTTP {status}: {raw}")
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
            raise UpdateFailed(f"Tuya token error code={code}: {msg}")

        result = data["result"]
        self._token = result["access_token"]
        self._token_expire = time.time() + result.get("expire_time", 7200) - 60
        _LOGGER.debug("[TuyaToken] Token obtained, expires in %s s", result.get("expire_time"))
        return self._token

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        if self._token is None or time.time() >= self._token_expire:
            await self._fetch_token(session)
        return self._token  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Cloud API calls
    # ------------------------------------------------------------------

    async def _cloud_device_info(self, session: aiohttp.ClientSession, token: str) -> dict:
        path = f"/v1.0/devices/{self._device_id}"
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = self._sign(ts, nonce, "GET", path, token)
        headers = self._base_headers(ts, nonce, sign, token)
        _LOGGER.debug("[TuyaDevice] GET %s%s", self._endpoint, path)

        async with session.get(self._endpoint + path, headers=headers) as resp:
            raw = await resp.text()
            _LOGGER.debug("[TuyaDevice] status=%d response=%s", resp.status, raw)
            data = json.loads(raw)

        if not data.get("success"):
            _LOGGER.error("[TuyaDevice] info failed code=%s msg=%s", data.get("code"), data.get("msg"))
            raise UpdateFailed(f"Tuya device info error {data.get('code')}: {data.get('msg')}")
        return data["result"]

    async def _cloud_device_status(self, session: aiohttp.ClientSession, token: str) -> dict[str, Any]:
        path = f"/v1.0/devices/{self._device_id}/status"
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = self._sign(ts, nonce, "GET", path, token)
        headers = self._base_headers(ts, nonce, sign, token)
        _LOGGER.debug("[TuyaStatus] GET %s%s", self._endpoint, path)

        async with session.get(self._endpoint + path, headers=headers) as resp:
            raw = await resp.text()
            _LOGGER.debug("[TuyaStatus] status=%d response=%s", resp.status, raw)
            data = json.loads(raw)

        if not data.get("success"):
            _LOGGER.error("[TuyaStatus] failed code=%s msg=%s", data.get("code"), data.get("msg"))
            raise UpdateFailed(f"Tuya device status error {data.get('code')}: {data.get('msg')}")
        return {item["code"]: item["value"] for item in data["result"]}

    # ------------------------------------------------------------------
    # Local LAN calls (tinytuya)
    # ------------------------------------------------------------------

    async def _local_get_status(self) -> dict[str, Any]:
        """Fetch device status directly over LAN using tinytuya."""
        import tinytuya  # noqa: PLC0415 — deferred to avoid import cost when not needed

        device_id = self._device_id
        local_ip = self._local_ip
        local_key = self._local_key
        version = self._local_version

        def _sync_fetch() -> dict:
            d = tinytuya.Device(
                dev_id=device_id,
                address=local_ip,
                local_key=local_key,
                version=version,
            )
            d.set_socketTimeout(5)
            result = d.status()
            return result

        result: dict = await self.hass.async_add_executor_job(_sync_fetch)

        if not result or "Error" in result:
            raise RuntimeError(
                f"tinytuya error: {result.get('Error', result) if result else 'no response'}"
            )

        dps: dict = result.get("dps", {})
        status = {DPS_TO_CODE[str(k)]: v for k, v in dps.items() if str(k) in DPS_TO_CODE}
        _LOGGER.debug("[TuyaLocal] status=%s", status)
        return status

    async def _local_send_command(self, commands: list[dict]) -> None:
        """Send commands to the device directly over LAN."""
        import tinytuya  # noqa: PLC0415

        device_id = self._device_id
        local_ip = self._local_ip
        local_key = self._local_key
        version = self._local_version

        def _sync_send() -> None:
            d = tinytuya.Device(
                dev_id=device_id,
                address=local_ip,
                local_key=local_key,
                version=version,
            )
            d.set_socketTimeout(5)
            for cmd in commands:
                dp = CODE_TO_DPS.get(cmd["code"])
                if dp is not None:
                    result = d.set_value(dp, cmd["value"])
                    _LOGGER.debug(
                        "[TuyaLocal] Command dp=%d value=%s result=%s",
                        dp, cmd["value"], result,
                    )

        await self.hass.async_add_executor_job(_sync_send)

    # ------------------------------------------------------------------
    # DataUpdateCoordinator
    # ------------------------------------------------------------------

    def _build_result(self, status: dict[str, Any], mode: str) -> dict[str, Any]:
        return {
            "device_id": self._device_id,
            "name": self._cached_meta.get("name", "Tuya Lock"),
            "online": self._cached_meta.get("online", True),
            "product_name": self._cached_meta.get("product_name", ""),
            "status": status,
            "mode": mode,
        }

    async def _refresh_cloud_meta(self, session: aiohttp.ClientSession) -> None:
        """Fetch device info from cloud and cache metadata + local_key."""
        token = await self._get_token(session)
        info = await self._cloud_device_info(session, token)
        self._cached_meta = info
        self._last_meta_refresh = time.time()
        if info.get("local_key"):
            self._local_key = info["local_key"]
            _LOGGER.debug("[TuyaLocal] local_key refreshed from cloud")

    async def _async_update_data(self) -> dict[str, Any]:
        now = time.time()
        need_meta = not self._cached_meta or (now - self._last_meta_refresh) > CLOUD_META_REFRESH

        # --- Local mode ---
        if self._local_ip:
            # Refresh cloud metadata (and local_key) periodically
            if need_meta:
                try:
                    async with aiohttp.ClientSession() as session:
                        await self._refresh_cloud_meta(session)
                except Exception as err:  # noqa: BLE001
                    if not self._cached_meta:
                        raise UpdateFailed(f"Cannot reach cloud for initial setup: {err}") from err
                    _LOGGER.warning("[TuyaLocal] Cloud metadata refresh failed (using cache): %s", err)

            if not self._local_key:
                raise UpdateFailed("No local_key available — cloud metadata fetch required")

            # Try local
            if self._local_fail_count < LOCAL_FAIL_THRESHOLD:
                try:
                    status = await self._local_get_status()
                    self._local_fail_count = 0
                    return self._build_result(status, "local")
                except Exception as err:  # noqa: BLE001
                    self._local_fail_count += 1
                    _LOGGER.warning(
                        "[TuyaLocal] Local fetch failed (%d/%d): %s",
                        self._local_fail_count, LOCAL_FAIL_THRESHOLD, err,
                    )
            else:
                _LOGGER.warning(
                    "[TuyaLocal] %d consecutive local failures — falling back to cloud",
                    self._local_fail_count,
                )

            # Cloud fallback
            try:
                async with aiohttp.ClientSession() as session:
                    token = await self._get_token(session)
                    status = await self._cloud_device_status(session, token)
                return self._build_result(status, "cloud_fallback")
            except aiohttp.ClientError as err:
                raise UpdateFailed(f"Both local and cloud failed: {err}") from err

        # --- Cloud-only mode ---
        try:
            async with aiohttp.ClientSession() as session:
                await self._refresh_cloud_meta(session)
                token = await self._get_token(session)
                status = await self._cloud_device_status(session, token)
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Network error: {err}") from err

        return self._build_result(status, "cloud")

    # ------------------------------------------------------------------
    # Command helper (local first, cloud fallback)
    # ------------------------------------------------------------------

    async def async_send_command(self, commands: list[dict]) -> bool:
        """Send commands — tries local first if configured, falls back to cloud."""
        if self._local_ip and self._local_key and self._local_fail_count < LOCAL_FAIL_THRESHOLD:
            try:
                await self._local_send_command(commands)
                await asyncio.sleep(0.3)
                await self.async_request_refresh()
                return True
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("[TuyaLocal] Command failed, trying cloud: %s", err)

        return await self._cloud_send_command(commands)

    async def _cloud_send_command(self, commands: list[dict]) -> bool:
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
                _LOGGER.error("Tuya command failed %s: %s", data.get("code"), data.get("msg"))
                return False

            await self.async_request_refresh()
            return True

        except aiohttp.ClientError as err:
            _LOGGER.error("Network error sending command: %s", err)
            return False



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
