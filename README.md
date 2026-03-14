# PiHub – Universal Remote Bridge (Harmony Remote & Pi)

PiHub turns a Raspberry Pi into a tiny, fast “universal remote” bridge.
It listens to RF key events from a Logitech Harmony Remote (simple type, no display) paired to a Logitech Unifying receiver and sends actions to:

* **BLE HID** (Consumer + Keyboard) tested with Apple TV 4K 3rd Gen
* **Samsung TV**
* **LinkPlay/WiiM speaker**
* **Local runtime flows** over HTTP

It’s lightweight, stateless, and tuned for **Raspberry Pi 3B+ (aarch64)**. No Harmony Hub or cloud required.

---

## ✨ Features

* **RF → Actions** via Linux `evdev`, mapped to canonical `rem_*` names
* **Local mode authority** with active key bindings selected by PiHub
* **HTTP control surface** on port `9123`
* **BLE Output**: per-button **Consumer + Keyboard** usages
* **TV control** via Samsung WebSocket + SSDP discovery
* **Speaker control** via LinkPlay TCP + HTTP
* **Flows**: local named flows such as `watch`, `listen`, `power_off`
* **Device-state signals**: passive state-driven routing from TV/speaker changes into local runtime behavior
* **Precise edges**: explicit **down/up**; filters kernel auto-repeat
* **Long-press** via `min_hold_ms`
* **No queueing**: drops actions when offline/unavailable; reconnects locally as needed

---

## 🧩 Requirements

* Raspberry Pi 3B+ (tested on **aarch64** Raspberry Pi OS Lite)
* Logitech Unifying receiver (model U-0007 recommended)
* Nordic nRF52840 Dongle  
  `https://www.nordicsemi.com/Products/Development-hardware/nRF52840-Dongle`
* Samsung TV on the local network
* LinkPlay / WiiM speaker on the local network (optional)

---

## 🚀 Quick Start

### docker-compose.yml & prebuilt docker image

```yaml
services:
  pihub-nrf:
    image: a1exm/pihub-nrf:latest
    network_mode: host
    restart: unless-stopped
    cpu_shares: 2048
    device_cgroup_rules:
      - 'c 13:* rmw'
    environment:
      TV_IP: "192.168.90.43"
      TV_MAC: "28:07:08:97:42:c8"
      SPEAKER_IP: "192.168.70.43"
      # DEBUG: 1                   # optional for debug chatter
    volumes:
      - /dev/input:/dev/input:ro
      - /dev/input/by-id:/dev/input/by-id:ro
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
      - ./data:/data
    devices:
      - /dev/serial/by-id/usb-ZEPHYR_USB-DEV_425DAED15E820B58-if00:/dev/ttyACM0
    group_add:
      - dialout
```

Start with:

```bash
docker compose up -d
```

### Build then push to docker hub (personal reminder)

```bash
# From repo root
git fetch origin
git reset --hard origin/main
export DOCKER_BUILDKIT=1
docker build -f Dockerfile -t pihub-nrf:latest .
```

Then push image to Docker Hub:

```bash
VER=x.x.x
docker tag pihub:latest a1exm/pihub-nrf:$VER
docker tag pihub:latest a1exm/pihub-nrf:latest
docker push a1exm/pihub-nrf:$VER
docker push a1exm/pihub-nrf:latest
```

---

## ⚙️ Configuration

| Variable | Description | Default / Notes |
| --- | --- | --- |
| `BLE_SERIAL_DEVICE` | CDC ACM device for the BLE dongle | `auto` (prefers `/dev/serial/by-id`, then falls back to `/dev/ttyACM*`) |
| `BLE_SERIAL_BAUD` | BLE serial baud rate | `115200` |
| `HEALTH_HOST` | Bind address for the HTTP endpoint | `0.0.0.0` |
| `HEALTH_PORT` | Port for health and local commands | `9123` |
| `TV_IP` | Samsung TV IP address | required for TV support |
| `TV_MAC` | Samsung TV MAC address | required for Wake-on-LAN / power-on path |
| `TV_TOKEN_FILE` | Samsung TV token path | `/data/samsungtv-token.txt` |
| `TV_NAME` | Name presented to the Samsung TV | `PiHub Remote` |
| `SPEAKER_IP` | LinkPlay/WiiM speaker IP address | optional |
| `SPEAKER_HTTP_SCHEME` | Speaker HTTP scheme | `https` |
| `DEBUG` | Debug knob | defaults to INFO/WARN |

