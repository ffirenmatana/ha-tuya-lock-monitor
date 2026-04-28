"""Switch entities for Tuya Lock Monitor v2.

Entities:
  * Do Not Disturb — when the DP is present on the device.
  * Passage Mode — emulated via auto_lock_time bump + periodic re-unlock.
    See coordinator.async_enter_passage_mode for the mechanics. Offered on
    DL026HA-family devices only (detected by presence of lock_motor_state
    or automatic_lock in the status dict) and requires cloud credentials.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    STATUS_AUTOMATIC_LOCK,
    STATUS_DO_NOT_DISTURB,
    STATUS_LOCK_MOTOR_STATE,
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
    if STATUS_DO_NOT_DISTURB in status_keys:
        entities.append(TuyaDoNotDisturbSwitch(coordinator, entry))
    if (
        coordinator.cloud_enabled
        and (
            STATUS_LOCK_MOTOR_STATE in status_keys
            or STATUS_AUTOMATIC_LOCK in status_keys
        )
    ):
        entities.append(TuyaPassageModeSwitch(coordinator, entry))
    if entities:
        async_add_entities(entities)


class TuyaPassageModeSwitch(CoordinatorEntity[TuyaLockCoordinator], SwitchEntity):
    """Emulated passage mode (auto_lock_time bump + periodic re-unlock)."""

    _attr_has_entity_name = True
    _attr_name = "Passage Mode"
    _attr_icon = "mdi:door-open"

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_passage_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.data["name"] if coordinator.data else entry.title,
            model=coordinator.data["product_name"] if coordinator.data else None,
            manufacturer="Tuya",
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.passage_mode_active

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.cloud_enabled
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        ok = await self.coordinator.async_enter_passage_mode()
        if not ok:
            _LOGGER.warning("[PassageV2] Failed to enter passage mode")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_exit_passage_mode()
        self.async_write_ha_state()


class TuyaDoNotDisturbSwitch(CoordinatorEntity[TuyaLockCoordinator], SwitchEntity):
    """Suppress keypad/beep sounds regardless of beep_volume."""

    _attr_has_entity_name = True
    _attr_name = "Do Not Disturb"
    _attr_icon = "mdi:volume-off"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_do_not_disturb"
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
        value = self._status().get(STATUS_DO_NOT_DISTURB)
        if value is None:
            return None
        return bool(value)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and STATUS_DO_NOT_DISTURB in self._status()
        )

    def _set_optimistic(self, value: bool) -> None:
        if self.coordinator.data is None:
            return
        status = self.coordinator.data.get("status")
        if status is None:
            return
        new_status = {**status, STATUS_DO_NOT_DISTURB: value}
        self.coordinator.async_set_updated_data(
            {**self.coordinator.data, "status": new_status}
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        ok = await self.coordinator.async_send_command(
            [{"code": STATUS_DO_NOT_DISTURB, "value": True}]
        )
        if ok:
            self._set_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        ok = await self.coordinator.async_send_command(
            [{"code": STATUS_DO_NOT_DISTURB, "value": False}]
        )
        if ok:
            self._set_optimistic(False)
