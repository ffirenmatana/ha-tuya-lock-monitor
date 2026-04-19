"""Switch entities for Tuya Lock Monitor.

Currently exposes a single switch — Passage Mode — for DL026HA-style locks.

Passage mode in the Tuya app is a composite of two operations:
  ON  → unlock the door (Smart Lock door-operate, open=true)
        + write automatic_lock=False so the door stays open
  OFF → write automatic_lock=True (door will re-latch on next close or motor cycle)

The switch's reported state mirrors automatic_lock inverted: passage mode is
ON when auto-lock is OFF.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    STATUS_AUTOMATIC_LOCK,
)
from .coordinator import TuyaLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    status_keys: set[str] = set(
        (coordinator.data or {}).get("status", {}).keys()
    )
    entities: list = []
    # Only attach the passage-mode switch when the device actually reports
    # automatic_lock — i.e. it's a DL026HA-family lock.
    if STATUS_AUTOMATIC_LOCK in status_keys:
        entities.append(TuyaPassageModeSwitch(coordinator, entry))
    if entities:
        async_add_entities(entities)


class TuyaPassageModeSwitch(CoordinatorEntity[TuyaLockCoordinator], SwitchEntity):
    """Toggle that mirrors the Tuya app's Passage Mode."""

    _attr_has_entity_name = True
    _attr_name = "Passage Mode"
    _attr_icon = "mdi:door-open"

    def __init__(
        self, coordinator: TuyaLockCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_passage_mode"
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

    @property
    def is_on(self) -> bool | None:
        """Passage mode is ON when automatic_lock is False."""
        auto_lock = self._status().get(STATUS_AUTOMATIC_LOCK)
        if auto_lock is None:
            return None
        return not bool(auto_lock)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and STATUS_AUTOMATIC_LOCK in self._status()
        )

    def _set_optimistic(self, auto_lock: bool) -> None:
        """Reflect the new auto_lock value in the coordinator dict immediately."""
        if self.coordinator.data is None:
            return
        status = self.coordinator.data.get("status")
        if status is None:
            return
        new_status = {**status, STATUS_AUTOMATIC_LOCK: auto_lock}
        self.coordinator.async_set_updated_data(
            {**self.coordinator.data, "status": new_status}
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enter passage mode: unlock the door, then disable auto-lock."""
        _LOGGER.debug("[Passage] Enabling — unlock + automatic_lock=False")
        # Order matters: unlock first so the bolt is retracted before we
        # disable auto-lock; otherwise the door could re-latch in between.
        ok = await self.coordinator.async_smart_lock_door_operate(open_lock=True)
        if not ok:
            _LOGGER.warning("[Passage] Door-open failed; leaving auto_lock unchanged")
            return
        sent = await self.coordinator.async_send_command(
            [{"code": STATUS_AUTOMATIC_LOCK, "value": False}]
        )
        if sent:
            self._set_optimistic(False)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Exit passage mode: re-enable auto-lock.

        This does not actively engage the motor — it just tells the firmware
        to re-latch on the next close (or on the next normal lock cycle).
        Use the Door Lock entity if you also want to trigger the bolt.
        """
        _LOGGER.debug("[Passage] Disabling — automatic_lock=True")
        sent = await self.coordinator.async_send_command(
            [{"code": STATUS_AUTOMATIC_LOCK, "value": True}]
        )
        if sent:
            self._set_optimistic(True)