# Tuya Lock Monitor for Home Assistant

A Home Assistant custom integration for Tuya Smart Locks, with support for the **DL026HA** family when paired with an **SG120HA** BLE-to-Wi-Fi gateway. Works in either **cloud** mode (via the Tuya IoT Platform OpenAPI) or **local** mode (via `tinytuya`).

This integration is a v2 rewrite of [**@crestall**'s original `ha-tuya-lock-monitor`](https://github.com/crestall/ha-tuya-lock-monitor). Huge thanks to crestall for the foundational work that made this possible — the DP mapping, the dual cloud/local coordinator design, and the config-flow UX all originate there.

## What's new in v2

- **Shared user-name YAML** — a single `tuya_lock_users.yaml` drives fingerprint / password / card name resolution across every lock entry. No more per-entry duplication.
- **Last-user event tracking** — the raw DP pulses to an ID and back to `0` in a fraction of a second; v2 captures the last non-zero ID and exposes it as the sensor state plus `id` / `person_name` / `last_seen` attributes.
- **`tuya_lock_monitor_v2_unlock` bus event** — fires on every new unlock with `{entry_id, device_id, device_name, kind, id, time}` for easy automations.
- **Passage Mode switch** — emulates passage mode by maxing `auto_lock_time` and re-issuing the unlock on a timer (DL026HA firmware treats `automatic_lock` as read-only, so the "real" approach doesn't work).
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

You can add the same device in both modes if you want the reliability of local polling with cloud-only controls (like passage mode) as a backup.

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

DL026HA firmware treats `automatic_lock` as read-only — writing it causes phantom unlock events — so true passage mode isn't reachable over the API. The switch emulates it:

- **On** → saves the current `auto_lock_time`, bumps it to 1800 s, unlocks the door, then re-issues an unlock every 1700 s.
- **Off** → cancels the timer, restores the saved `auto_lock_time`, relocks.

Caveats: it's cloud-only (local mode can't call door-operate), and active passage mode adds roughly 50 extra API calls per day from the refresh loop. The switch is only offered on DL026HA-family entries with cloud credentials configured. - This isn't ideal, but I'm working on a better solution.

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

## Credits

- **[@crestall](https://github.com/crestall)** — original `ha-tuya-lock-monitor` integration, which this project is forked from. The dual-mode coordinator, DP mapping, and general shape of the integration are all his work.
- The wider Home Assistant, [tinytuya](https://github.com/jasonacox/tinytuya), and [tuya-iotos-embeded-sdk](https://github.com/tuya/tuya-iotos-embeded-sdk-wifi-ble-bk7231n) communities for the protocol reverse-engineering this all leans on.

## License

Inherits the license of the upstream `ha-tuya-lock-monitor` project.
