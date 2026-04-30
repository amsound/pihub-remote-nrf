# PiHub – Universal Remote Bridge (Harmony Remote & Pi)

PiHub turns a Raspberry Pi into a tiny, fast “universal remote” bridge.
It listens to RF key events from a Logitech Harmony Remote (simple type, no display) paired to a Logitech Unifying receiver and sends actions to:

* **BLE HID** (Consumer + Keyboard) tested with Apple TV 4K 3rd Gen
* **Samsung TV**
* **Speaker backends**
  * **Audio Pro / LinkPlay / Arylic / WiiM** via TCP API + HTTP API
  * **Samsung soundbar (local)** via **Google Cast + AirPlay /info**
* **Local runtime flows** over HTTP

It’s lightweight, locally stateful, and tuned for Raspberry Pi 3B+ (aarch64). No Harmony Hub required.

---

## ✨ Features

* **RF → Actions** via Linux `evdev`, mapped to canonical `rem_*` names
* **Local mode authority** with active key bindings selected by PiHub
* **HTTP control surface** on port `9123`, including `/health`, `/dashboard`, `/tools`, and `/settings`
* **BLE Output**: per-button **Consumer + Keyboard** usages
* **TV control** via Samsung WebSocket + SSDP discovery, or Samsung Frame IP Control on port 1516
* **Speaker control** via pluggable speaker backends:
  * **Audio Pro / LinkPlay / WiiM** via local TCP + HTTP control
  * **Samsung soundbar (local)** via Google Cast for control and AirPlay `/info` for listen-state detection
* **Flows**: local named flows such as `watch`, `listen`, `power_off`
* **Device-state signals**: passive state-driven routing from TV/speaker changes into local runtime behavior
* **Precise edges**: explicit **down/up**; filters kernel auto-repeat
* **Long-press** via `min_hold_ms`
* **Bounded local queueing on hot paths**: with reconnect and best-effort recovery; explicit flows fail truthfully when a required backend command cannot be sent through the active local transport; some backend-specific unsupported operations may be intentionally skipped by policy

---

## 🧩 Requirements

