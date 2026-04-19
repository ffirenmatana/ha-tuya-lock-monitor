"""Lock entity for Tuya Lock Monitor."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
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
    """Set up the lock entity."""
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TuyaSmartLock(coordinator, entry)])


class TuyaSmartLock(CoordinatorEntity[TuyaLockCoordinator], LockEntity):
    """Representation of the Tuya smart lock as a HA lock entity.

    Adapts behavior at runtime:
    - DL026HA family (BLE locks via gateway): uses Tuya Smart Lock API
      (ticket + door-operate) for unlock; writes `automatic_lock=True` for lock.
      State is taken from `lock_motor_state` (True = locked).
    - DL031HA family: uses `normal_open_switch` (passage mode) as the
      lock/unlock toggle.
    """

    _attr_has_entity_name = True
    _attr_name = "Lock"

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the lock entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_lock"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Tuya Lock",
            "manufacturer": "Tuya",
        }

    # ---- helpers --------------------------------------------------------

    def _status(self) -> dict[str, Any]:
        """Return the current status dict from the coordinator (may be empty)."""
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("status", {}) or {}

    def _uses_smart_lock_api(self) -> bool:
        """Detect DL026HA-family devices by the presence of unlock_* or
        automatic_lock / lock_motor_state status codes."""
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
        """Entity is available once the coordinator has data."""
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def is_locked(self) -> bool | None:
        """Return whether the lock is locked.

        Order of precedence:
        1. If `lock_motor_state` is present, it is authoritative
           (True = locked, False = unlocked).
        2. If `automatic_lock` is present (DL026HA) but no `lock_motor_state`
           has been seeded yet, return `None` rather than defaulting to the
           DL031HA passage-mode behaviour. This avoids the misleading "Locked"
           default on cold boot before the device-logs seed runs.
        3. DL031HA fallback: derive from `normal_open_switch` (passage mode).
           True = held open, so we invert it.
        """
        if self.coordinator.data is None:
            return None
        status = self._status()

        # DL026HA family: lock_motor_state is authoritative.
        if STATUS_LOCK_MOTOR_STATE in status:
            motor = status.get(STATUS_LOCK_MOTOR_STATE)
            if motor is None:
                return None
            return bool(motor)

        # DL026HA detected via automatic_lock but motor state not seeded yet.
        # Return None so the UI shows "unknown" instead of falsely "Locked".
        if STATUS_AUTOMATIC_LOCK in status:
            return None

        # DL031HA fallback: passage-mode boolean. True means held open.
        open_mode = status.get(STATUS_NORMAL_OPEN_SWITCH, False)
        return not open_mode

# ---- commands -------------------------------------------------------

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the door."""
        if self._uses_smart_lock_api():
            # DL026HA: ticket + door-operate, optimistic update + watch are
            # handled inside the coordinator helper.
            try:
                await self.coordinator.async_smart_lock_door_operate(open_lock=True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("[Lock] Smart-lock unlock failed: %s", err)
                raise
        else:
            # DL031HA: enable passage mode (normal_open_switch=True).
            await self.coordinator.async_send_command(
                [{"code": STATUS_NORMAL_OPEN_SWITCH, "value": True}]
            )
            await self.coordinator.async_request_refresh()

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the door."""
        if self._uses_smart_lock_api():
            # DL026HA: writing automatic_lock=True triggers the lock motor.
            ok = await self.coordinator.async_send_command(
                [{"code": STATUS_AUTOMATIC_LOCK, "value": True}]
            )
            if not ok:
                _LOGGER.error("[Lock] automatic_lock command failed")
                return

            # Optimistic update so the UI flips immediately.
            if self.coordinator.data is not None:
                status = self.coordinator.data.get("status") or {}
                new_status = {
                    **status,
                    STATUS_AUTOMATIC_LOCK: True,
                    STATUS_LOCK_MOTOR_STATE: True,
                }
                self.coordinator.async_set_updated_data(
                    {**self.coordinator.data, "status": new_status}
                )

            # Burst-poll cloud to confirm the motor state caught up.
            await self.coordinator.async_watch_lock_state()
        else:
            # DL031HA: disable passage mode.
            await self.coordinator.async_send_command(
                [{"code": STATUS_NORMAL_OPEN_SWITCH, "value": False}]
            )
            await self.coordinator.async_request_refresh()
            