"""Load the integration through HA's real config-entry machinery.

Uses the config entry exactly as stored on the live box — cloud mode PLUS
the gateway's IP as `local_ip` — and drives it through services, the way the
kids' dashboard (lock.*) and Node-RED (switch.*_passage_mode) do.

Needs pytest-homeassistant-custom-component, and a directory on sys.path
holding `custom_components/tuya_lock_monitor_v2` (see conftest.py).
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
)

from fake_tuya import FakeTuyaCloud  # noqa: E402

DOMAIN = "tuya_lock_monitor_v2"
LOCK = "lock.dl026ha_test_lock"
PASSAGE = "switch.dl026ha_test_passage_mode"

AS_DEPLOYED = {
    "access_id": "a" * 20,
    "access_secret": "s" * 32,
    "device_id": "bf0123456789abcdefghij",
    "endpoint": "https://openapi.tuyaeu.com",
    "local_ip": "192.168.1.50",          # the SG120HA gateway, not the lock
    "local_version": "3.4",
    "mode": "cloud",
}


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def loaded(hass, monkeypatch):
    import custom_components.tuya_lock_monitor_v2.coordinator as coord

    cloud = FakeTuyaCloud(sub=True)
    monkeypatch.setattr(coord, "async_get_clientsession", lambda _hass: cloud)
    monkeypatch.setattr(coord, "STATE_WATCH_INTERVAL", 0.05)
    monkeypatch.setattr(coord.TuyaLockCoordinator, "_RETRY_DELAYS", (0.01, 0.01))

    entry = MockConfigEntry(domain=DOMAIN, data=AS_DEPLOYED, title="Test lock")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry, cloud, hass.data[DOMAIN][entry.entry_id]
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_loads_cloud_only_with_clean_entities(hass, loaded):
    entry, _cloud, coordinator = loaded
    assert entry.state is ConfigEntryState.LOADED
    assert coordinator.is_sub_device
    assert coordinator._ping_task is None               # noqa: SLF001  no gateway polling

    assert hass.states.get(LOCK).state == "locked"
    assert hass.states.get(PASSAGE).state == "off"
    assert hass.states.get("sensor.dl026ha_test_battery").state == "71"
    # Entities that only ever existed because gateway DPs were mis-decoded:
    assert hass.states.get("sensor.dl026ha_test_pending_unlock_requests") is None


async def test_dashboard_unlock_then_lock(hass, loaded):
    _entry, cloud, _c = loaded
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    assert hass.states.get(LOCK).state == "unlocked"
    assert {"door_operate": True} in cloud.writes

    await hass.services.async_call("lock", "lock", {"entity_id": LOCK}, blocking=True)
    assert hass.states.get(LOCK).state == "locked"


async def test_card_relocks_itself_when_the_unlock_window_closes(hass, loaded, freezer):
    """No poll for 10 min now — the card must not sit on 'Unlocked' till then."""
    _entry, cloud, _c = loaded
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    assert hass.states.get(LOCK).state == "unlocked"

    freezer.tick(30 + 5 + 2)                            # auto_lock_time + grace
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == "locked"
    assert cloud.calls["info"] == 1                     # …with no scheduled poll involved


async def test_node_red_passage_toggle_and_lock_card_agree(hass, loaded):
    _entry, _cloud, _c = loaded
    await hass.services.async_call("switch", "turn_on", {"entity_id": PASSAGE}, blocking=True)
    assert hass.states.get(PASSAGE).state == "on"
    assert hass.states.get(LOCK).state == "unlocked"    # the card now tells the truth

    await hass.services.async_call("switch", "turn_off", {"entity_id": PASSAGE}, blocking=True)
    assert hass.states.get(PASSAGE).state == "off"
    assert hass.states.get(LOCK).state == "locked"


async def test_offline_lock_gives_the_person_tapping_an_error(hass, loaded):
    _entry, cloud, _c = loaded
    cloud.fail_codes = [(2001, "device is offline")]
    with pytest.raises(HomeAssistantError, match="offline"):
        await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    assert hass.states.get(LOCK).state == "locked"


async def test_unload_relocks_a_door_left_in_passage_mode(hass, loaded):
    entry, cloud, _c = loaded
    await hass.services.async_call("switch", "turn_on", {"entity_id": PASSAGE}, blocking=True)
    await asyncio.sleep(0.2)                            # let the fake lock apply it
    cloud.writes.clear()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert cloud.writes.count({"automatic_lock": True}) == 1   # once, not twice
    assert {"auto_lock_time": 30} in cloud.writes
