"""Binary sensors for Tuya Lock Monitor."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TuyaLockCoordinator


@dataclass(frozen=True, kw_only=True)
class TuyaLockBinarySensorDescription(BinarySensorEntityDescription):
    status_key: str = ""
    invert: bool = False


BINARY_SENSORS: tuple[TuyaLockBinarySensorDescription, ...] = (
    TuyaLockBinarySensorDescription(
        key="doorbell",
        name="Doorbell",
        status_key="doorbell",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        icon="mdi:doorbell",
    ),
    TuyaLockBinarySensorDescription(
        key="reverse_lock",
        name="Deadbolt (Reverse Lock)",
        status_key="reverse_lock",
        device_class=BinarySensorDeviceClass.LOCK,
        icon="mdi:lock-plus",
        invert=True,
    ),
    TuyaLockBinarySensorDescription(
        key="hijack",
        name="Duress / Hijack Alert",
        status_key="hijack",
        device_class=BinarySensorDeviceClass.TAMPER,
        icon="mdi:alert-octagon",
    ),
    TuyaLockBinarySensorDescription(
        key="normal_open_switch",
        name="Normally Open Mode",
        status_key="normal_open_switch",
        icon="mdi:door-open",
    ),
    TuyaLockBinarySensorDescription(
        key="online",
        name="Online",
        status_key="__online__",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:cloud-check",
    ),
    TuyaLockBinarySensorDescription(
        key="lock_motor_state",
        name="Lock Motor State",
        status_key="lock_motor_state",
        device_class=BinarySensorDeviceClass.LOCK,
        icon="mdi:lock-check",
        invert=True,
    ),
    TuyaLockBinarySensorDescription(
        key="automatic_lock",
        name="Auto-lock Enabled",
        status_key="automatic_lock",
        icon="mdi:lock-clock",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    status_keys: set[str] = set(
        (coordinator.data or {}).get("status", {}).keys()
    )
    supported = [
        desc for desc in BINARY_SENSORS
        if desc.status_key == "__online__" or desc.status_key in status_keys
    ]
    async_add_entities(
        TuyaLockBinarySensor(coordinator, entry, desc) for desc in supported
    )


class TuyaLockBinarySensor(
    CoordinatorEntity[TuyaLockCoordinator], BinarySensorEntity
):
    entity_description: TuyaLockBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
        description: TuyaLockBinarySensorDescription,
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
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        key = self.entity_description.status_key
        if key == "__online__":
            value = self.coordinator.data.get("online")
        else:
            value = self.coordinator.data["status"].get(key)
        if value is None:
            return None
        result = bool(value)
        return not result if self.entity_description.invert else result

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None