Keymap is bundled with the application and loaded from packaged assets in production; it is not configurable at runtime.

### Current terminology

* **mode** = current active keymap / button behavior set
* **flow** = named blocking orchestration, e.g. `watch`, `listen`, `power_off`
* **device-state signal** = a live edge emitted by a domain (for example TV on, or speaker entering a listen-capable source/playback state)
* **device-state flow** = a flow triggered from a device-state signal rather than an explicit remote intent
* **last_trigger** = sticky record of the most recent runtime trigger source

---

## 🌡️ HTTP endpoint

PiHub exposes an HTTP endpoint at:

```text
http://<host>:9123
```

### Health

```text
GET /health
```

Example response:

```json
{
  "pihub_id": "test-pihub",
  "status": "ok",
  "degraded_reasons": [],
  "domains": {
    "usb": "ok",
    "ble": "ok",
    "tv": "ok",
    "speaker": "ok"
  },
  "runtime": {
    "mode": "watch",
    "last_flow": "watch",
    "flow_running": false,
    "last_trigger": "http.flow"
  },
  "usb": {
    "status": "ok",
    "reasons": [],
    "present": true,
    "path": "/dev/input/by-id/usb-Logitech_USB_Receiver-if02-event-kbd",
    "link_up": true,
    "link_ready": true,
    "error": false,
    "details": {
      "paired_remote": true,
      "reader_running": true,
      "input_open": true,
      "grabbed": true
    }
  },
  "ble": {
    "status": "ok",
    "reasons": [],
    "present": true,
    "path": "/dev/ttyACM0",
    "link_up": true,
    "link_ready": true,
    "error": false,
    "details": {
      "advertising": false,
      "connected": true,
      "last_disc_reason": null,
      "conn_params": {
        "interval_ms": 15,
        "latency": 0,
        "timeout_ms": 3000
      }
    }
  },
  "tv": {
    "status": "ok",
    "reasons": [],
    "present": true,
    "link_up": true,
    "link_ready": true,
    "error": false,
    "details": {
      "initialized": true,
      "presence_on": true,
      "presence_source": "msearch",
      "last_change_age_s": 2,
      "ws_connected": true,
      "token_present": true,
      "last_error": ""
    }
  },
  "speaker": {
    "status": "ok",
    "reasons": [],
    "present": true,
    "link_up": true,
    "link_ready": true,
    "error": false,
    "details": {
      "connected": true,
      "playback_status": null,
      "volume_pct": 30,
      "muted": false,
      "source": "hdmi",
      "last_update_ts": 1773052406,
      "update_age_s": 57
    }
  }
}
```

### Commands accepted over HTTP

PiHub currently accepts three local command forms:

#### Run a flow

```text
POST /flow/run/{name}
```

Examples:

```bash
curl -X POST http://pihub.local:9123/flow/run/watch
curl -X POST http://pihub.local:9123/flow/run/listen
curl -X POST http://pihub.local:9123/flow/run/power_off
```

Optional JSON body:

```json
{ "trigger": "http.browser" }
```

#### Set mode directly

```text
POST /mode/set/{name}
```

Examples:

```bash
curl -X POST http://pihub.local:9123/mode/set/watch
curl -X POST http://pihub.local:9123/mode/set/listen
curl -X POST http://pihub.local:9123/mode/set/power_off
```

Optional JSON body:

```json
{ "trigger": "http.browser" }
```

#### Universal command endpoint

```text
POST /command
```

JSON body format:

```json
{
  "domain": "flow",
  "action": "run",
  "args": {
    "name": "watch",
    "trigger": "http.command"
  }
}
```

Supported runtime commands today:

##### Run a flow

```json
{
  "domain": "flow",
  "action": "run",
  "args": {
    "name": "watch",
    "trigger": "http.command"
  }
}
```

##### Set mode

```json
{
  "domain": "mode",
  "action": "set",
  "args": {
    "name": "listen",
    "trigger": "http.command"
  }
}
```

