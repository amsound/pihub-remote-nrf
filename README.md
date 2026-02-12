
# PiHub – Universal Remote Bridge (Harmony Remote & Pi)

PiHub turns a Raspberry Pi into a tiny, fast “universal remote” bridge.
It listens to RF key events from a Logitech Harmony Remote (simple, no display) paired to a Logitech Unifying receiver and sends actions to:

* **Home Assistant (HA)** over WebSocket (`pihub.cmd` events)
* **BLE HID** (Consumer + Keyboard) tested with Apple TV 4k 3rd Gen.

It’s lightweight, stateless, and tuned for **Raspberry Pi 3B+ (aarch64)**. No Harmony Hub or cloud required.

---

## ✨ Features

* **RF → Actions** via Linux `evdev`, mapped to canonical `rem_*` names
* **HA WebSocket**: subscribe to `input_select.activity` to change keymap
* **BLE Output**: per-button **Consumer + Keyboard** usages
* **Macros**: **HA-driven** e.g. ble keys to return Apple TV to Home Screen for automations
* **Precise edges**: explicit **down/up**; filters kernel auto-repeat
* **Optional synthetic repeats** (global initial/rate, defaults 400ms/400ms; off unless enabled per-key) ideal for HA calls
* **Long-press**: hold for 'X' ms then trigger
* **No queueing**: drops actions when offline; **reconnects with jitter**

---

## 🧩 Requirements

* Raspberry Pi 3B+ (tested on **aarch64** Raspberry Pi OS Lite)
* Logitech Unifying receiver (model U-0007 recommended!)
* Home Assistant reachable over WebSocket (I run this locally in a Docker Container)
* BlueZ running on the host (DBus socket exposed to the container)

---

## 🚀 Quick Start

### docker-compose.yml & prebuilt docker image

```yaml
services:
  pihub:
    image: a1exm/pihub:latest
    network_mode: host
    restart: unless-stopped
    cpu_shares: 2048
    device_cgroup_rules:
      - 'c 13:* r'
    environment:
      HA_TOKEN: "###############"                        # This ENV takes precedence
      # HA_TOKEN_FILE: "/run/secrets/ha"                 # optional fallback
      # HA_WS_URL: "ws://127.0.0.1:8123/api/websocket"   # defaults to local
      # DEBUG: 1                                         # optional for debug chatter
    volumes:
      - /dev/input:/dev/input:ro
      - /dev/input/by-id:/dev/input/by-id:ro
      - /run/dbus:/run/dbus
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro

  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:stable
    container_name: homeassistant
    cpu_shares: 512
    network_mode: host
    restart: unless-stopped
    environment:
      TZ: Europe/London
    volumes:
      - ./homeassistant:/config
      - /etc/localtime:/etc/localtime:ro
```
then
```bash
docker compose up -d
```

> ✅ I've tested with these settings and works without full blown `--privileged`.

### Build then push to docker hub (personal reminder):

```bash
# From repo root
git fetch origin
git reset --hard origin/main
export DOCKER_BUILDKIT=1
docker build -f Dockerfile -t pihub:latest .
```

then push image to docker hub:

```
VER=x.x.x
docker tag pihub:latest a1exm/pihub:$VER
docker tag pihub:latest a1exm/pihub:latest
docker push a1exm/pihub:$VER
docker push a1exm/pihub:latest
```

---

## ⚙️ Configuration

| Variable             | Description                                                   | Default / Notes                    |
| -------------------- | ------------------------------------------------------------- | ---------------------------------- |
| `HA_TOKEN`           | HA Long-Lived Access Token                                    | ENV takes priority                 |
| `HA_TOKEN_FILE`      | Path to a file containing the HA Long-Lived Access Token      | Fallback if `HA_TOKEN` not set     |
| `HA_WS_URL`          | Home Assistant WebSocket URL                                  | Defaults to `127.0.0.1`            |
| `HEALTH_HOST`        | Bind address for the HTTP health endpoint                     | Defaults to `0.0.0.0`              |
| `HEALTH_PORT`        | Port for the HTTP health endpoint                             | Defaults to `9123`                 |
| `DEBUG`              | Debug knob                                                    | Defaults to INFO/WARN              |

