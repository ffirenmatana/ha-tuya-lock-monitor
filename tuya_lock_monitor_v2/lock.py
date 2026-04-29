"""Lock entity for Tuya Lock Monitor v2.

DL026HA (BLE sub-device of SG120HA gateway):
    * Unlock → Smart Lock door-operate with open=true.
    * Lock   → Smart Lock door-operate with open=false.
    * State is DERIVED, not read from lock_motor_state. The DP only tracks
      the most-recent cloud-API door-operate command — Tuya-app unlocks,
      fingerprint scans, and the auto-lock timer firing don't move it. We
      compute is_locked from automatic_lock + recent-unlock window instead.

DL031HA (fallback, kept for parity):
    * `normal_open_switch` toggle.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    AUTO_LOCK_TIME_DEFAULT,
    DOMAIN,
    STATUS_AUTO_LOCK_TIME,
    STATUS_AUTOMATIC_LOCK,
    STATUS_LOCK_MOTOR_STATE,
    STATUS_NORMAL_OPEN_SWITCH,
    STATUS_UNLOCK_BLE,
    STATUS_UNLOCK_PHONE_REMOTE,
)
from .coordinator import TuyaLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TuyaSmartLockV2(coordinator, entry)])


class TuyaSmartLockV2(CoordinatorEntity[TuyaLockCoordinator], LockEntity):
    """Tuya smart lock v2 HA entity."""

    _attr_has_entity_name = True
    _attr_name = "Lock"

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_lock"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": coordinator.data.get("name", "Tuya Lock") if coordinator.data else "Tuya Lock",
            "manufacturer": "Tuya",
        }

    # ---- helpers --------------------------------------------------------

    def _status(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("status", {}) or {}

    def _uses_smart_lock_api(self) -> bool:
        status = self._status()
        return any(
            key in status
            for key in (
                STATUS_LOCK_MOTOR_STATE,
                STATUS_AUTOMATIC_LOCK,
                STATUS_UNLOCK_BLE,
                STATUS_UNLOCK_PHONE_REMOTE,
            )
        )

    # ---- entity properties ---------------------------------------------

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def is_locked(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        status = self._status()

        # DL026HA family — derived state. lock_motor_state on this firmware
        # only updates from cloud-API door-operate commands; it ignores
        # Tuya-app unlocks, fingerprint scans, and the auto-lock timer
        # firing. So we ignore it and compute state from things that DO
        # update reliably:
        #   1. automatic_lock = false  →  passage mode  →  Unlocked.
        #   2. _last_unlock_at within auto_lock_time + grace  →  Unlocked.
        #   3. otherwise  →  Locked.
        if STATUS_AUTOMATIC_LOCK in status:
            if status.get(STATUS_AUTOMATIC_LOCK) is False:
                return False  # passage mode

            last_unlock = self.coordinator.last_unlock_at
            if last_unlock is not None:
                try:
                    auto_lock_time = int(
                        status.get(STATUS_AUTO_LOCK_TIME, AUTO_LOCK_TIME_DEFAULT)
                    )
                except (TypeError, ValueError):
                    auto_lock_time = AUTO_LOCK_TIME_DEFAULT
                # Small grace window so the entity stays Unlocked through
                # the firmware's own settling time before re-engaging.
                window = timedelta(seconds=max(auto_lock_time, 1) + 5)
                if dt_util.utcnow() - last_unlock < window:
                    return False
            return True

        # DL031HA fallback: normal_open_switch True means "held open".
        if STATUS_NORMAL_OPEN_SWITCH in status:
            open_mode = status.get(STATUS_NORMAL_OPEN_SWITCH, False)
            return not open_mode

        # Last resort: motor_state with the inverted DL026HA convention.
        if STATUS_LOCK_MOTOR_STATE in status:
            motor = status.get(STATUS_LOCK_MOTOR_STATE)
            if motor is None:
                return None
            return not bool(motor)
        return None

    # ---- commands -------------------------------------------------------

    async def async_unlock(self, **kwargs: Any) -> None:
        if self._uses_smart_lock_api():
            try:
                await self.coordinator.async_unlock_door()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("[LockV2] Smart-lock unlock failed: %s", err)
                raise
            return

        await self.coordinator.async_send_command(
            [{"code": STATUS_NORMAL_OPEN_SWITCH, "value": True}]
        )
        await self.coordinator.async_request_refresh()

    async def async_lock(self, **kwargs: Any) -> None:
        if self._uses_smart_lock_api():
            try:
                ok = await self.coordinator.async_lock_door()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("[LockV2] Smart-lock lock failed: %s", err)
                raise
            if not ok:
                _LOGGER.warning(
                    "[LockV2] door-operate(open=false) did not succeed; "
                    "the auto-lock timer should still engage shortly."
                )
            return

        await self.coordinator.async_send_command(
            [{"code": STATUS_NORMAL_OPEN_SWITCH, "value": False}]
        )
        await self.coordinator.async_request_refresh()