---

## ⌨️ Input Mapping

* Reads from `/dev/input` Unifying device via `evdev`
* Filters kernel auto-repeat; uses only `down/up` edges
* Falls back to `MSC_SCAN` for stubborn keys
* Maps physical keys → canonical `rem_*` names, then keymap decides action
* Top-level remote mode buttons are bound to local flows, not external automation
* `min_hold_ms` supports long-press flow triggering
* Synthetic repeat is limited to physical volume keys

Keymap concepts:

* `scancode_map` maps raw scan codes → canonical `rem_*` names
* `modes` selects the active binding set
* actions currently support:
  * `flow`
  * `ble`
  * `tv`
  * `speaker`
  * `noop`

---

## 🔀 Startup and device-state behavior

### Startup

Startup is intentionally conservative:

* PiHub always starts in **`power_off`** mode
* It does **not** run any flows on boot
* It does **not** mutate device state on boot
* `last_trigger` is set to `startup_reconcile`

This avoids boot-time races and lets late device truth arrive safely.

### TV discovery

TV presence is determined using:

* passive SSDP `NOTIFY`
* one-shot startup `M-SEARCH` burst

If the TV is already on at boot, PiHub sends a short `M-SEARCH` burst and may set:

* `presence_on: true`
* `presence_source: "msearch"`

Later passive updates may overwrite the source with:

* `presence_source: "ssdp_alive"`
* `presence_source: "ssdp_byebye"`

### Device-state signals

PiHub also reacts to live device-state signals emitted by domains.

Current signal sources:

* TV logical off → on emits a `watch` device-state signal
* speaker entering a listen-capable state emits a `listen` device-state signal

Current listen-capable speaker sources:

* `airplay`
* `wifi`
* `multiroom-secondary`

These signals are edge-triggered and intended to behave more like live state changes than periodic polling.

Routing behavior:

* explicit remote intent flows remain authoritative and may always be run again
* device-state signals are routed through runtime and may trigger dedicated device-state flows
* device-state signals are suppressed while another sequence is already running
* device-state idempotence is based on the last logical flow, to avoid flapping / “howling around”

Logical activity normalization:

* `listen` and `listen_signal` both normalize to logical last flow `listen`
* `watch` and `watch_signal` both normalize to logical last flow `watch`

---

## 🧠 Flows

Current named flows:

* `watch`
* `listen`
* `power_off`

Current intent:

### `watch`
* set mode `watch`
* if TV was off, power it on
* wait for TV on, then allow extra settle time for CEC handshake
* power on BLE target
* set speaker source to HDMI
* set speaker volume to 30

### `listen`
* capture whether TV was on
* set mode `listen`
* if TV was on, power it off
* if TV was on, return BLE target home
* set speaker preset 1
* set speaker volume to 20

### `power_off`
* capture whether TV was on
* set mode `power_off`
* if TV was on, return BLE target home
* wait 3 seconds
* if TV was on, power TV off

These are the normal explicit intent flows. Separate device-state flows may also exist for signal-driven behavior such as `listen_signal` or `watch_signal`.

---

## 🧪 Troubleshooting

* **No input events?** Look for `/dev/input/by-id/*event-kbd` (often `usb-Logitech_USB_Receiver-*event-kbd`). Ensure the relevant `/dev/input` paths are bind-mounted read-only into the container.
* **TV already on at boot but mode stays `power_off`?** Check `/health` for `tv.details.presence_on` and `presence_source`. Startup remains conservative until an explicit flow or later device-state signal acts.
* **TV discovery confusion?** `presence_source` shows the most recent TV discovery source, not the current mode source of truth.
* **No device-state flow action?** Check whether the same logical flow already ran recently, or whether another sequence was already active and the signal was ignored as busy.
* **Mode changed but `last_flow` is null?** That is expected when mode changed by startup reconcile or direct mode set rather than by a successfully completed flow.

---

## 🏗️ Dev Notes

* Built with `aiohttp`
* Local-only control plane; Home Assistant WebSocket support has been removed
* Runtime is the authority for:
  * current mode
  * last flow
  * sticky last trigger
* Dispatcher owns key bindings and hot-path action dispatch
