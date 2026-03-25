"""Tuya Lock Monitor coordinator — ping-driven local polling with cloud fallback."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CLOUD_META_REFRESH,
    CODE_TO_DPS,
    DOMAIN,
    DPS_TO_CODE,
    LOCAL_POLL_INTERVAL,
    PING_INTERVAL,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Tuya local protocol port used for TCP-ping reachability checks
TUYA_LOCAL_PORT = 6668


class TuyaLockCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator with 1-second TCP-ping loop for instant local reachability detection.

    Behaviour:
    - If local IP is set: pings port 6668 every second.
      - Ping OK  → polls tinytuya every LOCAL_POLL_INTERVAL seconds (default 15 s).
      - Ping fail → falls back to cloud (if cloud credentials provided); never gives up
                    on local — resumes local polling the moment the device responds again.
    - If no local IP: cloud polls on UPDATE_INTERVAL (default 60 s).
    - Local-only mode (no cloud creds): keeps trying local forever; returns stale data
      while unreachable so entities don't go unavailable.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        access_id: str,
        access_secret: str,
        device_id: str,
        endpoint: str,
        local_ip: str | None = None,
        local_version: str = "3.4",
        local_key_direct: str | None = None,
    ) -> None:
        self._access_id = access_id
        self._access_secret = access_secret
        self._device_id = device_id
        self._endpoint = endpoint.rstrip("/")
        self._local_ip = local_ip or None
        self._local_version = float(local_version)
        self._local_key: str | None = local_key_direct or None
        self._cloud_enabled: bool = bool(access_id and access_secret)

        # Ping-loop state
        self._local_reachable: bool = False
        self._last_local_poll: float = 0.0
        self._ping_task: asyncio.Task | None = None
        self._last_contact: datetime | None = None

        # Cloud state
        self._cached_meta: dict[str, Any] = {}
        self._last_meta_refresh: float = 0.0
        self._token: str | None = None
        self._token_expire: float = 0.0

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # The coordinator's scheduled updates handle cloud fallback / meta refresh.
            # Local updates come from the ping loop via async_set_updated_data().
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    # ------------------------------------------------------------------
    # Ping loop — started by __init__.py after first refresh
    # ------------------------------------------------------------------

    @property
    def last_contact(self) -> datetime | None:
        """UTC datetime of the most recent successful data fetch from the device."""
        return self._last_contact

    async def async_start_ping_loop(self) -> None:
        """Start the background 1-second ping loop."""
        if self._ping_task and not self._ping_task.done():
            return
        self._ping_task = self.hass.async_create_task(
            self._ping_loop(), name="tuya_lock_ping"
        )
        _LOGGER.debug("[TuyaPing] Ping loop started for %s", self._local_ip)

    def async_stop_ping_loop(self) -> None:
        """Cancel the ping loop (called on unload)."""
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            _LOGGER.debug("[TuyaPing] Ping loop stopped")

    async def _ping_loop(self) -> None:
        """Ping the device every PING_INTERVAL seconds.

        When reachable and LOCAL_POLL_INTERVAL has elapsed, do a full
        tinytuya status poll and push the result to listeners immediately via
        async_set_updated_data(). When unreachable, the next scheduled
        _async_update_data() call will use cloud fallback.
        """
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                reachable = await self._ping_local()
            except asyncio.CancelledError:
                return

            if reachable and not self._local_reachable:
                _LOGGER.info("[TuyaPing] Device back online at %s", self._local_ip)
            elif not reachable and self._local_reachable:
                _LOGGER.warning("[TuyaPing] Device went offline at %s", self._local_ip)

            self._local_reachable = reachable

            if not reachable:
                continue

            # Reachable — poll if enough time has elapsed since last poll
            if (time.time() - self._last_local_poll) < LOCAL_POLL_INTERVAL:
                continue

            try:
                status = await self._local_get_status()
                self._last_local_poll = time.time()
                result = self._build_result(
                    status,
                    "local" if self._cloud_enabled else "local_only",
                )
                self._last_contact = dt_util.utcnow()
                self.async_set_updated_data(result)
                _LOGGER.debug("[TuyaPing] Local poll OK — pushed to listeners")
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("[TuyaPing] Reachable but poll failed: %s", err)
                # Don't clear _local_reachable — ping still works; poll might
                # succeed next cycle (e.g. a transient DPS read error)

    async def _ping_local(self) -> bool:
        """Return True if the device responds on port 6668 within 0.5 s."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self._local_ip, TUYA_LOCAL_PORT),
                timeout=0.5,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

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
        signature = hmac.new(
            self._access_secret.encode(),
            message.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest().upper()
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
            _LOGGER.error(
                "[TuyaDevice] info failed code=%s msg=%s",
                data.get("code"), data.get("msg"),
            )
            raise UpdateFailed(
                f"Tuya device info error {data.get('code')}: {data.get('msg')}"
            )
        return data["result"]

    async def _cloud_device_status(
        self, session: aiohttp.ClientSession, token: str
    ) -> dict[str, Any]:
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
            _LOGGER.error(
                "[TuyaStatus] failed code=%s msg=%s",
                data.get("code"), data.get("msg"),
            )
            raise UpdateFailed(
                f"Tuya device status error {data.get('code')}: {data.get('msg')}"
            )
        return {item["code"]: item["value"] for item in data["result"]}

    # ------------------------------------------------------------------
    # Local LAN calls (tinytuya)
    # ------------------------------------------------------------------

    async def _local_get_status(self) -> dict[str, Any]:
        """Fetch device status directly over LAN using tinytuya."""
        import tinytuya  # noqa: PLC0415

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
            return d.status()

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
    # Helpers
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

    # ------------------------------------------------------------------
    # DataUpdateCoordinator — scheduled cloud fallback / meta refresh
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Called on the 60-second schedule.

        The ping loop handles all local updates via async_set_updated_data().
        This method handles:
        - Cloud-only mode (no local IP)
        - Cloud fallback when local is unreachable
        - Periodic cloud metadata refresh (renews local_key from cloud)
        - Local-only mode when device is unreachable (returns stale data)
        """
        now = time.time()

        # ── Local-only mode (no cloud credentials) ──────────────────────
        if not self._cloud_enabled:
            if not self._local_ip or not self._local_key:
                raise UpdateFailed(
                    "Local-only mode requires a device IP and local key. "
                    "Use Configure to update them."
                )
            if self._local_reachable:
                # Ping loop is actively polling; return current data if we have it,
                # otherwise do a one-off poll to satisfy the first-refresh requirement.
                if self.data:
                    return self.data
                status = await self._local_get_status()
                self._last_local_poll = now
                self._last_contact = dt_util.utcnow()
                return self._build_result(status, "local_only")

            # Device unreachable — return stale data so entities stay available,
            # or raise on first call (no data yet).
            if self.data:
                _LOGGER.debug(
                    "[TuyaLocal] Device unreachable — returning stale data while retrying"
                )
                return self.data
            raise UpdateFailed(
                "Cannot reach the lock at %s — check the IP and that the device is on." %
                self._local_ip
            )

        # ── Cloud credentials available ──────────────────────────────────
        need_meta = not self._cached_meta or (now - self._last_meta_refresh) > CLOUD_META_REFRESH

        if self._local_ip:
            # Hybrid mode — ping loop drives local updates.
            if self._local_reachable and self.data:
                # Refresh cloud meta periodically (renews local_key).
                if need_meta:
                    try:
                        async with aiohttp.ClientSession() as session:
                            await self._refresh_cloud_meta(session)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning(
                            "[TuyaLocal] Cloud meta refresh failed (using cache): %s", err
                        )
                return self.data

            # Local unreachable — cloud fallback; never give up on local.
            _LOGGER.info("[TuyaCloud] Local unreachable — polling cloud as fallback")
            try:
                async with aiohttp.ClientSession() as session:
                    await self._refresh_cloud_meta(session)
                    token = await self._get_token(session)
                    status = await self._cloud_device_status(session, token)
                self._last_contact = dt_util.utcnow()
                return self._build_result(status, "cloud_fallback")
            except Exception as err:  # noqa: BLE001
                if self.data:
                    _LOGGER.warning(
                        "[TuyaCloud] Cloud fallback also failed — returning stale data: %s", err
                    )
                    return self.data
                raise UpdateFailed(f"Both local and cloud unavailable: {err}") from err

        # ── Cloud-only mode (no local IP) ────────────────────────────────
        try:
            async with aiohttp.ClientSession() as session:
                await self._refresh_cloud_meta(session)
                token = await self._get_token(session)
                status = await self._cloud_device_status(session, token)
        except Exception as err:  # noqa: BLE001
            if self.data:
                _LOGGER.warning(
                    "[TuyaCloud] Cloud poll failed — returning stale data: %s", err
                )
                return self.data
            raise UpdateFailed(f"Network error: {err}") from err

        self._last_contact = dt_util.utcnow()
        return self._build_result(status, "cloud")

    # ------------------------------------------------------------------
    # Command helper — local first (if reachable), cloud fallback
    # ------------------------------------------------------------------

    async def async_send_command(self, commands: list[dict]) -> bool:
        """Send commands — local if reachable, cloud otherwise."""
        if self._local_ip and self._local_key and self._local_reachable:
            try:
                await self._local_send_command(commands)
                await asyncio.sleep(0.3)
                await self.async_request_refresh()
                return True
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "[TuyaLocal] Command failed%s: %s",
                    " — trying cloud" if self._cloud_enabled else "",
                    err,
                )
                if not self._cloud_enabled:
                    return False

        if not self._cloud_enabled:
            _LOGGER.error("[TuyaLocal] Cannot send command — device unreachable and no cloud configured")
            return False

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
                _LOGGER.error(
                    "Tuya command failed %s: %s", data.get("code"), data.get("msg")
                )
                return False

            await self.async_request_refresh()
            return True

        except aiohttp.ClientError as err:
            _LOGGER.error("Network error sending command: %s", err)
            return False
