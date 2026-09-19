# Tuya Lock Monitor for Home Assistant

A Home Assistant custom integration for Tuya Smart Locks, with first-class support for the **DL026HA** family when paired with an **SG120HA** BLE-to-Wi-Fi gateway. Works in either **cloud** mode (via the Tuya IoT Platform OpenAPI) or **local** mode (via `tinytuya`).

This integration is a v2 rewrite of [**@crestall**'s original `ha-tuya-lock-monitor`](https://github.com/crestall/ha-tuya-lock-monitor). Huge thanks to crestall for the foundational work that made this possible — the DP mapping, the dual cloud/local coordinator design, and the config-flow UX all originate there.

## What's new in v2

- **Shared user-name YAML** — a single `tuya_lock_users.yaml` drives fingerprint / password / card name resolution across every lock entry. No more per-entry duplication.
- **Last-user event tracking** — the raw DP pulses to an ID and back to `0` in a fraction of a second; v2 captures the last non-zero ID and exposes it as the sensor state plus `id` / `person_name` / `last_seen` attributes.
- **`tuya_lock_monitor_v2_unlock` bus event** — fires on every new unlock with `{entry_id, device_id, device_name, kind, id, time}` for easy automations.
- **Passage Mode switch** — real passage mode (not emulated). Writes `automatic_lock=false` (auto-lock off) to put the lock into stay-unlocked mode, with a 30-minute hardware-level backstop in case HA crashes while it's on. Cloud-credentials only.
- **Do Not Disturb switch** — toggles the DP of the same name when the device exposes it.
- **Beep volume select** — `mute` / `normal`.
- **Auto-lock time number** — slider for 1–1800 s.
- **Auto-lock armed binary sensor** — read-only reflection of the `automatic_lock` status.
- Domain renamed to `tuya_lock_monitor_v2` so it coexists cleanly with v1.

## Supported devices

- **DL026HA** — BLE smart lock, sub-device of an SG120HA gateway. Primary target.
- **DL031HA** — legacy Wi-Fi lock. All v1 DPs carry forward; newer v2 control surfaces appear where the device reports them.
- **SG120HA** — paired as a hub. Not controlled directly by this integration (use the core Tuya integration for the hub's switch/light DPs).

Every DP is surfaced conditionally — entities only appear when the device reports the matching status code, so no phantom controls on models that don't support a feature.

## Installation

1. Copy the `tuya_lock_monitor_v2/` folder into `<config>/custom_components/`.
2. (Optional) Copy `tuya_lock_monitor_v2/tuya_lock_users.yaml.example` to `<config>/tuya_lock_users.yaml` and fill in your user IDs. No `configuration.yaml` entry is required.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Tuya Lock Monitor v2.**
5. Pick a mode:
   - **Cloud** — needs your Tuya IoT Platform `access_id`, `access_secret`, `device_id`, and region endpoint.
   - **Local** — needs the device's LAN IP, `local_key`, and protocol version (3.3 / 3.4 / 3.5).

### BLE locks behind a gateway: cloud only, no local IP

A DL026HA has no Wi-Fi — it is a Bluetooth sub-device of the SG120HA gateway. **Do not give it a local IP.** The only IP you could enter is the gateway's, and the gateway answers a local poll with its *own* DPs plus relayed reports from *every* child lock, numbered per the BLE-lock schema. None of that matches this integration's local DP table (a DL031HA Wi-Fi table), and nothing routes it per lock. What that looked like in practice (v2.6.0, measured on a live system):

| Symptom | Cause |
| --- | --- |
| Lock entity stuck on **Unlocked**; passage switch showing the *previous* command's state | The 1 Hz ping loop "succeeded" every second and each success reset HA's poll timer — the cloud poll **never ran** (0 polls in 150 s) |
| "Fingerprint unlock by user 1" on **both** locks, every second, forever (~76,000 recorder rows/day per lock) | The gateway socket's relay state (DP 1 = `true`) decoded as `unlock_fingerprint`; `int(True) == 1` |
| Battery jumping to 0–2 %; *Last alarm* reading "71" | Real DP 12 is the fingerprint ID (decoded as battery); real DP 8 is the battery (decoded as the alarm) |
| HA start-up "timed out waiting on `tuya_lock_v2_ping`" | The loop was a tracked task, not a background task |

Since v2.7.0 the integration detects sub-devices from cloud metadata (`sub: true`) and refuses to poll them locally, whatever the entry says — existing entries need no edit. Local polling remains available for locks with their own Wi-Fi (DL031HA).

## Entities

| Platform | Entity | Notes |
| --- | --- | --- |
| `lock` | Lock | Cloud mode uses Smart Lock door-operate (ticket + `open`). Local mode toggles the motor DP where the device accepts writes. |
| `sensor` | Battery | `residual_electricity`. |
| `sensor` | Last Fingerprint / Password / Card Unlock | State = resolved name; attributes expose `id`, `person_name`, `last_seen`. |
| `sensor` | App / Temporary / Remote / BLE unlock counters | `TOTAL_INCREASING` state class where appropriate. |
| `sensor` | Last Alarm | Mirrors `alarm_lock`. |
| `sensor` | Last Contact | Timestamp of the last successful poll. Diagnostic. |
| `binary_sensor` | Auto-lock Armed | Read-only reflection of `automatic_lock`. |
| `switch` | Do Not Disturb | When the DP is present. |
| `switch` | Passage Mode | DL026HA + cloud credentials only. See below. |
| `select` | Beep Volume | `mute` / `normal`. |
| `number` | Auto-lock Time | 1–1800 s slider. |

## The user YAML

`<config>/tuya_lock_users.yaml`:

```yaml
fingerprint_names:
  1: Pat
  2: Alex
  3: Guest

password_names:
  1: Front door code
  2: Cleaner pin

card_names:
  1: Blue key fob
  2: Spare card
```

IDs not listed fall through as the raw integer string. Reload any v2 entry (or restart HA) to pick up edits.

## Passage Mode

Real, server-side passage mode — no refresh loop, no extra API calls while held.

The DL026HA firmware exposes the `automatic_lock` DP as a writable Boolean function with the obvious semantics — "should the lock auto-lock?" (verified empirically against the Tuya app's passage-mode toggle):

- **On** → saves the current `auto_lock_time`, bumps it to 1800 s as a hardware-level safety backstop, then writes `automatic_lock=false`. Auto-lock is disabled: the motor unlocks and stays unlocked indefinitely. Two API calls total.
- **Off** → writes `automatic_lock=true` (auto-lock re-enabled, the door relocks immediately), then restores the saved `auto_lock_time`. Two API calls total.

Cloud credentials are required (the writable DP is only reachable via the IoT Platform), so the switch is only offered on DL026HA-family entries with a cloud config.

### Crash-safety

If HA shuts down cleanly (or the integration is unloaded / reconfigured) while passage mode is active, a shutdown hook writes `automatic_lock=true` (auto-lock back on) so the door doesn't stay open.

If HA dies hard before the shutdown hook runs, the 30-minute `auto_lock_time` cap set when entering passage mode acts as a hardware-level backstop: the lock physically re-engages within half an hour even if no software ever talks to it again. Worst-case unlocked exposure after a hard crash is therefore bounded.

## How commands are confirmed

The Tuya cloud answers a write with `success` as soon as it has *queued* it for the gateway. A BLE lock applies it some seconds later — or never, if the Bluetooth hop fails. So after every write the integration:

1. shows the commanded value immediately (no UI bounce while the lock catches up);
2. burst-polls `/status` every 3 s until the lock reports that value back;
3. if it still hasn't after 45 s, logs `[Confirm] … never reached it over Bluetooth` and lets the lock's own reported value win.

Writes that fail outright are retried twice (transient cloud errors such as `501`), except `2001 device is offline`, which fails at once. Either way the service call **raises**, so a dashboard tap shows an error toast and an automation can catch it, instead of a silent no-op.

The passage-mode switch reads the lock's `automatic_lock` DP rather than remembering what was last sent, so it is still right after an HA restart, and after someone toggles passage mode in the Tuya app.

## API budget

Tuya's IoT Core **Trial** plan allows 26,000 API calls a month per cloud project and suspends service when they run out. The scheduled poll is therefore one call (device info embeds the full status) every 10 minutes — about 8,600 calls a month for two locks, roughly 13,000 with commands and confirmation bursts. Nothing in HA waits on that poll; it only picks up changes made outside HA. See `UPDATE_INTERVAL` in `const.py` before shortening it. Check real usage under *Cloud → your project → Overview* on iot.tuya.com.

## Events

Every new unlock fires a Home Assistant bus event you can trigger on:

```yaml
trigger:
  - platform: event
    event_type: tuya_lock_monitor_v2_unlock
    event_data:
      kind: unlock_fingerprint
      id: 1           # or omit to match any user
```

Payload fields: `entry_id`, `device_id`, `device_name`, `kind` (`unlock_fingerprint` / `unlock_password` / `unlock_card`), `id`, `time`.

## Tests

`tests/` runs the integration against the real Home Assistant coordinator and config-entry machinery, with a fake Tuya cloud and a simulated slow BLE lock (`tests/fake_tuya.py`):

```bash
python -m venv .venv && .venv/bin/pip install "homeassistant==2026.9.3" tinytuya pytest pytest-asyncio
.venv/bin/pip install --no-deps pytest-homeassistant-custom-component   # then its non-HA requirements
.venv/bin/python -m pytest tests -c tests/pytest.ini
```

Set `OLD_PKG_DIR` to a directory holding v2.6.0 as a package named `tlm_old` to also reproduce the v2.6.0 bugs on a known-bad baseline.

## Changelog

### 2.7.0 — 2026-09-19
- **Fix:** local polling no longer starves the cloud poll (`async_set_updated_data` resets the refresh timer; the ping loop now publishes without touching it, and only on change).
- **Fix:** gateway sub-devices are never polled locally (see above).
- **Fix:** a boolean DP can no longer be read as a user ID (phantom unlock events).
- **Fix:** passage-mode state comes from the lock, not memory — `turn_off` after an HA restart used to be a silent no-op that left the door open.
- **Fix:** lock / unlock / passage failures raise instead of being logged and swallowed.
- **Fix:** the lock entity re-renders when the recent-unlock window closes, and whenever that marker changes, rather than waiting for the next poll.
- **New:** command confirmation, retries, a per-request timeout, and per-lock command serialisation.
- **Changed:** one API call per poll, every 600 s (was two every 60 s on paper, zero in practice) to fit the Trial plan.
- Ping and state-watch loops are background tasks; the coordinator is given its config entry explicitly; HA's own unload hook now reaches the base-class shutdown.

## Credits

- **[@crestall](https://github.com/crestall)** — original `ha-tuya-lock-monitor` integration, which this project is forked from. The dual-mode coordinator, DP mapping, and general shape of the integration are all his work.
- The wider Home Assistant, [tinytuya](https://github.com/jasonacox/tinytuya), and [tuya-iotos-embeded-sdk](https://github.com/tuya/tuya-iotos-embeded-sdk-wifi-ble-bk7231n) communities for the protocol reverse-engineering this all leans on.

## License

Inherits the license of the upstream `ha-tuya-lock-monitor` project.
