# Tuya Lock Monitor

A Home Assistant custom component for monitoring **Tuya smart locks** via the [Tuya OpenAPI](https://developer.tuya.com/en/docs/cloud/).

Provides real-time lock status, unlock event counters, battery level, alarm events, and doorbell state — all as native Home Assistant entities that update every 60 seconds.

> Tested with the **DL031HA Series 2** smart lock. Compatible with any Tuya lock in the `jtmspro` category.

---

## Features

- Battery level sensor
- Unlock counters (fingerprint, password, card, app, temporary code)
- Last alarm event (wrong finger, wrong password, pry, low battery, etc.)
- Doorbell binary sensor
- Deadbolt (reverse lock) state
- Duress / hijack alert
- Online / connectivity status
- Lock entity with passage mode control (lock / unlock)
- Config flow UI — no YAML required
- Polls the Tuya OpenAPI every 60 seconds

---

## Prerequisites

Before installing, you need a **Tuya IoT Platform** account with a cloud project set up:

1. Sign up at [iot.tuya.com](https://iot.tuya.com)
2. Go to **Cloud → Development → Create Cloud Project**
   - Select your region (e.g. Europe)
   - Data Centre: **Central Europe Data Centre** (for EU users)
3. In your project → **Service API** tab → subscribe to **IoT Core**
4. In your project → **Devices** tab → **Link Tuya App Account**
   - Scan the QR code with your **Smart Life** or **Tuya Smart** app
   - Your devices will now appear in the project
5. Note down your:
   - **Access ID** (Client ID) — shown on the project Overview tab
   - **Access Secret** (Client Secret) — shown on the project Overview tab
   - **Device ID** — shown under Cloud → Devices

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations → ⋮ → Custom repositories**
3. Add `https://github.com/crestall/ha-tuya-lock-monitor` as category **Integration**
4. Search for **Tuya Lock Monitor** and install
5. Restart Home Assistant

### Manual

1. Download or clone this repository
2. Copy the `tuya_lock_monitor` folder into your HA config directory:
   ```
   config/custom_components/tuya_lock_monitor/
   ```
3. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Tuya Lock Monitor**
3. Fill in the form:

| Field | Description |
|---|---|
| Access ID | Client ID from your Tuya IoT project |
| Access Secret | Client Secret from your Tuya IoT project |
| Device ID | The ID of your lock device |
| API Endpoint | Select your region (EU / US / CN / IN) |

4. Click **Submit** — the integration will validate your credentials and add the device

---

## Entities

Once configured, the following entities are created under your lock device:

### Sensors

| Entity | Description |
|---|---|
| `sensor.battery` | Battery level (%) |
| `sensor.fingerprint_unlocks` | Cumulative fingerprint unlock count |
| `sensor.password_unlocks` | Cumulative password unlock count |
| `sensor.card_unlocks` | Cumulative card unlock count |
| `sensor.app_unlocks` | Cumulative app unlock count |
| `sensor.temporary_code_unlocks` | Cumulative temporary code unlock count |
| `sensor.pending_unlock_requests` | Number of pending unlock requests |
| `sensor.last_alarm` | Most recent alarm event type |

### Binary Sensors

| Entity | Description |
|---|---|
| `binary_sensor.doorbell` | On when doorbell is pressed |
| `binary_sensor.deadbolt_reverse_lock` | On when deadbolt is engaged |
| `binary_sensor.duress_hijack_alert` | On when a duress/hijack event is detected |
| `binary_sensor.normally_open_mode` | On when lock is held in passage mode |
| `binary_sensor.online` | Connectivity — on when lock is online |

### Lock

| Entity | Description |
|---|---|
| `lock.door_lock` | Controls passage mode (hold open / release) |

> **Note:** The lock entity controls the `normal_open_switch` data point (passage/hold-open mode). This is not a remote unlock — it holds the lock open for hands-free access or closes it to normal latching mode.

---

## API Endpoints by Region

| Region | Endpoint |
|---|---|
| Europe | `https://openapi.tuyaeu.com` |
| United States | `https://openapi.tuyaus.com` |
| China | `https://openapi.tuyacn.com` |
| India | `https://openapi.tuyain.com` |

Choose the region that matches the **Data Centre** you selected when creating your Tuya IoT project.

---

## Alarm Event Values

The `last_alarm` sensor reports one of the following strings:

| Value | Meaning |
|---|---|
| `wrong_finger` | Failed fingerprint attempt |
| `wrong_password` | Failed password attempt |
| `wrong_card` | Failed card attempt |
| `wrong_face` | Failed face recognition |
| `pry` | Pry/tamper detected |
| `low_battery` | Battery critically low |
| `power_off` | Power lost |
| `shock` | Physical shock detected |
| `key_in` | Key inserted |
| `unclosed_time` | Door left open too long |
| `tongue_bad` | Latch bolt problem |
| `tongue_not_out` | Bolt not extended |
| `defense` | Armed/defense mode triggered |

---

## Troubleshooting

**"No permissions" error**
- Make sure your Tuya IoT project is subscribed to the **IoT Core** API service
- Make sure your Smart Life app account is linked to the project via the **Link Tuya App Account** QR code

**"Invalid Access ID or Secret" error**
- Double-check you are using the correct Access ID and Secret from your project's Overview tab
- Make sure your chosen endpoint region matches your project's Data Centre region

**Entities unavailable**
- Check that the lock is online in the Smart Life app
- Check HA logs: **Settings → System → Logs** (filter by `tuya_lock_monitor`)

**Enable debug logging** by adding to `configuration.yaml`:
```yaml
logger:
  logs:
    custom_components.tuya_lock_monitor: debug
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.
