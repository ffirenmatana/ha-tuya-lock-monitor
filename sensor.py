"""Sensors for Tuya Lock Monitor."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TuyaLockCoordinator


@dataclass(frozen=True, kw_only=True)
class TuyaLockSensorDescription(SensorEntityDescription):
    status_key: str = ""


SENSORS: tuple[TuyaLockSensorDescription, ...] = (
    TuyaLockSensorDescription(
        key="battery",
        name="Battery",
        status_key="residual_electricity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TuyaLockSensorDescription(
        key="unlock_fingerprint",
        name="Fingerprint Unlocks",
        status_key="unlock_fingerprint",
        icon="mdi:fingerprint",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    TuyaLockSensorDescription(
        key="unlock_password",
        name="Password Unlocks",
        status_key="unlock_password",
        icon="mdi:form-textbox-password",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    TuyaLockSensorDescription(
        key="unlock_card",
        name="Card Unlocks",
        status_key="unlock_card",
        icon="mdi:card-account-details",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    TuyaLockSensorDescription(
        key="unlock_app",
        name="App Unlocks",
        status_key="unlock_app",
        icon="mdi:cellphone-lock",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    TuyaLockSensorDescription(
        key="unlock_temporary",
        name="Temporary Code Unlocks",
        status_key="unlock_temporary",
        icon="mdi:key-clock",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    TuyaLockSensorDescription(
        key="unlock_request",
        name="Pending Unlock Requests",
        status_key="unlock_request",
        icon="mdi:door-open",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TuyaLockSensorDescription(
        key="alarm_lock",
        name="Last Alarm",
        status_key="alarm_lock",
        icon="mdi:alarm-light",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        TuyaLockSensor(coordinator, entry, desc) for desc in SENSORS
    )


class TuyaLockSensor(CoordinatorEntity[TuyaLockCoordinator], SensorEntity):
    """A sensor derived from a Tuya lock status key."""

    entity_description: TuyaLockSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
        description: TuyaLockSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.data["name"] if coordinator.data else entry.title,
            model=coordinator.data["product_name"] if coordinator.data else None,
            manufacturer="Tuya",
        )

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data["status"].get(
            self.entity_description.status_key
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.get("online", False)
        )