Keymap is bundled with the application and loaded from packaged assets in production; it is not configurable at runtime.

**Fail-fast:** the app exits on startup if it can’t obtain an HA token from env or file, logging `"[app] Cannot start without Home Assistant token: ..."` to point operators at the missing credential.

---

## 🌡️ Health endpoint

An HTTP endpoint publishes a JSON snapshot at `http://<host>:9123/health`:

```json
{
  "status": "ok",
  "degraded_reasons": [],
  "ws": {
    "connected": true,
    "last_activity": "watch"
  },
  "usb": {
    "receiver_present": true,
    "paired_remote": true,
    "reader_running": true,
    "input_open": true,
    "input_path": "/dev/input/by-id/usb-Logitech_USB_Receiver-if02-event-kbd",
    "grabbed": true
  },
  "ble": {
    "adapter_present": true,
    "advertising": false,
    "connected": true
  }
}
```

This can be polled from Home Assistant via a REST sensor. A degraded status can indicate the HA websocket is unavailable, the USB reader is unhealthy, or BLE/USB pairing state is not ready. A reason is provided.

---

## 🔌 Event Contracts

### PiHub → Home Assistant (events)

PiHub uses the existing **bidirectional** `pihub.cmd` convention and **does not change schema**.

Example:

```json
{"dest": "ha", "text": "media_next"}
```

Volume

```json
{"dest": "ha", "do": "emit", "text": "volume_up", "repeat": true}
```

### Home Assistant → PiHub (commands/state)

* Activity/state: push updates for `input_select.activity` to switch keymap modes
* Commands, e.g.:

**Macro:**

```json
{"dest": "pi", "text": "macro", "name": "power_on"}
```
> Options: **power_on / power_off / return_home**, executed from HA.

**Send a BLE key:**

```json
{"dest": "pi", "text": "ble_key", "usage": "consumer", "code": "menu"}
```
> BLE: per-button may use **consumer or keyboard** usages. 40ms default hold.
Optionally include `"hold_ms": "40"` - values accepted: `0, 40, 80, 100, 500, 2000.`

---

## ⌨️ Input Mapping

* Reads from `/dev/input` Unifying device via `evdev`
* Filters kernel auto-repeat; uses only `down/up` edges
* Falls back to `MSC_SCAN` for stubborn keys
* Maps physical keys → canonical `rem_*` names, then keymap decides action:

  * `emit` → sends WebSocket `{"dest":"ha","text":...}`
  * `ble` → sends BLE Consumer/Keyboard usage
  * Optional `min_hold_ms` "Long Press" - hold for 'X' ms then trigger
  * Optional `repeat` (synthetic; **HA only**)

---

## 🧠 Behavior & Resilience

* **Drop when offline**: commands return `False`; nothing is queued
* **Reconnect with jitter**: automatic, capped backoff
* **Minimal logs**: quiet by default; env-gated debug available
* **Seed-then-subscribe**: fetch current activity once post-connect, then push-only

---

## 🧪 Troubleshooting

* **No input events?** Look for `/dev/input/by-id/*event-kbd` (often `usb-Logitech_USB_Receiver-*event-kbd`); if missing, fall back to `/dev/input/by-path/*-event-kbd`. If no remote is paired/active, there may be no `event-kbd` node even if the receiver is plugged in. Ensure the relevant `/dev/input` paths are bind-mounted read-only into the container.
* **BLE not reacting?** Verify BlueZ DBus socket is present (`/var/run/dbus/system_bus_socket`) and mounted read-only. Use `bluetoothctl` to remove all known devices
* **Offline drops?** Expected by design: when HA WS is down, send paths return `False` and do not crash the process.
* **Token issues?** Confirm `HA_TOKEN` is set (preferred) or `HA_TOKEN_FILE` path is mounted and readable.

---

## 🏗️ Dev Notes

* Built with `aiohttp` (one session, WS pings/clean reconnect)
* Multi-stage Dockerfile for minimal runtime image: `Dockerfile`
