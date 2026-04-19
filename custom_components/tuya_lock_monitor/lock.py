"""Lock entity for Tuya Lock Monitor.

Two control surfaces are supported, selected automatically based on which
status keys the device actually reports:

1. DL031HA-style locks — expose ``normal_open_switch`` as a writable
   passage-mode boolean.
2. DL026HA-style locks (BLE sub-devices behind a gateway) — report
   ``lock_motor_state`` and are unlocked remotely via Tuya's Smart Lock
   cloud API. Lock re-enables auto-latch via the ``automatic_lock`` DP.
"""
from __future__ import annotations

import logging

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    STATUS_AUTOMATIC_LOCK,
    STATUS_LOCK_MOTOR_STATE,
    STATUS_NORMAL_OPEN_SWITCH,
)
from .coordinator import TuyaLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TuyaSmartLock(coordinator, entry)])


class TuyaSmartLock(CoordinatorEntity[TuyaLockCoordinator], LockEntity):
    _attr_has_entity_name = True
    _attr_name = "Door Lock"

    def __init__(
        self, coordinator: TuyaLockCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_lock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.data["name"] if coordinator.data else entry.title,
            model=coordinator.data["product_name"] if coordinator.data else None,
            manufacturer="Tuya",
        )

    def _status(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("status") or {}

    def _uses_smart_lock_api(self) -> bool:
        status = self._status()
        return (
            STATUS_LOCK_MOTOR_STATE in status
            and STATUS_NORMAL_OPEN_SWITCH not in status
        )

    @property
    def is_locked(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        status = self._status()

        # DL026HA family: lock_motor_state is authoritative.
        if STATUS_LOCK_MOTOR_STATE in status:
            motor = status.get(STATUS_LOCK_MOTOR_STATE)
            if motor is None:
                return None
            return bool(motor)

        # If this device reports automatic_lock, it's a DL026HA and the
        # absence of lock_motor_state means we just haven't seen it yet
        # (common on initial boot). Return None so the card shows
        # "Unknown" rather than defaulting to "Locked" — that default was
        # misleading users into a manual lock/unlock cycle on every restart.
        if STATUS_AUTOMATIC_LOCK in status:
            return None

        # DL031HA fallback: passage-mode boolean. True means held open.
        open_mode = status.get(STATUS_NORMAL_OPEN_SWITCH, False)
        return not open_mode    def is_locked(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        status = self._status()

        if STATUS_LOCK_MOTOR_STATE in status:
            motor = status.get(STATUS_LOCK_MOTOR_STATE)
            if motor is None:
                return None
            return bool(motor)

        open_mode = status.get(STATUS_NORMAL_OPEN_SWITCH, False)
        return not open_mode

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    async def async_unlock(self, **kwargs) -> None:
        if self._uses_smart_lock_api():
            _LOGGER.debug("[TuyaLock] DL026HA-style unlock via Smart Lock API")
            ok = await self.coordinator.async_smart_lock_door_operate(open_lock=True)
            if not ok:
                _LOGGER.warning(
                    "[TuyaLock] Smart-lock unlock did not succeed; "
                    "see prior log lines for cloud response."
                )
            return

        _LOGGER.debug("[TuyaLock] DL031HA-style unlock via normal_open_switch=True")
        await self.coordinator.async_send_command(
            [{"code": STATUS_NORMAL_OPEN_SWITCH, "value": True}]
        )

    async def async_lock(self, **kwargs) -> None:
        if self._uses_smart_lock_api():
            _LOGGER.debug("[TuyaLock] DL026HA-style lock via automatic_lock=True")
            await self.coordinator.async_send_command(
                [{"code": STATUS_AUTOMATIC_LOCK, "value": True}]
            )
            return

        _LOGGER.debug("[TuyaLock] DL031HA-style lock via normal_open_switch=False")
        await self.coordinator.async_send_command(
            [{"code": STATUS_NORMAL_OPEN_SWITCH, "value": False}]
        )
