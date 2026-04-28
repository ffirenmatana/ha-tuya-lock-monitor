"""Select entity for Tuya Lock Monitor v2 — beep volume."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BEEP_VOLUME_OPTIONS,
    DOMAIN,
    STATUS_BEEP_VOLUME,
)
from .coordinator import TuyaLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    status = (coordinator.data or {}).get("status", {}) or {}
    if STATUS_BEEP_VOLUME not in status:
        _LOGGER.debug(
            "[SelectV2] %s not present in device status — skipping entity",
            STATUS_BEEP_VOLUME,
        )
        return
    async_add_entities([TuyaLockBeepVolume(coordinator, entry)])


class TuyaLockBeepVolume(CoordinatorEntity[TuyaLockCoordinator], SelectEntity):
    """Beep volume select (mute / normal)."""

    _attr_has_entity_name = True
    _attr_name = "Beep Volume"
    _attr_icon = "mdi:volume-high"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(BEEP_VOLUME_OPTIONS)

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_beep_volume"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.data["name"] if coordinator.data else entry.title,
            model=coordinator.data["product_name"] if coordinator.data else None,
            manufacturer="Tuya",
        )

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data["status"].get(STATUS_BEEP_VOLUME)
        if value is None:
            return None
        return str(value)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    async def async_select_option(self, option: str) -> None:
        if option not in BEEP_VOLUME_OPTIONS:
            _LOGGER.error(
                "[SelectV2] %s is not a valid beep_volume option (want one of %s)",
                option, BEEP_VOLUME_OPTIONS,
            )
            return

        ok = await self.coordinator.async_send_command(
            [{"code": STATUS_BEEP_VOLUME, "value": option}]
        )
        if not ok:
            _LOGGER.warning("[SelectV2] beep_volume=%s command failed", option)
            return

        # Optimistic reflect — the coordinator's refresh will correct us if the
        # device rejects the value.
        if self.coordinator.data is not None:
            status = self.coordinator.data.get("status") or {}
            new_status = {**status, STATUS_BEEP_VOLUME: option}
            self.coordinator.async_set_updated_data(
                {**self.coordinator.data, "status": new_status}
            )
