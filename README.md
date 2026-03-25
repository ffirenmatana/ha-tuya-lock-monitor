# Tuya Lock Monitor

A Home Assistant custom component for monitoring **Tuya smart locks** — works fully locally over your LAN or via the Tuya cloud API.

Provides real-time lock status, unlock event counters, battery level, alarm events, and doorbell state — all as native Home Assistant entities.

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
- **Local-only mode** — polls your lock directly over LAN every 15 seconds, no cloud account needed
- **Cloud mode** — polls the Tuya OpenAPI every 60 seconds (or 15s if local IP is also provided)

---

## Connection Modes

### Local Only (recommended for privacy)

Talk directly to the lock over your local network. No Tuya IoT Platform account required after the initial local key retrieval.

**What you need:**
| Item | How to find it |
|---|---|
| Device ID | Tuya/Smart Life app → device details, or tinytuya wizard |
| Local Key | See below |
| Device IP | Router DHCP client table |
| Protocol version | Usually `3.4` — try `3.3` or `3.5` if it doesn't connect |

**How to get the local key** (one-time):
- **Option A — tinytuya wizard:** Install tinytuya (`pip install tinytuya`) on any PC on the same network and run `python -m tinytuya wizard`. It does a one-time cloud login to retrieve the key, then you can keep the key without any ongoing cloud access.
- **Option B — previous cloud setup:** If you already ran the cloud version of this integration, the local key appeared in your HA logs. Search for `local_key` in your HA debug logs.

> **Note:** The local key changes if you reset the device and re-pair it to the app. If the lock stops responding locally, go to **Settings → Devices & Services → Tuya Lock Monitor → Configure** and update the key.

---

### Cloud Mode

Uses the Tuya OpenAPI. Requires a free Tuya IoT Platform account.

**Prerequisites:**

1. Sign up at [iot.tuya.com](https://iot.tuya.com)
2. Go to **Cloud → Development → Create Cloud Project**
   - Select your region (e.g. Europe)
   - Data Centre: **Central Europe Data Centre** (for EU users)
3. In your project → **Service API** tab → subscribe to **IoT Core**
4. In your project → **Devices** tab → **Link Tuya App Account**
   - Scan the QR code with your **Smart Life** or **Tuya Smart** app
5. Note down your **Access ID**, **Access Secret**, and **Device ID**

**Optional — add local IP in cloud mode:**  
Enter your lock's LAN IP when setting up cloud mode to enable hybrid polling: local LAN at 15s intervals with automatic cloud fallback if the local connection fails.

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
2. Copy the `custom_components/tuya_lock_monitor` folder into your HA config directory:
   ```
   config/custom_components/tuya_lock_monitor/
   ```
3. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Tuya Lock Monitor**
3. Choose your connection mode:

**Local only:**

| Field | Description |
|---|---|
| Device ID | Your lock's device ID |
| Local Key | The device's encryption key |
| Lock LAN IP address | Local IP address of the lock |
| Protocol version | Usually `3.4` |

**Cloud:**

| Field | Description |
|---|---|
| Access ID | Client ID from your Tuya IoT project |
| Access Secret | Client Secret from your Tuya IoT project |
| Device ID | The ID of your lock device |
| API Endpoint | Select your region (EU / US / CN / IN) |
| Lock LAN IP address | Optional — enables fast local polling |
| Protocol version | Optional — only used if local IP is set |

4. Click **Submit** — the integration will validate the connection and add the device

### Updating settings after setup

Go to **Settings → Devices & Services → Tuya Lock Monitor → Configure** to update the local IP, local key, or protocol version without re-entering your cloud credentials.

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

**Lock stops responding in local-only mode**
- The local key changes when the device is reset and re-paired. Go to **Configure** and enter the new key.
- Check the IP hasn't changed — set a DHCP reservation in your router for the lock's MAC address.
- Try a different protocol version (3.3, 3.4, 3.5).

**"No permissions" error (cloud mode)**
- Make sure your Tuya IoT project is subscribed to the **IoT Core** API service
- Make sure your Smart Life app account is linked to the project via the **Link Tuya App Account** QR code

**"Invalid Access ID or Secret" error (cloud mode)**
- Double-check you are using the correct Access ID and Secret from your project's Overview tab
- Make sure your chosen endpoint region matches your project's Data Centre region

**Entities unavailable**
- Check that the lock is online in the Smart Life app (cloud mode) or reachable on your network (local mode)
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
