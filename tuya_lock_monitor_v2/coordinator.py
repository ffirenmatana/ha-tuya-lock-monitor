"""Tuya Lock Monitor v2 coordinator.

Differences from v1:
  * Never writes the `automatic_lock` DP — that is a read-only status
    ("auto-lock timer armed") on DL026HA firmware, and writing caused a
    phantom unlock. Use `async_smart_lock_door_operate(open_lock=False)`
    to relock instead.
  * Exposes `async_lock_door()` / `async_unlock_door()` helpers so the lock
    entity never has to reason about which API to hit.
  * No passage-mode logic (deferred — unreliable on BLE locks).
  * Domain constant bumped so v1 and v2 can coexist.
"""
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
    EVENT_UNLOCK,
    AUTO_LOCK_TIME_DEFAULT,
    PASSAGE_MODE_MAX_AUTO_LOCK,
    PING_INTERVAL,
    SMART_LOCK_DOOR_OPERATE_PATH,
    SMART_LOCK_TICKET_PATH,
    STATE_WATCH_DURATION,
    STATE_WATCH_INTERVAL,
    STATUS_AUTO_LOCK_TIME,
    STATUS_AUTOMATIC_LOCK,
    STATUS_LOCK_MOTOR_STATE,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class TuyaLockCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """DataUpdateCoordinator for the DL026HA-family locks (v2)."""

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

        # Burst-poll state (used after smart-lock door-operate).
        self._state_watch_task: asyncio.Task | None = None
        self._state_watch_until: float = 0.0

        # Auto-reset handles for edge-triggered DPs.
        self._doorbell_reset_unsub: object | None = None
        self._unlock_reset_unsubs: dict[str, object] = {}

        # Last-seen user event per unlock kind (survives the DP's auto-zero).
        # Maps status_key → {"id": int, "time": datetime}.
        self._last_user_event: dict[str, dict[str, Any]] = {}

        # Passage-mode state. We still capture the previous auto_lock_time so
        # we can restore it on exit (and so the 1800s safety cap is reverted).
        self._passage_mode_active: bool = False
        self._passage_saved_auto_lock: int | None = None

        # First-refresh flag — we always query the device-logs endpoint on
        # the first refresh after HA startup to seed lock_motor_state from
        # the authoritative event stream rather than the (potentially stale)
        # cloud /status cache. Subsequent refreshes rely on status + the
        # state-watch burst poll after door-operate.
        self._motor_state_seeded: bool = False

        # Derived-state tracking. lock_motor_state on DL026HA only tracks
        # the most-recent cloud-API door-operate command; it doesn't reflect
        # actual door state for Tuya-app unlocks, fingerprint scans, or the
        # auto-lock timer firing. We derive the lock entity's state from:
        #   1. automatic_lock (false = passage mode = always unlocked)
        #   2. _last_unlock_at within auto_lock_time + grace window
        #   3. otherwise locked
        # _last_unlock_at is set whenever we observe ANY unlock event:
        # HA door-operate, fingerprint/password/card pulses, or any of the
        # _UNLOCK_COUNTER_KEYS counters incrementing.
        self._last_unlock_at: datetime | None = None
        # Per-key snapshot of unlock counters; deltas indicate fresh events.
        # Seeded on first observation so historical counts don't fire spurious
        # events at startup.
        self._unlock_counter_baseline: dict[str, int] = {}
        self._unlock_counter_baseline_seeded: bool = False

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

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def cloud_enabled(self) -> bool:
        return self._cloud_enabled

    @property
    def passage_mode_active(self) -> bool:
        return self._passage_mode_active

    @property
    def last_unlock_at(self) -> datetime | None:
        """Most recent observed unlock event from any source.

        Sources: HA door-operate calls, fingerprint/password/card pulses,
        and increments of the cloud-tracked unlock_* counters.
        """
        return self._last_unlock_at

    # Counter DPs that increment monotonically on each unlock of that kind.
    # Pulse-based unlocks (fingerprint/password/card) are handled separately
    # via _last_user_event because those DPs reset to 0.
    _UNLOCK_COUNTER_KEYS: tuple[str, ...] = (
        "unlock_app",
        "unlock_temporary",
        "unlock_phone_remote",
        "unlock_ble",
    )

    def _record_unlock_event(self, source: str) -> None:
        """Mark 'now' as the last observed unlock event."""
        self._last_unlock_at = dt_util.utcnow()
        _LOGGER.info(
            "[TuyaUnlock] Detected unlock from %s at %s",
            source, self._last_unlock_at.isoformat(),
        )

    def _record_lock_event(self, source: str) -> None:
        """Clear the recent-unlock state (deliberate lock action)."""
        if self._last_unlock_at is not None:
            _LOGGER.info(
                "[TuyaUnlock] Cleared recent-unlock state from %s",
                source,
            )
        self._last_unlock_at = None

    def _detect_unlock_counter_events(self, status: dict[str, Any]) -> None:
        """Compare incrementing unlock counters to baseline; fire on delta.

        On the first observation of each counter we just record the value
        without firing — historical counts shouldn't trigger spurious unlock
        events when HA starts up.
        """
        any_seeded = False
        for key in self._UNLOCK_COUNTER_KEYS:
            raw = status.get(key)
            if raw is None:
                continue
            try:
                current = int(raw)
            except (TypeError, ValueError):
                continue
            if not self._unlock_counter_baseline_seeded:
                self._unlock_counter_baseline[key] = current
                any_seeded = True
                continue
            previous = self._unlock_counter_baseline.get(key)
            if previous is None:
                # First time we've seen this specific counter.
                self._unlock_counter_baseline[key] = current
                continue
            if current > previous:
                self._unlock_counter_baseline[key] = current
                self._record_unlock_event(key)
        if any_seeded and not self._unlock_counter_baseline_seeded:
            self._unlock_counter_baseline_seeded = True

    def last_user_event(self, status_key: str) -> dict[str, Any] | None:
        """Return the most recently observed non-zero event for a user-ID DP.

        ``status_key`` must be one of ``unlock_fingerprint``,
        ``unlock_password``, ``unlock_card``. Returns ``None`` until the first
        event is observed.
        """
        return self._last_user_event.get(status_key)

    async def async_start_ping_loop(self) -> None:
        if self._ping_task and not self._ping_task.done():
            return
        self._ping_task = self.hass.async_create_task(
            self._ping_loop(), name="tuya_lock_v2_ping"
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
        async with session.get(url, headers=headers) as resp:
            status = resp.status
            raw = await resp.text()
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

        async with session.get(self._endpoint + path, headers=headers) as resp:
            raw = await resp.text()
            data = json.loads(raw)

        if not data.get("success"):
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

        async with session.get(self._endpoint + path, headers=headers) as resp:
            raw = await resp.text()
            data = json.loads(raw)

        if not data.get("success"):
            raise UpdateFailed(
                f"Tuya device status error {data.get('code')}: {data.get('msg')}"
            )
        return {item["code"]: item["value"] for item in data["result"]}

    async def async_cloud_get_specifications(self) -> dict[str, Any]:
        """GET /v1.0/devices/{device_id}/specifications.

        Returns the full DP schema (category, functions, status) reported by
        the Tuya IoT Platform for this device. Useful for discovering DPs
        that the device supports but doesn't include in its status payload
        until they've been written to — most notably passage-mode
        candidates like ``normal_open_switch``.

        Raises ``UpdateFailed`` on cloud errors. Requires cloud credentials.
        """
        if not self._cloud_enabled:
            raise UpdateFailed(
                "Device specifications require cloud credentials — the "
                "endpoint is only reachable via the Tuya IoT Platform."
            )
        path = f"/v1.0/devices/{self._device_id}/specifications"
        async with aiohttp.ClientSession() as session:
            token = await self._get_token(session)
            ts = str(int(time.time() * 1000))
            nonce = uuid.uuid4().hex
            sign = self._sign(ts, nonce, "GET", path, token)
            headers = self._base_headers(ts, nonce, sign, token)
            async with session.get(self._endpoint + path, headers=headers) as resp:
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise UpdateFailed(
                        f"Specifications: non-JSON response: {raw[:200]}"
                    ) from exc
        if not data.get("success"):
            raise UpdateFailed(
                f"Specifications error {data.get('code')}: {data.get('msg')}"
            )
        return data.get("result") or {}

    async def _cloud_device_logs(
        self,
        session: aiohttp.ClientSession,
        token: str,
        codes: str,
        size: int = 1,
    ) -> list[dict]:
        end_time = int(time.time() * 1000)
        start_time = end_time - (30 * 24 * 3600 * 1000)
        query = (
            f"codes={codes}&size={size}&type=7"
            f"&start_time={start_time}&end_time={end_time}"
        )
        path = f"/v1.0/devices/{self._device_id}/logs"
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = self._sign(ts, nonce, "GET", f"{path}?{query}", token)
        headers = self._base_headers(ts, nonce, sign, token)

        async with session.get(
            f"{self._endpoint}{path}?{query}", headers=headers
        ) as resp:
            raw = await resp.text()
            data = json.loads(raw)

        if not data.get("success"):
            return []
        return data.get("result", {}).get("logs") or []

    @staticmethod
    def _coerce_log_value(raw: Any) -> Any:
        if raw in ("true", "True"):
            return True
        if raw in ("false", "False"):
            return False
        if isinstance(raw, str) and raw.lstrip("-").isdigit():
            return int(raw)
        return raw

    async def _seed_missing_state(
        self,
        session: aiohttp.ClientSession,
        token: str,
        status: dict[str, Any],
    ) -> None:
        """Reconcile lock_motor_state with the device-logs event stream.

        For BLE sub-devices the cloud /status cache can lag indefinitely
        (the lock only pushes changes through the gateway). The device-
        logs endpoint is event-driven and authoritative. We unconditionally
        query it once per HA startup and prefer its value, then fall back
        to status for the rest of the session unless the field is missing.
        """
        if STATUS_AUTOMATIC_LOCK not in status:
            if not self._motor_state_seeded:
                _LOGGER.info(
                    "[TuyaSeed] Skipped: automatic_lock not in /status, "
                    "treating as non-DL026HA family device"
                )
                self._motor_state_seeded = True
            return

        # After the first refresh, only run the log query if status is
        # actually missing the field (the original v1 behaviour).
        if self._motor_state_seeded and STATUS_LOCK_MOTOR_STATE in status:
            return

        first_run = not self._motor_state_seeded
        if first_run:
            _LOGGER.info(
                "[TuyaSeed] First refresh — querying device logs to verify "
                "lock_motor_state (cloud /status reported %s)",
                status.get(STATUS_LOCK_MOTOR_STATE, "<missing>"),
            )

        try:
            logs = await self._cloud_device_logs(
                session, token, STATUS_LOCK_MOTOR_STATE, size=1
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "[TuyaSeed] device-logs query failed (status value retained): %s",
                err,
            )
            self._motor_state_seeded = True
            return

        self._motor_state_seeded = True

        if not logs:
            _LOGGER.info(
                "[TuyaSeed] device-logs returned no lock_motor_state events "
                "in the last 30 days — keeping /status value %s",
                status.get(STATUS_LOCK_MOTOR_STATE, "<missing>"),
            )
            return
        value = self._coerce_log_value(logs[0].get("value"))
        previous = status.get(STATUS_LOCK_MOTOR_STATE)
        status[STATUS_LOCK_MOTOR_STATE] = value
        if previous is None:
            _LOGGER.info(
                "[TuyaSeed] Seeded lock_motor_state=%s from logs event at %s",
                value, logs[0].get("event_time"),
            )
        elif previous != value:
            _LOGGER.warning(
                "[TuyaSeed] lock_motor_state corrected from stale /status "
                "value %s → %s (logs event_time=%s). The /status cache lags "
                "for BLE sub-devices; logs are authoritative.",
                previous, value, logs[0].get("event_time"),
            )
        else:
            _LOGGER.info(
                "[TuyaSeed] lock_motor_state=%s confirmed by latest logs "
                "event at %s",
                value, logs[0].get("event_time"),
            )

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
                    d.set_value(dp, cmd["value"])

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

    # Keys where the cloud (especially the device-logs endpoint) is the
    # authoritative source. For BLE sub-devices behind an SG120HA gateway,
    # tinytuya's view of these is the gateway's cached value, which can lag
    # the actual lock indefinitely. When cloud is enabled we strip these
    # from local status before merging so the cloud-corrected value
    # persists across the ping loop.
    _CLOUD_AUTHORITATIVE_KEYS: frozenset[str] = frozenset({
        STATUS_LOCK_MOTOR_STATE,
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
            self.async_set_updated_data({**self.data, "status": new_status})

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

    # ------------------------------------------------------------------

    def _build_result(self, status: dict[str, Any], mode: str) -> dict[str, Any]:
        if status.get("doorbell"):
            self._schedule_doorbell_reset()

        # Track counter-based unlock events (Tuya app, BLE, temporary code).
        # Fingerprint/password/card pulses are handled below via _last_user_event.
        self._detect_unlock_counter_events(status)

        # Track last-seen user events so the sensor keeps displaying a name
        # even after the DP's momentary pulse returns to 0. Fire a bus event
        # only on a transition into a non-zero ID (not on repeated polls of
        # the same pulse).
        old_status = (self.data or {}).get("status", {}) or {}
        for key in ("unlock_fingerprint", "unlock_password", "unlock_card"):
            new_raw = status.get(key)
            if new_raw is None or new_raw == 0:
                continue
            try:
                new_id = int(new_raw)
            except (TypeError, ValueError):
                continue
            if new_id == 0:
                continue
            old_raw = old_status.get(key)
            try:
                old_id = int(old_raw) if old_raw not in (None, 0, "") else 0
            except (TypeError, ValueError):
                old_id = 0
            if old_id == new_id:
                # Same pulse still being observed — don't re-fire.
                continue
            self._last_user_event[key] = {
                "id": new_id,
                "time": dt_util.utcnow(),
            }
            self.hass.bus.async_fire(
                EVENT_UNLOCK,
                {
                    "device_id": self._device_id,
                    "device_name": self._cached_meta.get("name", "Tuya Lock"),
                    "kind": key.removeprefix("unlock_"),
                    "id": new_id,
                    "time": dt_util.utcnow().isoformat(),
                },
            )
            self._record_unlock_event(key)
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
        # When cloud is enabled, the cloud is the source of truth for some
        # keys (lock_motor_state for BLE sub-devices). Drop them from the
        # local payload so the merge doesn't clobber the cloud value.
        if self._cloud_enabled:
            new_status = {
                k: v for k, v in new_status.items()
                if k not in self._CLOUD_AUTHORITATIVE_KEYS
            }
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
                return self.data
            raise UpdateFailed(
                "Cannot reach the lock at %s — check the IP and that the device is on." %
                self._local_ip
            )

        need_meta = not self._cached_meta or (now - self._last_meta_refresh) > CLOUD_META_REFRESH

        if self._local_ip:
            # Cloud + local mode. We always poll cloud /status here — even
            # when local is reachable — so externally-triggered events
            # (Tuya app unlocks, fingerprint scans, the auto-lock timer
            # firing) are reflected in HA. The local ping loop runs in
            # parallel at sub-second cadence and is responsible for the
            # local-only / push-only DPs (unlock_fingerprint pulses etc.).
            try:
                async with aiohttp.ClientSession() as session:
                    if need_meta:
                        await self._refresh_cloud_meta(session)
                    token = await self._get_token(session)
                    cloud_status = await self._cloud_device_status(session, token)
                    await self._seed_missing_state(session, token, cloud_status)
            except Exception as err:  # noqa: BLE001
                if self.data:
                    _LOGGER.warning(
                        "[TuyaCloud] Scheduled cloud poll failed — keeping stale data: %s",
                        err,
                    )
                    return self.data
                raise UpdateFailed(f"Cloud poll failed and no cached data: {err}") from err

            # Layer local-only DPs on top of the cloud snapshot. These are
            # values the cloud /status can't see (unlock pulses, doorbell)
            # but the local ping loop has captured.
            if self.data:
                local_status = self.data.get("status", {})
                for k in self._LOCAL_ONLY_KEYS:
                    if k in local_status:
                        cloud_status[k] = local_status[k]
                    else:
                        cloud_status.pop(k, None)

            mode = "cloud+local" if self._local_reachable else "cloud_fallback"
            return self._build_result(cloud_status, mode)

        try:
            async with aiohttp.ClientSession() as session:
                await self._refresh_cloud_meta(session)
                token = await self._get_token(session)
                status = await self._cloud_device_status(session, token)
                await self._seed_missing_state(session, token, status)
        except Exception as err:  # noqa: BLE001
            if self.data:
                _LOGGER.warning(
                    "[TuyaCloud] Cloud poll failed — returning stale data: %s", err
                )
                return self.data
            raise UpdateFailed(f"Network error: {err}") from err

        return self._build_result(status, "cloud")

    # ------------------------------------------------------------------
    # Smart Lock cloud API — ticket-based door operate (DL026HA family)
    # ------------------------------------------------------------------

    async def _smart_lock_get_ticket(
        self, session: aiohttp.ClientSession, token: str
    ) -> str:
        path = SMART_LOCK_TICKET_PATH.format(device_id=self._device_id)
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = self._sign(ts, nonce, "POST", path, token)
        headers = self._base_headers(ts, nonce, sign, token)
        headers["Content-Type"] = "application/json"

        async with session.post(self._endpoint + path, headers=headers) as resp:
            raw = await resp.text()
            data = json.loads(raw)

        if not data.get("success"):
            raise UpdateFailed(
                f"Smart-lock ticket error {data.get('code')}: {data.get('msg')}"
            )
        result = data.get("result") or {}
        ticket_id = result.get("ticket_id")
        if not ticket_id:
            raise UpdateFailed(
                f"Smart-lock ticket response missing ticket_id: {result}"
            )
        return ticket_id

    async def async_smart_lock_door_operate(self, open_lock: bool) -> bool:
        """POST /password-free/door-operate with open=true|false.

        open_lock=True  → remote unlock.
        open_lock=False → remote lock (re-engage the latch immediately).
        Returns True on API success, False otherwise.
        """
        if not self._cloud_enabled:
            _LOGGER.error(
                "[SmartLock] Remote unlock/lock requires cloud credentials — "
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

                async with session.post(
                    self._endpoint + path, headers=headers, data=body
                ) as resp:
                    raw = await resp.text()
                    _LOGGER.debug(
                        "[SmartLock] door-operate open=%s status=%d response=%s",
                        open_lock, resp.status, raw,
                    )
                    data = json.loads(raw)

            if not data.get("success"):
                _LOGGER.error(
                    "[SmartLock] door-operate failed code=%s msg=%s",
                    data.get("code"), data.get("msg"),
                )
                return False

            # Optimistically reflect the commanded motor state. Note the
            # firmware's lock_motor_state semantic is inverted relative to
            # the DP name: true = motor in unlocked position, false = locked.
            # On DL026HA the lock entity ignores motor_state and uses derived
            # state instead; this update is preserved for non-DL026HA fallback.
            if self.data is not None and self.data.get("status") is not None:
                new_status = {
                    **self.data["status"],
                    STATUS_LOCK_MOTOR_STATE: bool(open_lock),
                }
                self.async_set_updated_data({**self.data, "status": new_status})

            # Record the action so the derived-state lock entity flips
            # immediately rather than waiting for a status poll.
            if open_lock:
                self._record_unlock_event("ha_door_operate")
            else:
                self._record_lock_event("ha_door_operate")

            await self.async_watch_lock_state()
            return True

        except aiohttp.ClientError as err:
            _LOGGER.error("[SmartLock] Network error: %s", err)
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("[SmartLock] Unexpected error: %s", err)
            return False

    # Convenience aliases for the lock entity.
    async def async_unlock_door(self) -> bool:
        return await self.async_smart_lock_door_operate(open_lock=True)

    async def async_lock_door(self) -> bool:
        """Lock the door.

        Uses the /commands endpoint with automatic_lock=true rather than
        door-operate(open=false). Both physically lock the door, but only
        automatic_lock=true keeps the cloud's lock_motor_state register in
        sync with reality on DL026HA firmware. Door-operate(open=false)
        leaves motor_state stuck at the previous value, which makes the
        Tuya app and the device's state machine drift out of sync until
        the user toggles passage mode in the app to force a realign. We
        avoid that by using the firmware's native lock command directly.

        Side effect: if passage mode is currently active, this also exits
        it (the write is the same as async_exit_passage_mode's relock).
        """
        if not self._cloud_enabled:
            _LOGGER.error(
                "[SmartLock] Lock requires cloud credentials — the "
                "/commands endpoint is cloud-only."
            )
            return False

        ok = await self._cloud_send_command(
            [{"code": STATUS_AUTOMATIC_LOCK, "value": True}]
        )
        if not ok:
            _LOGGER.error("[SmartLock] async_lock_door: /commands rejected")
            return False

        # If the lock entity was used while passage mode was on, the same
        # write also exited passage mode — keep our internal flag in sync.
        if self._passage_mode_active:
            _LOGGER.info(
                "[SmartLock] Lock entity used during passage mode — "
                "exiting passage mode to match"
            )
            self._passage_mode_active = False
            if self._passage_saved_auto_lock is not None:
                # Restore the user's auto_lock_time too so the timer is
                # back to normal after this implicit passage-mode exit.
                await self._cloud_send_command(
                    [{"code": STATUS_AUTO_LOCK_TIME,
                      "value": self._passage_saved_auto_lock}]
                )
                self._passage_saved_auto_lock = None

        # Optimistically reflect the commanded motor state for any non-
        # DL026HA fallback consumers; the DL026HA derived-state lock entity
        # uses _record_lock_event below.
        if self.data is not None and self.data.get("status") is not None:
            new_status = {
                **self.data["status"],
                STATUS_LOCK_MOTOR_STATE: False,  # firmware: false = locked
            }
            self.async_set_updated_data({**self.data, "status": new_status})

        self._record_lock_event("ha_lock_action")
        await self.async_watch_lock_state()
        return True

    # ------------------------------------------------------------------
    # State watch — burst-poll cloud status after a door-operate call
    # ------------------------------------------------------------------

    async def async_watch_lock_state(
        self,
        duration: float = STATE_WATCH_DURATION,
        interval: float = STATE_WATCH_INTERVAL,
    ) -> None:
        if not self._cloud_enabled:
            return

        self._state_watch_until = max(
            self._state_watch_until, time.time() + duration
        )
        if self._state_watch_task and not self._state_watch_task.done():
            return

        async def _watch() -> None:
            # Firmware semantic: motor_state True = unlocked, False = locked.
            last_state: Any = None
            if self.data:
                last_state = self.data.get("status", {}).get(STATUS_LOCK_MOTOR_STATE)
            saw_unlocked = last_state is True
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
                            if current is True:
                                saw_unlocked = True
                            if saw_unlocked and current is False:
                                return
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.debug("[TuyaWatch] Poll error: %s", err)
                        await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            finally:
                self._state_watch_task = None

        self._state_watch_task = self.hass.async_create_task(
            _watch(), name="tuya_lock_v2_state_watch"
        )

    # ------------------------------------------------------------------
    # Command helper — local first (if reachable), cloud fallback
    # ------------------------------------------------------------------

    # DPs we refuse to write over — they are read-only status codes and
    # writing to them has been observed to cause unintended behaviour.
    # NOTE: STATUS_AUTOMATIC_LOCK is intentionally NOT in this set even though
    # the v1 integration treated it as read-only. Diagnostic testing on
    # DL026HA firmware (v2.2.1 try_dp_write probe) confirmed it IS writable
    # and that the DP is mis-named: writing true puts the door into stay-
    # unlocked / passage mode, writing false relocks. async_enter_passage_mode
    # / async_exit_passage_mode rely on this.
    _READ_ONLY_DPS: frozenset[str] = frozenset({
        STATUS_LOCK_MOTOR_STATE,
        "residual_electricity",
        "unlock_fingerprint",
        "unlock_password",
        "unlock_card",
        "unlock_ble",
        "unlock_phone_remote",
        "alarm_lock",
        "hijack",
        "doorbell",
        "lock_record",
        "record",
    })

    async def async_send_command(self, commands: list[dict]) -> bool:
        """Issue one or more DP writes.

        Commands targeting read-only DPs are filtered out with a warning.
        """
        safe_commands: list[dict] = []
        for cmd in commands:
            code = cmd.get("code")
            if code in self._READ_ONLY_DPS:
                _LOGGER.warning(
                    "[TuyaCmd] Refusing to write read-only DP '%s' (value=%s). "
                    "Use the door-operate API for motor state, or the cloud "
                    "for cloud-only flags.",
                    code, cmd.get("value"),
                )
                continue
            safe_commands.append(cmd)

        if not safe_commands:
            return False

        if self._local_ip and self._local_key and self._local_reachable:
            try:
                await self._local_send_command(safe_commands)
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
            _LOGGER.error(
                "[TuyaLocal] Cannot send command — device unreachable and no cloud configured"
            )
            return False

        return await self._cloud_send_command(safe_commands)

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
                    "[TuyaCmd] Cloud command failed %s: %s",
                    data.get("code"), data.get("msg"),
                )
                return False

            await self.async_request_refresh()
            return True

        except aiohttp.ClientError as err:
            _LOGGER.error("[TuyaCmd] Network error sending command: %s", err)
            return False

    # ------------------------------------------------------------------
    # Passage mode (real, via automatic_lock DP)
    # ------------------------------------------------------------------
    # The DL026HA firmware exposes `automatic_lock` as a writable Boolean
    # function with the obvious semantics — "should the lock auto-lock?":
    #
    #   * automatic_lock = false  → auto-lock OFF → passage mode ON,
    #                              motor unlocks and stays unlocked.
    #   * automatic_lock = true   → auto-lock ON → normal mode,
    #                              motor relocks immediately and the
    #                              auto-lock-time countdown resumes.
    #
    # (v2.3.0 had this inverted — the diagnostic probe's earlier reading
    # was wrong. Verified by cross-checking the Tuya app's passage-mode
    # toggle, which faithfully reflects the DP value.)
    #
    # We still bump auto_lock_time to its max (1800 s) when entering passage
    # mode so that, if HA crashes or the integration is unloaded uncleanly
    # before async_shutdown runs, the lock will physically re-engage after
    # half an hour rather than stay open indefinitely.

    async def async_enter_passage_mode(self) -> bool:
        """Open the door and hold it open via automatic_lock=false.

        Returns True on success, False if the firmware rejected the write.
        """
        if self._passage_mode_active:
            return True
        if not self._cloud_enabled:
            _LOGGER.error(
                "[PassageV2] Passage mode requires cloud credentials — "
                "the writable DP is only reachable via the IoT Platform."
            )
            return False

        # Capture the current auto_lock_time so we can restore it on exit.
        # If it's already at the max, that almost certainly means a previous
        # passage-mode run never restored it (HA crashed, or restart while
        # passage was on). Fall back to AUTO_LOCK_TIME_DEFAULT in that case
        # so we don't lock the user into permanently-1800 after toggling.
        current_status = (self.data or {}).get("status", {}) or {}
        saved_raw = current_status.get(STATUS_AUTO_LOCK_TIME)
        try:
            saved_int = int(saved_raw) if saved_raw is not None else None
        except (TypeError, ValueError):
            saved_int = None
        if saved_int == PASSAGE_MODE_MAX_AUTO_LOCK:
            _LOGGER.warning(
                "[PassageV2] auto_lock_time was already %d s — assuming a "
                "previous passage-mode run never restored it. Will restore "
                "to %d s on exit instead.",
                saved_int, AUTO_LOCK_TIME_DEFAULT,
            )
            self._passage_saved_auto_lock = AUTO_LOCK_TIME_DEFAULT
        else:
            self._passage_saved_auto_lock = saved_int

        # Bump auto_lock_time to the maximum as a hardware-level backstop.
        # If HA crashes mid-passage-mode, the lock will at least re-engage
        # after this timer fires rather than stay open indefinitely.
        # Passage-mode writes go straight to /commands rather than through
        # async_send_command — for BLE sub-devices the local tinytuya path
        # silently swallows these writes (the gateway accepts them but
        # doesn't propagate to the lock), which produced the "switch toggles
        # but nothing happens" symptom in v2.3.0.
        await self._cloud_send_command(
            [{"code": STATUS_AUTO_LOCK_TIME, "value": PASSAGE_MODE_MAX_AUTO_LOCK}]
        )

        # The actual passage-mode toggle.
        ok = await self._cloud_send_command(
            [{"code": STATUS_AUTOMATIC_LOCK, "value": False}]
        )
        if not ok:
            _LOGGER.error(
                "[PassageV2] Firmware rejected automatic_lock=false — "
                "aborting and restoring auto_lock_time"
            )
            if self._passage_saved_auto_lock is not None:
                await self._cloud_send_command(
                    [{"code": STATUS_AUTO_LOCK_TIME, "value": self._passage_saved_auto_lock}]
                )
            self._passage_saved_auto_lock = None
            return False

        self._passage_mode_active = True
        _LOGGER.info(
            "[PassageV2] Passage mode ON (saved auto_lock_time=%s s, "
            "30-min hardware backstop armed)",
            self._passage_saved_auto_lock,
        )

        # Force a state push so the switch and lock entity reflect the
        # new mode immediately, without waiting for the next poll.
        if self.data is not None:
            self.async_set_updated_data(self.data)
        return True

    async def async_exit_passage_mode(self, relock: bool = True) -> bool:
        """Close out passage mode and restore the saved auto_lock_time.

        ``relock`` is preserved for API symmetry but writing
        automatic_lock=false already relocks the door, so passing False
        only suppresses that single command.
        """
        if not self._passage_mode_active:
            return True

        self._passage_mode_active = False

        if relock:
            ok = await self._cloud_send_command(
                [{"code": STATUS_AUTOMATIC_LOCK, "value": True}]
            )
            if not ok:
                _LOGGER.warning(
                    "[PassageV2] automatic_lock=true write failed — "
                    "lock will still relock when auto_lock_time expires"
                )
            # Clear the recent-unlock state so the entity flips back to
            # Locked rather than reporting Unlocked from a pre-passage event.
            self._record_lock_event("passage_mode_exit")

        # Restore the user's previous auto_lock_time so normal behaviour
        # resumes (rather than leaving the 30-minute backstop in place).
        if self._passage_saved_auto_lock is not None:
            await self._cloud_send_command(
                [{"code": STATUS_AUTO_LOCK_TIME, "value": self._passage_saved_auto_lock}]
            )
            self._passage_saved_auto_lock = None

        _LOGGER.info("[PassageV2] Passage mode OFF")

        if self.data is not None:
            self.async_set_updated_data(self.data)
        return True

    async def async_shutdown(self) -> None:
        """Best-effort safety hook called on entry unload / HA stop.

        If passage mode is currently active, write automatic_lock=false so
        the door doesn't stay open after HA goes away. The 30-minute
        auto_lock_time backstop set by async_enter_passage_mode covers
        the case where this call also fails (e.g. a hard crash).
        """
        if not self._passage_mode_active:
            return
        if not self._cloud_enabled:
            return
        try:
            await self._cloud_send_command(
                [{"code": STATUS_AUTOMATIC_LOCK, "value": True}]
            )
            _LOGGER.warning(
                "[PassageV2] Shutdown: relocked door (passage mode was active)"
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("[PassageV2] Shutdown relock failed: %s", err)
