"""Behavioural tests for tuya_lock_monitor_v2 against the real HA coordinator.

Run from the project root:

    <venv>/bin/python -m pytest tests -q

`OLD_PKG_DIR` (optional env var) points at a directory holding the deployed
v2.6.0 as a package named `tlm_old`; when set, the starvation bug is
reproduced on it first, so the fix is shown against a known-bad baseline.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import frame
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
if os.environ.get("OLD_PKG_DIR"):
    sys.path.insert(0, os.environ["OLD_PKG_DIR"])

from fake_tuya import FakeTuyaCloud  # noqa: E402

NEW = importlib.import_module("tuya_lock_monitor_v2.coordinator")
NEW_LOCK = importlib.import_module("tuya_lock_monitor_v2.lock")
NEW_SWITCH = importlib.import_module("tuya_lock_monitor_v2.switch")
OLD = (
    importlib.import_module("tlm_old.coordinator")
    if os.environ.get("OLD_PKG_DIR")
    else None
)

# What the SG120HA gateway really answers a local status() with: its own
# socket DPs (DP 1 = relay on) plus relayed child-lock reports numbered per
# the BLE lock schema (DP 12 = fingerprint id, DP 8 = battery %).
GATEWAY_DPS = {"dps": {"1": True, "9": 0, "12": 2, "8": 71}}


_MADE: list = []


@pytest_asyncio.fixture
async def hass():
    h = HomeAssistant(tempfile.mkdtemp())
    frame.async_setup(h)    # v2.6.0 leans on HA's ContextVar lookup, which reports via this
    yield h
    # What an entry unload does in real HA: stop our tasks/timers, then the
    # base coordinator (v2.6.0's async_shutdown override never reached it).
    for c in _MADE:
        c.async_stop_ping_loop()
        for unsub in list(getattr(c, "_unlock_reset_unsubs", {}).values()):
            unsub()
        await DataUpdateCoordinator.async_shutdown(c)
    _MADE.clear()
    await asyncio.sleep(0)
    await h.async_stop(force=True)


def make(mod, hass, cloud, monkeypatch, *, local_ip=None, update_interval=1, **patches):
    """Build a coordinator from `mod` wired to the fake cloud."""
    monkeypatch.setattr(mod, "async_get_clientsession", lambda _hass: cloud)
    if mod is OLD:
        # v2.6.0 never passes config_entry. HA ignores that for a custom
        # integration; outside one (here) it would raise instead.
        monkeypatch.setattr(frame, "report_usage", lambda *a, **k: None)
    monkeypatch.setattr(mod, "UPDATE_INTERVAL", update_interval)
    monkeypatch.setattr(mod, "PING_INTERVAL", 0.05)
    monkeypatch.setattr(mod, "STATE_WATCH_INTERVAL", 0.1)
    for k, v in patches.items():
        monkeypatch.setattr(mod.TuyaLockCoordinator, k, v)
    c = mod.TuyaLockCoordinator(
        hass, "id" * 10, "secret" * 5, "bf0123456789abcdefghij",
        "https://openapi.tuyaeu.com", local_ip=local_ip, entry_id="E1",
    )

    async def fake_local_status(self=c):
        dps = GATEWAY_DPS["dps"]
        return {mod.DPS_TO_CODE[k]: v for k, v in dps.items() if k in mod.DPS_TO_CODE}

    c._local_get_status = fake_local_status  # noqa: SLF001
    _MADE.append(c)
    return c


async def start(c, *, ping: bool):
    await c.async_refresh()
    c.async_add_listener(lambda: None)      # no listeners → HA schedules nothing
    if ping:
        await c.async_start_ping_loop()


# --------------------------------------------------------------------------
# 1. The root cause: a succeeding 1 Hz ping loop starves the cloud poll
# --------------------------------------------------------------------------

@pytest.mark.skipif(OLD is None, reason="OLD_PKG_DIR not set")
async def test_deployed_v260_never_polls_cloud_while_gateway_answers(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    c = make(OLD, hass, cloud, monkeypatch, local_ip="192.168.1.50")
    await start(c, ping=True)               # what __init__ did: `if local_ip:`
    polls_at_start = cloud.polls
    await asyncio.sleep(3.6)                # 3+ update intervals
    c.async_stop_ping_loop()

    assert cloud.polls == polls_at_start, "cloud poll ran — bug not reproduced"
    # …and meanwhile the gateway's DPs are decoded as this lock's:
    st = c.data["status"]
    assert st["residual_electricity"] == 2   # a fingerprint ID shown as battery %
    assert st["alarm_lock"] == 71            # battery % shown as the last alarm
    assert c.last_unlock_at is not None      # relay state → phantom fingerprint unlock


async def test_sub_device_refuses_local_polling_and_cloud_poll_runs(hass, monkeypatch, caplog):
    cloud = FakeTuyaCloud(sub=True)
    c = make(NEW, hass, cloud, monkeypatch, local_ip="192.168.1.50")
    with caplog.at_level(logging.WARNING):
        await start(c, ping=False)
    assert c.is_sub_device and not c.local_polling_enabled
    assert "gateway sub-device" in caplog.text

    polls_at_start = cloud.polls
    await asyncio.sleep(3.6)
    assert cloud.polls - polls_at_start >= 3
    st = c.data["status"]
    assert st["residual_electricity"] == 71 and st["alarm_lock"] == "wrong_finger"
    assert c.last_unlock_at is None


async def test_wifi_lock_ping_loop_no_longer_starves_cloud_poll(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=False)        # a lock with its own Wi-Fi
    c = make(NEW, hass, cloud, monkeypatch, local_ip="192.168.1.60")
    await start(c, ping=True)
    assert c.local_polling_enabled
    polls_at_start = cloud.polls
    await asyncio.sleep(3.6)
    c.async_stop_ping_loop()
    assert cloud.polls - polls_at_start >= 3


async def test_a_scheduled_poll_costs_one_api_call(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60)
    await start(c, ping=False)
    before = sum(cloud.calls.values())
    await c.async_refresh()
    assert sum(cloud.calls.values()) - before == 1      # metadata + status + online
    assert c.data["online"] is True and c.data["status"]["residual_electricity"] == 71


def test_polling_fits_the_tuya_trial_allowance():
    from tuya_lock_monitor_v2 import const
    locks, allowance = 2, 26_000                        # calls/month, IoT Core Trial
    polling = locks * 30 * 86_400 / const.UPDATE_INTERVAL
    assert polling <= allowance * 0.5, f"{polling:.0f} scheduled polls/month"


async def test_bool_is_never_a_user_id(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    c = make(NEW, hass, cloud, monkeypatch)
    await start(c, ping=False)
    fired = []
    hass.bus.async_listen(NEW.EVENT_UNLOCK, fired.append)
    c._build_result({**c.data["status"], "unlock_fingerprint": True}, "cloud")  # noqa: SLF001
    await hass.async_block_till_done()
    assert not fired and c.last_unlock_at is None
    c._build_result({**c.data["status"], "unlock_fingerprint": 2}, "cloud")  # noqa: SLF001
    await hass.async_block_till_done()
    assert len(fired) == 1 and fired[0].data["id"] == 2


# --------------------------------------------------------------------------
# 2. Commands: accepted ≠ applied
# --------------------------------------------------------------------------

async def test_passage_state_holds_until_slow_ble_lock_confirms(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True, apply_delay=0.8)
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60)
    await start(c, ping=False)

    assert await c.async_enter_passage_mode()
    assert cloud.status["automatic_lock"] is True      # lock hasn't applied it yet
    assert c.passage_mode_active                        # …but HA doesn't bounce
    assert c.data["status"]["auto_lock_time"] == 1800

    await asyncio.sleep(1.5)
    assert cloud.status["automatic_lock"] is False      # applied over "BLE"
    assert c.passage_mode_active and not c._expected    # noqa: SLF001  confirmed
    assert cloud.writes == [{"auto_lock_time": 1800}, {"automatic_lock": False}]


async def test_write_the_lock_never_received_is_noticed(hass, monkeypatch, caplog):
    cloud = FakeTuyaCloud(sub=True)
    cloud.drop_writes = True                            # cloud says OK; lock never hears
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60, _CONFIRM_TIMEOUT=0.6)
    await start(c, ping=False)

    with caplog.at_level(logging.WARNING):
        assert await c.async_enter_passage_mode()       # the cloud did accept it
        assert c.passage_mode_active
        await asyncio.sleep(1.2)
    assert not c.passage_mode_active                    # truth wins
    assert "never reached it over Bluetooth" in caplog.text


async def test_transient_cloud_error_is_retried(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60, _RETRY_DELAYS=(0.01, 0.01))
    await start(c, ping=False)
    cloud.fail_codes = [(501, "request fail with unkown error")] * 2
    assert await c._cloud_send_command([{"code": "beep_volume", "value": "normal"}])  # noqa: SLF001
    assert cloud.calls["commands"] == 3


async def test_offline_fails_fast_with_a_readable_reason(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60, _RETRY_DELAYS=(0.01, 0.01))
    await start(c, ping=False)
    cloud.fail_codes = [(2001, "device is offline")] * 5
    assert not await c.async_unlock_door()
    assert cloud.calls["ticket"] == 1 and cloud.calls["door_operate"] == 0
    assert "offline" in c.last_command_error


async def test_hung_request_cannot_wedge_the_command_lock(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60,
             _RETRY_DELAYS=(0.01,), _POST_TIMEOUT=0.2)
    await start(c, ping=False)
    cloud.hang_posts = 2                                # both attempts never answer
    assert not await c.async_lock_door()
    assert "no answer" in c.last_command_error
    assert await c.async_lock_door()                    # next command isn't blocked


async def test_rejected_token_is_dropped_not_reused_for_two_hours(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60)
    await start(c, ping=False)
    assert cloud.tokens_issued == 1
    cloud.status_fail_codes = [(1010, "token invalid")]
    await c.async_refresh()
    await c.async_refresh()
    assert cloud.tokens_issued == 2 and c.last_update_success


# --------------------------------------------------------------------------
# 3. Passage mode survives an HA restart
# --------------------------------------------------------------------------

async def test_turn_off_after_restart_actually_relocks(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    cloud.status.update(automatic_lock=False, auto_lock_time=1800)   # left in passage mode
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60)       # "fresh boot"
    await start(c, ping=False)

    assert c.passage_mode_active                        # read from the lock, not memory
    assert await c.async_exit_passage_mode()
    assert {"automatic_lock": True} in cloud.writes
    assert {"auto_lock_time": 30} in cloud.writes       # cap undone too
    assert not c.passage_mode_active


@pytest.mark.skipif(OLD is None, reason="OLD_PKG_DIR not set")
async def test_deployed_v260_turn_off_after_restart_was_a_noop(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    cloud.status.update(automatic_lock=False, auto_lock_time=1800)
    c = make(OLD, hass, cloud, monkeypatch, update_interval=60)
    await start(c, ping=False)
    assert await c.async_exit_passage_mode()            # reports success…
    assert cloud.writes == []                           # …having sent nothing


# --------------------------------------------------------------------------
# 4. Entities
# --------------------------------------------------------------------------

def _entry():
    return SimpleNamespace(entry_id="E1", title="Test lock")


async def test_lock_entity_state_and_visible_failure(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60, _RETRY_DELAYS=(0.01, 0.01))
    await start(c, ping=False)
    lock = NEW_LOCK.TuyaSmartLockV2(c, _entry())

    assert lock.is_locked is True
    await lock.async_unlock()
    assert lock.is_locked is False
    await lock.async_lock()
    assert lock.is_locked is True

    cloud.fail_codes = [(2001, "device is offline")]
    with pytest.raises(HomeAssistantError, match="offline"):
        await lock.async_unlock()


async def test_passage_switch_reflects_lock_and_raises_on_failure(hass, monkeypatch):
    cloud = FakeTuyaCloud(sub=True)
    c = make(NEW, hass, cloud, monkeypatch, update_interval=60, _RETRY_DELAYS=(0.01, 0.01))
    await start(c, ping=False)
    sw = NEW_SWITCH.TuyaPassageModeSwitch(c, _entry())
    sw.async_write_ha_state = lambda: None              # not added to a platform

    await sw.async_turn_on()
    assert sw.is_on
    await sw.async_turn_off()
    assert not sw.is_on

    cloud.fail_codes = [(2001, "device is offline")] * 3
    with pytest.raises(HomeAssistantError, match="offline"):
        await sw.async_turn_on()
    assert not sw.is_on