* Raspberry Pi 3B+ (tested on **aarch64** Raspberry Pi OS Lite Bookworm)
* Logitech Unifying receiver (model U-0007 recommended)
* Nordic nRF52840 based [dongle](https://www.nordicsemi.com/Products/Development-hardware/nRF52840-Dongle) or an `Ebyte E104-BT5040U` for BLE connectivity
* Samsung Tizen based TV (same VLAN required for SSDP and WoL)
* One supported speaker backend:
  * **Audio Pro speaker** with local TCP/HTTP control
  * **Samsung soundbar** with local Google Cast support and AirPlay `/info` available on the device
* Logitech Unifying receiver and BLE are the core paths
* TV and speaker domains are optional integrations

---

## 🚀 Quick Start

### save docker-compose.yml & use prebuilt docker image

```yaml
services:
  pihub-nrf:
    image: a1exm/pihub-nrf:latest
    container_name: pihub-nrf
    init: true
    cpu_shares: 2048
    network_mode: host
    restart: unless-stopped

    device_cgroup_rules:
      - 'c 13:* r'

    environment:
      TV_IP: "192.168.xx.xx"
      TV_MAC: "xx:xx:xx:xx:xx:xx"

      # Samsung Frame IP Control backend alternative:
      # TV_FRAME_IP: "192.168.xx.xx"
      # TV_FRAME_TOKEN_FILE: "/data/samsung-frame-token.txt"

      # Speaker backend selection:
      # audiopro
      # samsung_soundbar
      SPEAKER_BACKEND: "samsung_soundbar"
      SPEAKER_IP: "192.168.xx.xx"

      # DEBUG: 1           # Verbose Logging
    volumes:
      - /home/pi/pihub-data:/data
      - /dev/input:/dev/input:ro
      - /etc/localtime:/etc/localtime:ro
    devices:
      - /dev/ttyACM0:/dev/ttyACM0
    group_add:
      - dialout

    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

`/data` is used for persistent tokens and state. 

Examples:

* Samsung TV token: `/data/samsungtv-token.txt`
* Samsung Frame IP Control token: `/data/samsung-frame-token.txt`

The local Samsung soundbar backend does not require any speaker-side tokens.

If the BLE dongle is not attached, remove or comment out the `/dev/ttyACM0` device mapping. Docker cannot mount a device path that does not exist on the host.

Start with:

```bash
docker compose up -d
````

---

## ⚙️ Configuration

| Variable | Description | Default / Notes |
| --- | --- | --- |
| `BLE_SERIAL_DEVICE` | CDC ACM device for the BLE dongle | `auto` (prefers `/dev/serial/by-id`, then falls back to `/dev/ttyACM*`) |
| `BLE_SERIAL_BAUD` | BLE serial baud rate | `115200` |
| `HTTP_SERVER_HOST` | Bind address for the HTTP endpoint | `0.0.0.0` |
| `HTTP_SERVER_PORT` | Port for the HTTP endpoint and local commands | `9123` |
| `TV_IP` | Samsung TV IP address | required for legacy Samsung TV support |
| `TV_MAC` | Samsung TV MAC address | required for legacy Wake-on-LAN / power-on path |
| `TV_TOKEN_FILE` | Samsung TV websocket token path | `/data/samsungtv-token.txt` |
| `TV_NAME` | Name presented to the Samsung TV websocket | `PiHub Remote` |
| `TV_FRAME_IP` | Samsung Frame IP Control address | when set, selects the Frame IP Control backend instead of `TV_IP`/`TV_MAC` |
| `TV_FRAME_TOKEN_FILE` | Samsung Frame IP Control access-token path | `/data/samsung-frame-token.txt` |
| `TV_ENABLED` | enable Samsung TV domain | default `true` |
| `SPEAKER_BACKEND` | speaker backend selection | `audiopro` or `samsung_soundbar`; default `audiopro` |
| `SPEAKER_IP` | speaker IP address | required for `audiopro` and `samsung_soundbar` |
| `SPEAKER_ENABLED` | enable speaker domain | default `true` |
| `DEBUG` | Debug knob | defaults to INFO/WARN |

Keymap is bundled with the application and loaded from packaged assets in production; it is not configurable at runtime.

## 🖼️ Samsung Frame IP Control backend

For newer Samsung Frame TVs exposing IP Remote on port `1516`, set `TV_FRAME_IP` instead of `TV_IP`/`TV_MAC`.

The backend uses Samsung IP Control G2 over HTTPS JSON-RPC and currently exposes a deliberately narrow surface:

* discreet `powerControl` on/off
* `inputSourceControl` for HDMI1
* `remoteKeyControl` for `return`
* boot/reconcile polling for power and active source

Create the token once with the TV on and save it to `TV_FRAME_TOKEN_FILE`:

```bash
curl -k -X POST https://<tv-ip>:1516 \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  --data '{"method":"createAccessToken","id":"1","jsonrpc":"2.0"}'
```

Accept the prompt on the TV, then write `result.AccessToken` into `/data/samsung-frame-token.txt`.

## 🔊 Local Samsung soundbar backend

For:
SPEAKER_BACKEND=samsung_soundbar

* PiHub uses two local surfaces on the soundbar:
	*	Google Cast for:
	*	volume
	*	mute
	*	stop/interruption behavior
	*	AirPlay /info for:
	*	local detection of whether an AirPlay session is active

* Current intent of this backend:
	*	keep speaker control fully local
	*	avoid SmartThings/cloud dependencies
	*	provide reliable local volume control
	*	detect when the soundbar is actively being used for AirPlay/listen behavior

Notes:
	*	stop_playback is implemented by launching the default Google Cast receiver app to interrupt active AirPlay playback
	*	this backend is intentionally narrower than a full source-control integration
	*	some generic speaker actions may be intentionally skipped when the backend does not expose a meaningful local primitive

---

## 🌡️ HTTP endpoint

PiHub exposes an HTTP endpoint at:

```text
http://<host>:9123
```

The rest of the HTTP, flow, input-mapping, and troubleshooting behaviour is unchanged from the legacy Samsung TV backend.
