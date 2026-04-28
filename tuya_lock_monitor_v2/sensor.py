"""Sensors for Tuya Lock Monitor v2.

Changes vs v1:
  * Name lookups now route through users_yaml.resolve_* instead of a
    per-entry config string.
  * Doorbell sensor is dropped (not useful on the DL026HA family).
  * `person_name` attribute is exposed on every id-bearing sensor so
    Node-RED / template sensors can read a single key.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_USERS_YAML_PATH,
    DOMAIN,
    USERS_YAML_CARD_KEY,
    USERS_YAML_FINGERPRINT_KEY,
    USERS_YAML_PASSWORD_KEY,
)
from .coordinator import TuyaLockCoordinator
from .users_yaml import resolve_name


@dataclass(frozen=True, kw_only=True)
class TuyaLockSensorDescription(SensorEntityDescription):
    status_key: str = ""
    # Section inside tuya_lock_users.yaml, e.g. "fingerprint_names".
    # Empty string means "not a user-ID sensor".
    users_section: str = ""


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
        name="Last Fingerprint Unlock",
        status_key="unlock_fingerprint",
        icon="mdi:fingerprint",
        users_section=USERS_YAML_FINGERPRINT_KEY,
    ),
    TuyaLockSensorDescription(
        key="unlock_password",
        name="Last Password Unlock",
        status_key="unlock_password",
        icon="mdi:form-textbox-password",
        users_section=USERS_YAML_PASSWORD_KEY,
    ),
    TuyaLockSensorDescription(
        key="unlock_card",
        name="Last Card Unlock",
        status_key="unlock_card",
        icon="mdi:card-account-details",
        users_section=USERS_YAML_CARD_KEY,
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
    TuyaLockSensorDescription(
        key="unlock_ble",
        name="Last Bluetooth Unlock",
        status_key="unlock_ble",
        icon="mdi:bluetooth-connect",
    ),
    TuyaLockSensorDescription(
        key="unlock_phone_remote",
        name="Remote App Unlocks",
        status_key="unlock_phone_remote",
        icon="mdi:cellphone-lock",
        state_class=SensorStateClass.TOTAL_INCREASING,
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
    supported = [desc for desc in SENSORS if desc.status_key in status_keys]
    entities: list = [
        TuyaLockSensor(coordinator, entry, desc) for desc in supported
    ]
    entities.append(TuyaLockLastContactSensor(coordinator, entry))
    async_add_entities(entities)


class TuyaLockSensor(CoordinatorEntity[TuyaLockCoordinator], SensorEntity):
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
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.data["name"] if coordinator.data else entry.title,
            model=coordinator.data["product_name"] if coordinator.data else None,
            manufacturer="Tuya",
        )

    def _users_yaml_override(self) -> str | None:
        return (
            self._entry.options.get(CONF_USERS_YAML_PATH)
            or self._entry.data.get(CONF_USERS_YAML_PATH)
            or None
        )

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None

        section = self.entity_description.users_section
        if section:
            # For user-ID sensors, display the last *non-zero* ID the device
            # reported. The DP itself pulses back to 0 almost immediately, so
            # reading status[key] directly would show 0 almost all the time.
            event = self.coordinator.last_user_event(
                self.entity_description.status_key
            )
            if event is None:
                return None
            return resolve_name(
                self.hass, section, event["id"], self._users_yaml_override()
            )

        raw = self.coordinator.data["status"].get(self.entity_description.status_key)
        return raw

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        section = self.entity_description.users_section
        if not section:
            return None

        event = self.coordinator.last_user_event(
            self.entity_description.status_key
        )
        if event is None:
            return None

        person = resolve_name(
            self.hass, section, event["id"], self._users_yaml_override()
        )
        last_seen = event.get("time")
        return {
            "id": event["id"],
            "person_name": person,
            "last_seen": last_seen.isoformat() if last_seen else None,
        }

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


class TuyaLockLastContactSensor(CoordinatorEntity[TuyaLockCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Last Contact"
    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_contact"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.data["name"] if coordinator.data else entry.title,
            model=coordinator.data["product_name"] if coordinator.data else None,
            manufacturer="Tuya",
        )

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_contact

    @property
    def available(self) -> bool:
        return self.coordinator.last_contact is not None
