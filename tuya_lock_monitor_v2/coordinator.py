"""Tuya Lock Monitor v2 coordinator.

Differences from v1:
  * Real passage mode via the writable `automatic_lock` DP:
    false → auto-lock off → door stays unlocked (passage mode),
    true → auto-lock on → door relocks immediately. See the passage-mode
    section below for the mechanics and the 30-minute safety backstop.
  * Exposes `async_lock_door()` / `async_unlock_door()` helpers so the lock
    entity never has to reason about which API to hit.
  * Derived lock state (recent-unlock window) instead of trusting
    `lock_motor_state`, which only tracks cloud door-operate commands.
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
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
        entry_id: str | None = None,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        self._entry_id = entry_id
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

        # Persistent tinytuya Device, reused across polls instead of a new
        # TCP connection per poll. Guarded by _local_lock (tinytuya isn't
        # thread-safe and status polls / command sends can overlap).
        self._local_device: Any | None = None
        self._local_lock: asyncio.Lock = asyncio.Lock()

        # Burst-poll state (used after smart-lock door-operate).
        self._state_watch_task: asyncio.Task | None = None
        self._state_watch_until: float = 0.0
        self._watch_motor: bool = False

        # Auto-reset handles for edge-triggered DPs.
        self._doorbell_reset_unsub: object | None = None
        self._unlock_reset_unsubs: dict[str, object] = {}
        self._relock_refresh_unsub: object | None = None

        # Last-seen user event per unlock kind (survives the DP's auto-zero).
        # Maps status_key → {"id": int, "time": datetime}.
        self._last_user_event: dict[str, dict[str, Any]] = {}

        # Passage-mode state. Whether passage mode is active is NOT tracked
        # here — it is read from the lock's own `automatic_lock` DP (see
        # passage_mode_active), so it survives HA restarts and reflects
        # changes made in the Tuya app. We only keep the previous
        # auto_lock_time so it can be restored on exit (reverting the 1800 s
        # safety cap).
        self._passage_saved_auto_lock: int | None = None

        # Serialises multi-step command sequences (enter/exit passage mode,
        # lock, unlock) so two callers can't interleave writes to one lock.
        self._cmd_lock: asyncio.Lock = asyncio.Lock()

        # DP writes the cloud has accepted but the lock hasn't reported back
        # yet. Maps status code → (commanded value, monotonic deadline).
        # "success" from the cloud only means the command was queued for the
        # gateway; a BLE lock applies it seconds later (or never). Until the
        # lock confirms, the commanded value is overlaid on polled status so
        # the UI doesn't bounce; if the deadline passes unconfirmed we log it
        # and let the lock's reported value win. See _apply_expectations.
        self._expected: dict[str, tuple[Any, float]] = {}

        # Set when cloud metadata shows this device is a gateway sub-device
        # (e.g. a BLE lock behind an SG120HA). Local polling is refused for
        # those — see _refresh_cloud_meta.
        self._is_sub_device: bool = False

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

        # Passed explicitly (None during config-flow validation) rather than
        # left to HA's ContextVar lookup: with an entry, HA calls
        # async_shutdown on unload for us.
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
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
        """True when the lock itself reports auto-lock disabled.

        Read from the `automatic_lock` DP (with any pending commanded value
        overlaid) rather than an in-memory flag: a flag is lost on restart,
        after which turn_off used to no-op while the door stayed open.
        """
        status = (self.data or {}).get("status") or {}
        return status.get(STATUS_AUTOMATIC_LOCK) is False

    @property
    def is_sub_device(self) -> bool:
        return self._is_sub_device

    @property
    def local_polling_enabled(self) -> bool:
        """Whether the tinytuya ping loop should run for this entry."""
        return bool(self._local_ip) and not self._is_sub_device

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

    def _dispatch_updated_data(self, data: dict[str, Any]) -> None:
        """async_set_updated_data, safe to call from any thread.

        Listener notification must happen on the event loop; if a code path
        ends up here from an executor thread, marshal the call across.
        """
        try:
            on_loop = asyncio.get_running_loop() is self.hass.loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self.async_set_updated_data(data)
        else:
            self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, data)

    @callback
    def _publish_local_data(self, data: dict[str, Any]) -> None:
        """Push ping-loop data to listeners WITHOUT touching the poll timer.

        async_set_updated_data() cancels the scheduled refresh (and any
        debounced async_request_refresh) and re-arms it a full
        update_interval away. Called from a 1 Hz loop that means the cloud
        poll never fires at all: cloud-sourced DPs froze at whatever the
        last command's immediate refresh returned. So the ping loop sets the
        data itself, and only wakes listeners when something changed.
        """
        if data == self.data:
            return
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()

    # ------------------------------------------------------------------
    # Command confirmation
    # ------------------------------------------------------------------

    # How long the lock gets to report a commanded DP back before we call
    # the write lost. BLE locks behind a gateway typically confirm in 3-15 s.
    _CONFIRM_TIMEOUT: float = 45.0

    def _expect(self, code: str, value: Any) -> None:
        """Note that `code` should read `value` once the lock applies it."""
        self._expected[code] = (value, time.monotonic() + self._CONFIRM_TIMEOUT)

    def _apply_expectations(
        self, status: dict[str, Any], fresh: dict[str, Any] | None = None
    ) -> None:
        """Overlay commanded-but-unconfirmed DP values onto polled status.

        `fresh` is what was JUST read from the lock's side (defaults to
        `status`). Only that may confirm an expectation: `status` is often
        built on top of self.data, which already carries our own overlay,
        and an overlay must never confirm itself.
        """
        if fresh is None:
            fresh = status
        now = time.monotonic()
        for code, (value, deadline) in list(self._expected.items()):
            actual = fresh.get(code)
            if code in fresh and actual == value:
                del self._expected[code]
                _LOGGER.debug("[Confirm] %s=%s confirmed by the lock", code, value)
            elif now >= deadline:
                del self._expected[code]
                _LOGGER.warning(
                    "[Confirm] %s=%s was accepted by the Tuya cloud but the "
                    "lock still reports %s after %.0f s — the command "
                    "probably never reached it over Bluetooth",
                    code, value, actual, self._CONFIRM_TIMEOUT,
                )
                if code in fresh:
                    status[code] = actual
            else:
                status[code] = value

    def _push_expected(self) -> None:
        """Reflect freshly-commanded values in entity state right away."""
        if self.data is None or self.data.get("status") is None:
            return
        # Overlay only — nothing here was read from the lock, so nothing
        # here can confirm (or expire) an expectation.
        status = dict(self.data["status"])
        for code, (value, _deadline) in self._expected.items():
            status[code] = value
        self._dispatch_updated_data({**self.data, "status": status})

    def _record_unlock_event(self, source: str) -> None:
        """Mark 'now' as the last observed unlock event."""
        self._last_unlock_at = dt_util.utcnow()
        _LOGGER.info(
            "[TuyaUnlock] Detected unlock from %s at %s",
            source, self._last_unlock_at.isoformat(),
        )
        self._schedule_relock_refresh()
        self.async_update_listeners()

    def _record_lock_event(self, source: str) -> None:
        """Clear the recent-unlock state (deliberate lock action)."""
        if self._last_unlock_at is not None:
            _LOGGER.info(
                "[TuyaUnlock] Cleared recent-unlock state from %s",
                source,
            )
        self._last_unlock_at = None
        self._cancel_relock_refresh()
        self.async_update_listeners()

    # The lock entity shows Unlocked for auto_lock_time + 5 s after
    # _last_unlock_at. That is derived from the clock, not from data, so
    # nothing tells HA when the window closes — and the next poll may be
    # minutes away. Re-render entities ourselves just after it does.
    _RELOCK_WINDOW_GRACE: int = 6

    def _schedule_relock_refresh(self) -> None:
        self._cancel_relock_refresh()
        status = (self.data or {}).get("status") or {}
        try:
            secs = int(status.get(STATUS_AUTO_LOCK_TIME, AUTO_LOCK_TIME_DEFAULT))
        except (TypeError, ValueError):
            secs = AUTO_LOCK_TIME_DEFAULT
        self._relock_refresh_unsub = async_call_later(
            self.hass, max(secs, 1) + self._RELOCK_WINDOW_GRACE,
            self._async_relock_refresh,
        )

    def _cancel_relock_refresh(self) -> None:
        if self._relock_refresh_unsub is not None:
            self._relock_refresh_unsub()  # type: ignore[operator]
            self._relock_refresh_unsub = None

    @callback
    def _async_relock_refresh(self, _now: object = None) -> None:
        self._relock_refresh_unsub = None
        self.async_update_listeners()

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
        # A background task: a plain async_create_task made HA bootstrap
        # ("Setup timed out for bootstrap waiting on tuya_lock_v2_ping") and
        # shutdown wait minutes for a loop that never returns.
        self._ping_task = self.hass.async_create_background_task(
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
        # Pending 1 s auto-resets of edge-triggered DPs.
        if self._doorbell_reset_unsub is not None:
            self._doorbell_reset_unsub()  # type: ignore[operator]
            self._doorbell_reset_unsub = None
        for unsub in self._unlock_reset_unsubs.values():
            unsub()  # type: ignore[operator]
        self._unlock_reset_unsubs.clear()
        self._cancel_relock_refresh()
        self._drop_local_device()

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
                        fresh=status,
                    )
                    self._last_contact = dt_util.utcnow()
                    self._publish_local_data(result)
                    _LOGGER.debug("[TuyaPing] Local poll OK")
                except asyncio.CancelledError:
                    return
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("[TuyaPing] Reachable but push failed: %s", err)

            await asyncio.sleep(PING_INTERVAL)

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

    def _forget_token_if_rejected(self, data: dict[str, Any]) -> None:
        """Drop the cached token when the cloud says it's invalid/expired.

        Otherwise every call keeps failing until our own 2-hour expiry.
        """
        if data.get("code") in self._TOKEN_ERROR_CODES:
            self._token = None

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
            self._forget_token_if_rejected(data)
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
            self._forget_token_if_rejected(data)
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
        session = async_get_clientsession(self.hass)
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

    def _get_local_device(self) -> Any:
        """Return the cached persistent tinytuya Device (create on demand).

        Must be called from an executor thread while _local_lock is held.
        """
        import tinytuya  # noqa: PLC0415

        d = self._local_device
        if d is None:
            d = tinytuya.Device(
                dev_id=self._device_id,
                address=self._local_ip,
                local_key=self._local_key,
                version=self._local_version,
                connection_timeout=0.3,
                connection_retry_limit=1,
                connection_retry_delay=0,
            )
            d.set_socketPersistent(True)
            self._local_device = d
        return d

    def _drop_local_device(self) -> None:
        """Forget the cached Device so the next call reconnects fresh."""
        d = self._local_device
        self._local_device = None
        if d is not None:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass

    async def _local_get_status(self) -> dict[str, Any]:
        def _sync_fetch() -> dict:
            return self._get_local_device().status()

        async with self._local_lock:
            try:
                result: dict = await self.hass.async_add_executor_job(_sync_fetch)
            except Exception:
                self._drop_local_device()
                raise

            if not result or "Error" in result:
                self._drop_local_device()
                raise RuntimeError(
                    f"tinytuya error: {result.get('Error', result) if result else 'no response'}"
                )

        dps: dict = result.get("dps", {})
        status = {DPS_TO_CODE[str(k)]: v for k, v in dps.items() if str(k) in DPS_TO_CODE}
        return status

    async def _local_send_command(self, commands: list[dict]) -> None:
        def _sync_send() -> None:
            d = self._get_local_device()
            d.set_socketTimeout(5)
            try:
                for cmd in commands:
                    dp = CODE_TO_DPS.get(cmd["code"])
                    if dp is not None:
                        d.set_value(dp, cmd["value"])
            finally:
                # Restore the short status-poll timeout.
                d.set_socketTimeout(0.3)

        async with self._local_lock:
            try:
                await self.hass.async_add_executor_job(_sync_send)
            except Exception:
                self._drop_local_device()
                raise

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
            self._dispatch_updated_data({**self.data, "status": new_status})

    def _schedule_unlock_reset(self, key: str) -> None:
        old = self._unlock_reset_unsubs.pop(key, None)
        if old is not None:
            old()  # type: ignore[operator]
        # The lambda must be marked as a callback: async_call_later infers
        # the job type from the callable it's given, and an unmarked lambda
        # is dispatched to the executor thread pool — where the state write
        # inside _async_clear_unlock is not allowed.
        self._unlock_reset_unsubs[key] = async_call_later(
            self.hass, 1, callback(lambda _now, k=key: self._async_clear_unlock(k))
        )

    @callback
    def _async_clear_unlock(self, key: str) -> None:
        self._unlock_reset_unsubs.pop(key, None)
        if self.data and self.data.get("status", {}).get(key):
            new_status = {**self.data["status"], key: 0}
            self._dispatch_updated_data({**self.data, "status": new_status})

    # ------------------------------------------------------------------

    def _build_result(
        self,
        status: dict[str, Any],
        mode: str,
        fresh: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Hold commanded-but-unconfirmed values over whatever was just polled
        # (the cloud's /status lags a BLE lock by seconds), and notice writes
        # the lock never confirmed. `fresh` = the part of `status` that was
        # just read, when `status` is a merge over older data.
        self._apply_expectations(status, fresh)

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
            # A user ID is an int. A bool here means we're decoding some
            # other device's DP (int(True) == 1 turned a gateway socket's
            # relay state into an endless "fingerprint #1 unlocked" storm).
            if isinstance(new_raw, bool):
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
            event_time = dt_util.utcnow()
            self._last_user_event[key] = {
                "id": new_id,
                "time": event_time,
            }
            self.hass.bus.async_fire(
                EVENT_UNLOCK,
                {
                    "entry_id": self._entry_id,
                    "device_id": self._device_id,
                    "device_name": self._cached_meta.get("name", "Tuya Lock"),
                    "kind": key,  # unlock_fingerprint / unlock_password / unlock_card
                    "id": new_id,
                    "time": event_time.isoformat(),
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
        self._ingest_meta(await self._cloud_device_info(session, token))

    async def _cloud_poll(
        self, session: aiohttp.ClientSession, token: str
    ) -> dict[str, Any]:
        """Metadata AND status in one API call.

        GET /v1.0/devices/{id} embeds the full status array, so polling it
        costs half of /status plus a periodic metadata refresh — it matters,
        see UPDATE_INTERVAL — and keeps `online` current on every poll.
        """
        info = await self._cloud_device_info(session, token)
        self._ingest_meta(info)
        embedded = info.get("status")
        if isinstance(embedded, list) and embedded:
            return {item["code"]: item["value"] for item in embedded}
        return await self._cloud_device_status(session, token)

    def _ingest_meta(self, info: dict[str, Any]) -> None:
        self._cached_meta = {k: v for k, v in info.items() if k != "status"}
        self._last_meta_refresh = time.time()

        # A gateway sub-device (e.g. a BLE DL026HA behind an SG120HA) has no
        # LAN presence of its own. Any "local IP" given for it is really the
        # gateway's, and polling that is actively harmful: the gateway
        # answers with its OWN DPs plus relayed reports from EVERY child
        # lock, numbered per the child's schema — none of which matches
        # DPS_TO_CODE (a DL031HA Wi-Fi table) or is routed by cid. Observed:
        # fingerprint IDs shown as battery %, battery % shown as the last
        # alarm, both locks mirroring each other's events, and the
        # gateway's relay state read as a fingerprint unlock every second.
        if info.get("sub") and not self._is_sub_device:
            self._is_sub_device = True
            if self._local_ip:
                _LOGGER.warning(
                    "[TuyaLocal] %s is a gateway sub-device (node_id=%s): "
                    "ignoring local IP %s, which is the gateway's. Local "
                    "polling is only valid for locks with their own Wi-Fi. "
                    "Running cloud-only.",
                    info.get("name", self._device_id),
                    info.get("node_id"),
                    self._local_ip,
                )

        if info.get("local_key"):
            if self._local_key != info["local_key"]:
                # Key rotated — the cached persistent device is now stale.
                self._drop_local_device()
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

        if self.local_polling_enabled:
            # Cloud + local mode. We always poll cloud /status here — even
            # when local is reachable — so externally-triggered events
            # (Tuya app unlocks, fingerprint scans, the auto-lock timer
            # firing) are reflected in HA. The local ping loop runs in
            # parallel at sub-second cadence and is responsible for the
            # local-only / push-only DPs (unlock_fingerprint pulses etc.).
            cloud_status: dict[str, Any] | None = None
            cloud_err: Exception | None = None
            try:
                session = async_get_clientsession(self.hass)
                if need_meta:
                    await self._refresh_cloud_meta(session)
                token = await self._get_token(session)
                cloud_status = await self._cloud_device_status(session, token)
                await self._seed_missing_state(session, token, cloud_status)
            except Exception as err:  # noqa: BLE001
                cloud_err = err

            if cloud_status is not None:
                # Layer local-only DPs on top of the cloud snapshot.
                if self.data:
                    local_status = self.data.get("status", {})
                    for k in self._LOCAL_ONLY_KEYS:
                        if k in local_status:
                            cloud_status[k] = local_status[k]
                        else:
                            cloud_status.pop(k, None)
                mode = "cloud+local" if self._local_reachable else "cloud_fallback"
                return self._build_result(cloud_status, mode)

            # Cloud failed. Three fallback strategies, in order:
            #   1. We have cached data — keep using it, just log the failure.
            #   2. Local is reachable — do a fresh local poll and run in
            #      local-only-degraded mode (no passage mode, no door-operate,
            #      no external-event detection, but the device is usable).
            #   3. Neither — raise UpdateFailed so HA shows the error.
            if self.data:
                _LOGGER.warning(
                    "[TuyaCloud] Scheduled cloud poll failed — keeping stale data: %s",
                    cloud_err,
                )
                return self.data

            if self._local_reachable or self._local_key:
                _LOGGER.warning(
                    "[TuyaCloud] First-refresh cloud poll failed (%s) — "
                    "falling back to local-only mode. Cloud-dependent "
                    "features (passage mode, remote unlock/lock) will be "
                    "unavailable until cloud is restored.",
                    cloud_err,
                )
                try:
                    status = await self._local_get_status()
                    self._last_local_poll = now
                    self._last_contact = dt_util.utcnow()
                    return self._build_result(status, "local_degraded")
                except Exception as local_err:  # noqa: BLE001
                    raise UpdateFailed(
                        f"Cloud failed ({cloud_err}); local fallback also "
                        f"failed ({local_err})"
                    ) from local_err

            raise UpdateFailed(
                f"Cloud poll failed and no cached data: {cloud_err}"
            ) from cloud_err

        try:
            session = async_get_clientsession(self.hass)
            token = await self._get_token(session)
            status = await self._cloud_poll(session, token)
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
    # Cloud writes — signed POST with retry
    # ------------------------------------------------------------------

    # Pauses before the 2nd and 3rd attempt at a cloud write.
    _RETRY_DELAYS: tuple[float, ...] = (0.7, 2.0)
    # Token invalid / expired: drop the cached token and ask again.
    _TOKEN_ERROR_CODES: frozenset[int] = frozenset({1010, 1011})
    # "device is offline": the gateway has lost the lock, or the cloud has
    # lost the gateway. Retrying within seconds doesn't help — fail fast.
    _OFFLINE_CODE: int = 2001
    # Per-attempt ceiling. Commands are serialised by _cmd_lock, so a hung
    # request would otherwise block every later command for this lock.
    _POST_TIMEOUT: float = 15.0

    # Why the most recent cloud write failed, phrased for a UI error toast.
    last_command_error: str | None = None

    async def _cloud_post(
        self, path: str, body: str | None, what: str
    ) -> dict[str, Any] | None:
        """Signed POST to the Tuya OpenAPI, retrying transient failures.

        Returns the decoded response on success, or None once retries are
        spent (the reason is left in last_command_error). Every write this
        integration makes is idempotent, so repeating one whose response
        got lost is safe.
        """
        session = async_get_clientsession(self.hass)
        attempts = len(self._RETRY_DELAYS) + 1
        err = "unknown error"
        offline = False
        for attempt in range(attempts):
            try:
                token = await self._get_token(session)
                ts = str(int(time.time() * 1000))
                nonce = uuid.uuid4().hex
                sign = self._sign(ts, nonce, "POST", path, token, body or "")
                headers = self._base_headers(ts, nonce, sign, token)
                headers["Content-Type"] = "application/json"
                async with asyncio.timeout(self._POST_TIMEOUT):
                    async with session.post(
                        self._endpoint + path, headers=headers, data=body
                    ) as resp:
                        http_status = resp.status
                        raw = await resp.text()
                _LOGGER.debug(
                    "[TuyaCmd] %s → HTTP %d %s", what, http_status, raw
                )
                data = json.loads(raw)
                if data.get("success"):
                    self.last_command_error = None
                    return data
                code = data.get("code")
                err = f"{code}: {data.get('msg', 'unknown')}"
                self._forget_token_if_rejected(data)
                if code == self._OFFLINE_CODE:
                    offline = True
                    break
            except TimeoutError:
                err = f"no answer from the Tuya cloud in {self._POST_TIMEOUT:.0f} s"
            except aiohttp.ClientError as exc:
                err = f"network error: {exc!r}"
            except (json.JSONDecodeError, UpdateFailed) as exc:
                err = str(exc)

            if attempt < attempts - 1:
                _LOGGER.warning(
                    "[TuyaCmd] Cloud %s failed (%s) — retrying (%d/%d)",
                    what, err, attempt + 1, attempts - 1,
                )
                await asyncio.sleep(self._RETRY_DELAYS[attempt])

        if offline:
            self.last_command_error = (
                "The lock is offline — its Bluetooth hub can't reach it "
                "right now."
            )
        else:
            self.last_command_error = f"Tuya cloud refused the {what} ({err})."
        _LOGGER.error("[TuyaCmd] Cloud %s failed %s", what, err)
        return None

    # ------------------------------------------------------------------
    # Smart Lock cloud API — ticket-based door operate (DL026HA family)
    # ------------------------------------------------------------------

    async def async_smart_lock_door_operate(self, open_lock: bool) -> bool:
        """POST /password-free/door-operate with open=true|false.

        open_lock=True  → remote unlock.
        open_lock=False → remote lock (re-engage the latch immediately).
        Returns True on API success, False otherwise (the reason is left in
        last_command_error).
        """
        what = "unlock" if open_lock else "lock"
        if not self._cloud_enabled:
            self.last_command_error = (
                "Remote unlock/lock needs Tuya cloud credentials."
            )
            _LOGGER.error(
                "[SmartLock] Remote unlock/lock requires cloud credentials — "
                "the Smart Lock API is cloud-only."
            )
            return False

        async with self._cmd_lock:
            ticket = await self._cloud_post(
                SMART_LOCK_TICKET_PATH.format(device_id=self._device_id),
                None,
                f"{what} ticket",
            )
            if ticket is None:
                return False
            ticket_id = (ticket.get("result") or {}).get("ticket_id")
            if not ticket_id:
                self.last_command_error = "Tuya cloud returned no unlock ticket."
                _LOGGER.error(
                    "[SmartLock] ticket response missing ticket_id: %s",
                    ticket.get("result"),
                )
                return False

            body = json.dumps({"ticket_id": ticket_id, "open": bool(open_lock)})
            operated = await self._cloud_post(
                SMART_LOCK_DOOR_OPERATE_PATH.format(device_id=self._device_id),
                body,
                what,
            )
            if operated is None:
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
            self._dispatch_updated_data({**self.data, "status": new_status})

        # Record the action so the derived-state lock entity flips
        # immediately rather than waiting for a status poll.
        if open_lock:
            self._record_unlock_event("ha_door_operate")
        else:
            self._record_lock_event("ha_door_operate")

        await self.async_watch_lock_state(watch_motor=True)
        return True

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
            self.last_command_error = "Locking needs Tuya cloud credentials."
            _LOGGER.error(
                "[SmartLock] Lock requires cloud credentials — the "
                "/commands endpoint is cloud-only."
            )
            return False

        async with self._cmd_lock:
            # Read before the write: the write flips passage_mode_active.
            was_passage = self.passage_mode_active

            ok = await self._cloud_send_command(
                [{"code": STATUS_AUTOMATIC_LOCK, "value": True}]
            )
            if not ok:
                return False

            # Used while passage mode was on, the same write also exited
            # passage mode — put the user's auto_lock_time back too.
            if was_passage:
                _LOGGER.info(
                    "[SmartLock] Lock entity used during passage mode — "
                    "exiting passage mode to match"
                )
                await self._restore_auto_lock_time()

        # Optimistically reflect the commanded motor state for any non-
        # DL026HA fallback consumers; the DL026HA derived-state lock entity
        # uses _record_lock_event below.
        if self.data is not None and self.data.get("status") is not None:
            new_status = {
                **self.data["status"],
                STATUS_LOCK_MOTOR_STATE: False,  # firmware: false = locked
            }
            self._dispatch_updated_data({**self.data, "status": new_status})

        self._record_lock_event("ha_lock_action")
        return True

    # ------------------------------------------------------------------
    # State watch — burst-poll cloud status after a command
    # ------------------------------------------------------------------

    async def async_watch_lock_state(
        self,
        duration: float | None = None,
        interval: float | None = None,
        watch_motor: bool = False,
    ) -> None:
        """Poll cloud /status every `interval` s for up to `duration` s.

        Ends early once every commanded DP has been confirmed by the lock
        and — when watch_motor is set (door-operate) — the motor has been
        seen to unlock and relock. One task per lock: a second call while
        it runs just extends the deadline.
        """
        if not self._cloud_enabled:
            return
        if duration is None:
            duration = STATE_WATCH_DURATION
        if interval is None:
            interval = STATE_WATCH_INTERVAL

        self._state_watch_until = max(
            self._state_watch_until, time.time() + duration
        )
        self._watch_motor = self._watch_motor or watch_motor
        if self._state_watch_task and not self._state_watch_task.done():
            return

        async def _watch() -> None:
            # Firmware semantic: motor_state True = unlocked, False = locked.
            last_state: Any = None
            if self.data:
                last_state = self.data.get("status", {}).get(STATUS_LOCK_MOTOR_STATE)
            saw_unlocked = last_state is True
            try:
                session = async_get_clientsession(self.hass)
                while time.time() < self._state_watch_until:
                    try:
                        token = await self._get_token(session)
                        cloud_status = await self._cloud_device_status(
                            session, token
                        )
                        current = cloud_status.get(STATUS_LOCK_MOTOR_STATE)
                        if self.data is not None:
                            local_status = self.data.get("status", {})
                            merged = {**local_status, **cloud_status}
                            for k in self._LOCAL_ONLY_KEYS:
                                if k in local_status:
                                    merged[k] = local_status[k]
                            self._apply_expectations(merged, fresh=cloud_status)
                            self._dispatch_updated_data(
                                {**self.data, "status": merged}
                            )

                        if current is True:
                            saw_unlocked = True
                        motor_done = not self._watch_motor or (
                            saw_unlocked and current is False
                        )
                        if motor_done and not self._expected:
                            return
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("[TuyaWatch] Poll error: %s", err)
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            finally:
                self._state_watch_task = None
                self._watch_motor = False

        # Background: must not hold up HA shutdown for the rest of a burst.
        self._state_watch_task = self.hass.async_create_background_task(
            _watch(), name="tuya_lock_v2_state_watch"
        )

    # ------------------------------------------------------------------
    # Command helper — local first (if reachable), cloud fallback
    # ------------------------------------------------------------------

    # DPs we refuse to write over — they are read-only status codes and
    # writing to them has been observed to cause unintended behaviour.
    # NOTE: STATUS_AUTOMATIC_LOCK is intentionally NOT in this set even though
    # the v1 integration treated it as read-only. Diagnostic testing on
    # DL026HA firmware (v2.2.1 try_dp_write probe) confirmed it IS writable,
    # with the obvious semantics: writing false disables auto-lock (stay-
    # unlocked / passage mode), writing true re-enables auto-lock and relocks.
    # async_enter_passage_mode / async_exit_passage_mode rely on this.
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
        if await self._cloud_post(path, body, "command") is None:
            return False

        # "success" only means the cloud queued the write for the gateway.
        # Show the commanded values now, then watch for the lock to confirm
        # them. Only DPs the lock actually reports can ever be confirmed, so
        # write-only DPs (and the diagnostic probe services) are left alone.
        reported = (self.data or {}).get("status") or {}
        for cmd in commands:
            if cmd.get("code") in reported:
                self._expect(cmd["code"], cmd.get("value"))
        if self._expected:
            self._push_expected()
            await self.async_watch_lock_state(duration=self._CONFIRM_TIMEOUT + 5)
        else:
            await self.async_request_refresh()
        return True

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

    async def _restore_auto_lock_time(self) -> None:
        """Undo the 1800 s passage-mode cap on auto_lock_time.

        Caller holds _cmd_lock.
        """
        saved = self._passage_saved_auto_lock
        self._passage_saved_auto_lock = None
        if saved is None:
            # We didn't set the cap this session (HA restarted while passage
            # mode was on, or it was started from the Tuya app). Only step
            # in if the cap is visibly still there.
            status = (self.data or {}).get("status") or {}
            try:
                current = int(status.get(STATUS_AUTO_LOCK_TIME))
            except (TypeError, ValueError):
                return
            if current != PASSAGE_MODE_MAX_AUTO_LOCK:
                return
            saved = AUTO_LOCK_TIME_DEFAULT
        await self._cloud_send_command(
            [{"code": STATUS_AUTO_LOCK_TIME, "value": saved}]
        )

    async def async_enter_passage_mode(self) -> bool:
        """Open the door and hold it open via automatic_lock=false.

        Returns True on success, False if the write failed (the reason is
        left in last_command_error).
        """
        if not self._cloud_enabled:
            self.last_command_error = "Passage mode needs Tuya cloud credentials."
            _LOGGER.error(
                "[PassageV2] Passage mode requires cloud credentials — "
                "the writable DP is only reachable via the IoT Platform."
            )
            return False

        async with self._cmd_lock:
            if self.passage_mode_active:
                return True

            # Capture the current auto_lock_time so we can restore it on
            # exit. If it's already at the max, that almost certainly means
            # a previous passage-mode run never restored it (HA crashed, or
            # restart while passage was on). Fall back to
            # AUTO_LOCK_TIME_DEFAULT in that case so we don't lock the user
            # into permanently-1800 after toggling.
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

            # Bump auto_lock_time to the maximum as a hardware-level
            # backstop. If HA crashes mid-passage-mode, the lock will at
            # least re-engage after this timer fires rather than stay open
            # indefinitely. Passage-mode writes go straight to /commands
            # rather than through async_send_command — for BLE sub-devices
            # the local tinytuya path silently swallows these writes (the
            # gateway accepts them but doesn't propagate to the lock).
            await self._cloud_send_command(
                [{"code": STATUS_AUTO_LOCK_TIME, "value": PASSAGE_MODE_MAX_AUTO_LOCK}]
            )

            # The actual passage-mode toggle.
            ok = await self._cloud_send_command(
                [{"code": STATUS_AUTOMATIC_LOCK, "value": False}]
            )
            if not ok:
                reason = self.last_command_error
                _LOGGER.error(
                    "[PassageV2] automatic_lock=false write failed — "
                    "aborting and restoring auto_lock_time"
                )
                await self._restore_auto_lock_time()
                self.last_command_error = reason
                return False

        _LOGGER.info(
            "[PassageV2] Passage mode ON (30-min hardware backstop armed)"
        )
        return True

    async def async_exit_passage_mode(self, relock: bool = True) -> bool:
        """Close out passage mode and restore the saved auto_lock_time.

        Decided from the lock's own `automatic_lock` DP, not from memory of
        what we last sent, so it still works after an HA restart. Returns
        False if the relock write failed — the door is then still in passage
        mode, and says so.

        ``relock=False`` only restores auto_lock_time.
        """
        async with self._cmd_lock:
            in_passage = self.passage_mode_active
            if in_passage and relock:
                ok = await self._cloud_send_command(
                    [{"code": STATUS_AUTOMATIC_LOCK, "value": True}]
                )
                if not ok:
                    _LOGGER.warning(
                        "[PassageV2] automatic_lock=true write failed — "
                        "the door is still in passage mode"
                    )
                    return False
                # Clear the recent-unlock state so the entity flips back to
                # Locked rather than reporting Unlocked from a pre-passage
                # event.
                self._record_lock_event("passage_mode_exit")

            # Restore the user's previous auto_lock_time so normal behaviour
            # resumes (rather than leaving the 30-minute backstop in place).
            reason = self.last_command_error
            await self._restore_auto_lock_time()
            self.last_command_error = reason

        if in_passage:
            _LOGGER.info("[PassageV2] Passage mode OFF")
        return True

    async def async_shutdown(self) -> None:
        """Relock on entry unload / HA stop, then shut the coordinator down.

        HA calls this itself when the entry unloads. If passage mode is
        active, write automatic_lock=true (auto-lock back on) so the door
        doesn't stay open after HA goes away. The 30-minute auto_lock_time
        backstop set by async_enter_passage_mode covers the case where this
        call also fails (e.g. a hard crash).
        """
        if self._cloud_enabled and self.passage_mode_active:
            try:
                async with self._cmd_lock:
                    ok = await self._cloud_send_command(
                        [{"code": STATUS_AUTOMATIC_LOCK, "value": True}]
                    )
                    await self._restore_auto_lock_time()
                if ok:
                    _LOGGER.warning(
                        "[PassageV2] Shutdown: relocked door (passage mode "
                        "was active)"
                    )
                else:
                    _LOGGER.error(
                        "[PassageV2] Shutdown relock failed: %s",
                        self.last_command_error,
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("[PassageV2] Shutdown relock failed: %s", err)
        self.async_stop_ping_loop()
        await super().async_shutdown()
