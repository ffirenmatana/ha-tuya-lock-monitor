"""Lock entity for Tuya Lock Monitor v2.

DL026HA (BLE sub-device of SG120HA gateway):
    * Unlock → Smart Lock door-operate with open=true.
    * Lock   → Smart Lock door-operate with open=false.
      (v1 wrote `automatic_lock=True`; that DP is read-only on this firmware
      and writes caused a phantom unlock.)

DL031HA (fallback, kept for parity):
    * `normal_open_switch` toggle.
"""
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

        # DL026HA: lock_motor_state reports motor-engaged position. Despite
        # the name, the firmware semantic is INVERTED:
        #   * lock_motor_state = false  → motor at rest → door LOCKED
        #   * lock_motor_state = true   → motor engaged/open → door UNLOCKED
        # Verified against devices.txt (motor_state=false while doors were
        # known-locked) and against passage-mode behaviour (motor_state=true
        # while doors are held open).
        if STATUS_LOCK_MOTOR_STATE in status:
            motor = status.get(STATUS_LOCK_MOTOR_STATE)
            if motor is None:
                return None
            return not bool(motor)

        # DL026HA detected but motor state not yet seeded — report unknown
        # rather than default to any state.
        if STATUS_AUTOMATIC_LOCK in status:
            return None

        # DL031HA fallback: normal_open_switch True means "held open".
        open_mode = status.get(STATUS_NORMAL_OPEN_SWITCH, False)
        return not open_mode

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
