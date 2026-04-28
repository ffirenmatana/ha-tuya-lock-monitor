"""Number entity for Tuya Lock Monitor v2 — auto-lock timer."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    AUTO_LOCK_TIME_MAX,
    AUTO_LOCK_TIME_MIN,
    AUTO_LOCK_TIME_STEP,
    DOMAIN,
    STATUS_AUTO_LOCK_TIME,
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
    if STATUS_AUTO_LOCK_TIME not in status:
        _LOGGER.debug(
            "[NumberV2] %s not present in device status — skipping entity",
            STATUS_AUTO_LOCK_TIME,
        )
        return
    async_add_entities([TuyaLockAutoLockTime(coordinator, entry)])


class TuyaLockAutoLockTime(CoordinatorEntity[TuyaLockCoordinator], NumberEntity):
    """Seconds after unlock before the motor re-latches (1..1800)."""

    _attr_has_entity_name = True
    _attr_name = "Auto-lock Time"
    _attr_icon = "mdi:timer-lock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = float(AUTO_LOCK_TIME_MIN)
    _attr_native_max_value = float(AUTO_LOCK_TIME_MAX)
    _attr_native_step = float(AUTO_LOCK_TIME_STEP)
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_auto_lock_time"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.data["name"] if coordinator.data else entry.title,
            model=coordinator.data["product_name"] if coordinator.data else None,
            manufacturer="Tuya",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data["status"].get(STATUS_AUTO_LOCK_TIME)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    async def async_set_native_value(self, value: float) -> None:
        clamped = int(round(value))
        if clamped < AUTO_LOCK_TIME_MIN:
            clamped = AUTO_LOCK_TIME_MIN
        elif clamped > AUTO_LOCK_TIME_MAX:
            clamped = AUTO_LOCK_TIME_MAX

        ok = await self.coordinator.async_send_command(
            [{"code": STATUS_AUTO_LOCK_TIME, "value": clamped}]
        )
        if not ok:
            _LOGGER.warning("[NumberV2] auto_lock_time=%s command failed", clamped)
            return

        # Optimistic push so the slider snaps immediately.
        if self.coordinator.data is not None:
            status = self.coordinator.data.get("status") or {}
            new_status = {**status, STATUS_AUTO_LOCK_TIME: clamped}
            self.coordinator.async_set_updated_data(
                {**self.coordinator.data, "status": new_status}
            )
