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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CLOUD_META_REFRESH,
    CODE_TO_DPS,
    DOMAIN,
    DPS_TO_CODE,
    PING_INTERVAL,
    SMART_LOCK_DOOR_OPERATE_PATH,
    SMART_LOCK_TICKET_PATH,
    STATE_WATCH_DURATION,
    STATE_WATCH_INTERVAL,
    STATUS_LOCK_MOTOR_STATE,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class TuyaLockCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator with 1-second TCP-ping loop for instant local reachability detection."""

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

        # Burst-poll state (used after smart-lock door-operate so we catch the
        # auto-lock re-engagement before the next scheduled cloud poll).
        self._state_watch_task: asyncio.Task | None = None
        self._state_watch_until: float = 0.0

        # Doorbell auto-reset handle
        self._doorbell_reset_unsub: object | None = None
        # Unlock-event auto-reset handles
        self._unlock_reset_unsubs: dict[str, object] = {}

        # Cloud state
        self._cached_meta: dict[str, Any] = {}
        self._last_meta_refresh: float = 0.0
        self._token: str | None = None
        self._token_expire: float = 0.0

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    # ------------------------------------------------------------------
    # Ping loop
    # ------------------------------------------------------------------

    @property
    def last_contact(self) -> datetime | None:
        return self._last_contact

    async def async_start_ping_loop(self) -> None:
        if self._ping_task and not self._ping_task.done():
            return
        self._ping_task = self.hass.async_create_task(
            self._ping_loop(), name="tuya_lock_ping"
        )
        _LOGGER.debug("[TuyaPing] Ping loop started for %s", self._local_ip)

    def async_stop_ping_loop(self) -> None:
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            _LOGGER.debug("[TuyaPing] Ping loop stopped")
        if self._state_watch_task and not self._state_watch_task.done():
            self._state_watch_task.cancel()
            _LOGGER.debug("[TuyaWatch] State watch cancelled")

    async def _ping_loop(self) -> None:
        while True:
            reachable = False
            status: dict[str, Any] = {}
            try:
                status = await self._local_get_status()
                reachable = True
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                pass

            if reachable and not self._local_reachable:
                _LOGGER.info("[TuyaPing] Device back online at %s", self._local_ip)
            elif not reachable and self._local_reachable:
                _LOGGER.warning("[TuyaPing] Device went offline at %s", self._local_ip)

            self._local_reachable = reachable

            if reachable:
                try:
                    self._last_local_poll = time.time()
                    merged = self._merge_local_status(status)
                    result = self._build_result(
                        merged,
                        "local" if self._cloud_enabled else "local_only",
                    )
                    self._last_contact = dt_util.utcnow()
                    self.async_set_updated_data(result)
                    _LOGGER.debug("[TuyaPing] Local poll OK — pushed to listeners")
                except asyncio.CancelledError:
                    return
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("[TuyaPing] Reachable but push failed: %s", err)

            await asyncio.sleep(0.2 if reachable else 0.8)

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
                connection_timeout=0.3,
                connection_retry_limit=1,
                connection_retry_delay=0,
            )
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
    # Auto-reset helpers
    # ------------------------------------------------------------------

    _LOCAL_ONLY_KEYS: frozenset[str] = frozenset({
        "doorbell",
        "unlock_fingerprint",
        "unlock_password",
        "unlock_card",
    })

    def _schedule_doorbell_reset(self) -> None:
        if self._doorbell_reset_unsub is not None:
            self._doorbell_reset_unsub()  # type: ignore[operator]
            self._doorbell_reset_unsub = None
        self._doorbell_reset_unsub = async_call_later(
            self.hass, 1, self._async_clear_doorbell
        )

    @callback
    def _async_clear_doorbell(self, _now: object = None) -> None:
        self._doorbell_reset_unsub = None
        if self.data and self.data.get("status", {}).get("doorbell"):
            new_status = {**self.data["status"], "doorbell": False}
            new_data = {**self.data, "status": new_status}
            self.async_set_updated_data(new_data)
            _LOGGER.debug("[TuyaDoorbell] Doorbell reset to False")

    def _schedule_unlock_reset(self, key: str) -> None:
        old = self._unlock_reset_unsubs.pop(key, None)
        if old is not None:
            old()  # type: ignore[operator]
        self._unlock_reset_unsubs[key] = async_call_later(
            self.hass, 1, lambda _now, k=key: self._async_clear_unlock(k)
        )

    @callback
    def _async_clear_unlock(self, key: str) -> None:
        self._unlock_reset_unsubs.pop(key, None)
        if self.data and self.data.get("status", {}).get(key):
            new_status = {**self.data["status"], key: 0}
            self.async_set_updated_data({**self.data, "status": new_status})
            _LOGGER.debug("[TuyaUnlock] %s reset to 0", key)

    # ------------------------------------------------------------------

    def _build_result(self, status: dict[str, Any], mode: str) -> dict[str, Any]:
        if status.get("doorbell"):
            self._schedule_doorbell_reset()
        for key in ("unlock_fingerprint", "unlock_password", "unlock_card"):
            if status.get(key):
                self._schedule_unlock_reset(key)
        return {
            "device_id": self._device_id,
            "name": self._cached_meta.get("name", "Tuya Lock"),
            "online": self._cached_meta.get("online", True),
            "product_name": self._cached_meta.get("product_name", ""),
            "status": status,
            "mode": mode,
        }

    def _merge_local_status(self, new_status: dict[str, Any]) -> dict[str, Any]:
        if self.data and "status" in self.data:
            merged = dict(self.data["status"])
            merged.update(new_status)
            return merged
        return new_status

    async def _refresh_cloud_meta(self, session: aiohttp.ClientSession) -> None:
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
        now = time.time()

        if not self._cloud_enabled:
            if not self._local_ip or not self._local_key:
                raise UpdateFailed(
                    "Local-only mode requires a device IP and local key. "
                    "Use Configure to update them."
                )
            if self._local_reachable:
                if self.data:
                    return self.data
                status = await self._local_get_status()
                self._last_local_poll = now
                self._last_contact = dt_util.utcnow()
                return self._build_result(status, "local_only")

            if self.data:
                _LOGGER.debug(
                    "[TuyaLocal] Device unreachable — returning stale data while retrying"
                )
                return self.data
            raise UpdateFailed(
                "Cannot reach the lock at %s — check the IP and that the device is on." %
                self._local_ip
            )

        need_meta = not self._cached_meta or (now - self._last_meta_refresh) > CLOUD_META_REFRESH

        if self._local_ip:
            if self._local_reachable and self.data:
                if need_meta:
                    try:
                        async with aiohttp.ClientSession() as session:
                            await self._refresh_cloud_meta(session)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning(
                            "[TuyaLocal] Cloud meta refresh failed (using cache): %s", err
                        )
                return self.data

            _LOGGER.info("[TuyaCloud] Local unreachable — polling cloud as fallback")
            try:
                async with aiohttp.ClientSession() as session:
                    await self._refresh_cloud_meta(session)
                    token = await self._get_token(session)
                    cloud_status = await self._cloud_device_status(session, token)
                if self.data:
                    local_status = self.data.get("status", {})
                    for k in self._LOCAL_ONLY_KEYS:
                        if k in local_status:
                            cloud_status[k] = local_status[k]
                        else:
                            cloud_status.pop(k, None)
                return self._build_result(cloud_status, "cloud_fallback")
            except Exception as err:  # noqa: BLE001
                if self.data:
                    _LOGGER.warning(
                        "[TuyaCloud] Cloud fallback also failed — returning stale data: %s", err
                    )
                    return self.data
                raise UpdateFailed(f"Both local and cloud unavailable: {err}") from err

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

        return self._build_result(status, "cloud")

    # ------------------------------------------------------------------
    # Smart Lock cloud API — ticket-based remote unlock (DL026HA family)
    # ------------------------------------------------------------------

    async def _smart_lock_get_ticket(
        self, session: aiohttp.ClientSession, token: str
    ) -> str:
        """Fetch a short-lived unlock ticket for the Smart Lock API.

        The ticket endpoint is POSTed with no body (matching the verified
        curl); the signature must therefore hash an empty body too.
        """
        path = SMART_LOCK_TICKET_PATH.format(device_id=self._device_id)
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = self._sign(ts, nonce, "POST", path, token)  # body="" default
        headers = self._base_headers(ts, nonce, sign, token)
        headers["Content-Type"] = "application/json"
        _LOGGER.debug("[SmartLock] POST %s%s", self._endpoint, path)

        async with session.post(self._endpoint + path, headers=headers) as resp:
            raw = await resp.text()
            _LOGGER.debug("[SmartLock] ticket status=%d response=%s", resp.status, raw)
            data = json.loads(raw)

        if not data.get("success"):
            raise UpdateFailed(
                f"Smart-lock ticket error {data.get('code')}: {data.get('msg')}"
            )
        result = data.get("result") or {}
        ticket_id = result.get("ticket_id")
        if not ticket_id:
            raise UpdateFailed(f"Smart-lock ticket response missing ticket_id: {result}")
        return ticket_id

    async def async_smart_lock_door_operate(self, open_lock: bool) -> bool:
        if not self._cloud_enabled:
            _LOGGER.error(
                "[SmartLock] Remote unlock requires cloud credentials — "
                "the Smart Lock API is cloud-only."
            )
            return False

        path = SMART_LOCK_DOOR_OPERATE_PATH.format(device_id=self._device_id)

        try:
            async with aiohttp.ClientSession() as session:
                token = await self._get_token(session)
                ticket_id = await self._smart_lock_get_ticket(session, token)
                body = json.dumps({"ticket_id": ticket_id, "open": bool(open_lock)})

                ts = str(int(time.time() * 1000))
                nonce = uuid.uuid4().hex
                sign = self._sign(ts, nonce, "POST", path, token, body)
                headers = self._base_headers(ts, nonce, sign, token)
                headers["Content-Type"] = "application/json"
                _LOGGER.debug(
                    "[SmartLock] POST %s%s body=%s", self._endpoint, path, body
                )

                async with session.post(
                    self._endpoint + path, headers=headers, data=body
                ) as resp:
                    raw = await resp.text()
                    _LOGGER.debug(
                        "[SmartLock] door-operate status=%d response=%s",
                        resp.status, raw,
                    )
                    data = json.loads(raw)

            if not data.get("success"):
                _LOGGER.error(
                    "[SmartLock] door-operate failed code=%s msg=%s",
                    data.get("code"), data.get("msg"),
                )
                return False

            # Optimistically reflect the commanded motor state so the UI
            # updates instantly, then burst-poll briefly to catch the
            # auto-lock re-engagement.
            if self.data is not None and self.data.get("status") is not None:
                new_status = {
                    **self.data["status"],
                    STATUS_LOCK_MOTOR_STATE: not bool(open_lock),
                }
                self.async_set_updated_data({**self.data, "status": new_status})

            await self.async_watch_lock_state()
            return True

        except aiohttp.ClientError as err:
            _LOGGER.error("[SmartLock] Network error: %s", err)
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("[SmartLock] Unexpected error: %s", err)
            return False

    # ------------------------------------------------------------------
    # State watch — burst-poll cloud status after a door-operate call
    # ------------------------------------------------------------------

    async def async_watch_lock_state(
        self,
        duration: float = STATE_WATCH_DURATION,
        interval: float = STATE_WATCH_INTERVAL,
    ) -> None:
        """Poll cloud status rapidly for a bounded window.

        Stops early as soon as we see ``lock_motor_state`` transition from
        False (unlocked) to True (locked) — i.e. the auto-lock has
        re-engaged — to keep API call count low. Calling while a watch is
        already running simply extends the existing window.
        """
        if not self._cloud_enabled:
            return

        self._state_watch_until = max(
            self._state_watch_until, time.time() + duration
        )
        if self._state_watch_task and not self._state_watch_task.done():
            return

        async def _watch() -> None:
            last_state: Any = None
            if self.data:
                last_state = self.data.get("status", {}).get(STATUS_LOCK_MOTOR_STATE)
            saw_unlocked = last_state is False
            try:
                async with aiohttp.ClientSession() as session:
                    while time.time() < self._state_watch_until:
                        try:
                            token = await self._get_token(session)
                            cloud_status = await self._cloud_device_status(
                                session, token
                            )
                            if self.data is not None:
                                local_status = self.data.get("status", {})
                                merged = {**local_status, **cloud_status}
                                for k in self._LOCAL_ONLY_KEYS:
                                    if k in local_status:
                                        merged[k] = local_status[k]
                                self.async_set_updated_data(
                                    {**self.data, "status": merged}
                                )

                            current = cloud_status.get(STATUS_LOCK_MOTOR_STATE)
                            if current is False:
                                saw_unlocked = True
                            if saw_unlocked and current is True:
                                _LOGGER.debug(
                                    "[TuyaWatch] Auto-lock re-engaged; stopping"
                                )
                                return
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.debug("[TuyaWatch] Poll error: %s", err)
                        await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            finally:
                _LOGGER.debug("[TuyaWatch] State watch ended")
                self._state_watch_task = None

        self._state_watch_task = self.hass.async_create_task(
            _watch(), name="tuya_state_watch"
        )

    # ------------------------------------------------------------------
    # Command helper — local first (if reachable), cloud fallback
    # ------------------------------------------------------------------

    async def async_send_command(self, commands: list[dict]) -> bool:
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