"""Lock entity for Tuya Lock Monitor."""
from __future__ import annotations

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TuyaLockCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TuyaSmartLock(coordinator, entry)])


class TuyaSmartLock(CoordinatorEntity[TuyaLockCoordinator], LockEntity):
    """Represents the door lock via the normal_open_switch data point.

    normal_open_switch = True  → lock held open (unlocked/passage mode)
    normal_open_switch = False → lock operating normally (locked)
    """

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

    @property
    def is_locked(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        # normal_open_switch True = held open = NOT locked
        open_mode = self.coordinator.data["status"].get("normal_open_switch", False)
        return not open_mode

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    async def async_lock(self, **kwargs) -> None:
        """Disable passage mode (allow the lock to latch normally)."""
        await self.coordinator.async_send_command(
            [{"code": "normal_open_switch", "value": False}]
        )

    async def async_unlock(self, **kwargs) -> None:
        """Enable passage mode (hold lock open)."""
        await self.coordinator.async_send_command(
            [{"code": "normal_open_switch", "value": True}]
        )
