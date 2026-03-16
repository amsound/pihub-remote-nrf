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
* **Bounded local queueing on hot paths**: with reconnect and best-effort recovery; unavailable domains may still drop actions

---

## 🧩 Requirements

* Raspberry Pi 3B+ (tested on **aarch64** Raspberry Pi OS Lite)
* Logitech Unifying receiver (model U-0007 recommended)
* Nordic nRF52840 Dongle  
  `https://www.nordicsemi.com/Products/Development-hardware/nRF52840-Dongle`
* Samsung TV - same VLAN recommended
* Audio Pro Speaker (LinkPlay / Arylic / WiiM may work) - will work across VLANs

---

## 🚀 Quick Start

### docker-compose.yml & prebuilt docker image

```yaml
services:
  pihub:
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
      SPEAKER_IP: "192.168.xx.xx"
#      DEBUG: 1           # Verbose Logging

    volumes:
      - /home/pi/pihub-data:/data
      - /dev/input:/dev/input:ro
      - /dev/input/by-id:/dev/input/by-id:ro
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro

    devices:
      - /dev/serial/by-id/usb-ZEPHYR_USB-DEV_425DAED15E820B58-if00:/dev/ttyACM0

    group_add:
      - dialout

    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
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
| `USB_ENABLED` | enable Logitech Unifying input domain | default `true` |
| `BLE_SERIAL_DEVICE` | CDC ACM device for the BLE dongle | `auto` (prefers `/dev/serial/by-id`, then falls back to `/dev/ttyACM*`) |
| `BLE_SERIAL_BAUD` | BLE serial baud rate | `115200` |
| `BLE_ENABLED` | enable BLE dongle domain | default `true` |
| `HTTP_SERVER_HOST` | Bind address for the HTTP endpoint | `0.0.0.0` |
| `HTTP_SERVER_PORT` | Port for the HTTP endpoint and local commands | `9123` |
| `TV_IP` | Samsung TV IP address | required for TV support |
| `TV_MAC` | Samsung TV MAC address | required for Wake-on-LAN / power-on path |
| `TV_TOKEN_FILE` | Samsung TV token path | `/data/samsungtv-token.txt` |
| `TV_NAME` | Name presented to the Samsung TV | `PiHub Remote` |
| `TV_ENABLED` | enable Samsung TV domain | default `true` |
| `SPEAKER_IP` | LinkPlay/WiiM speaker IP address | optional |
| `SPEAKER_HTTP_SCHEME` | Speaker HTTP scheme | `https` |
| `SPEAKER_ENABLED` | enable Audio Pro speaker domain | default `true` |
| `DEBUG` | Debug knob | defaults to INFO/WARN |

Keymap is bundled with the application and loaded from packaged assets in production; it is not configurable at runtime.

### Current terminology

* **mode** = current active keymap / button behavior set
* **flow** = named local sequence of steps; some steps block (sleep, wait), while dispatch steps are fire-and-continue requests
* **device-state signal** = a live edge emitted by a domain (for example TV on, or speaker entering a listen-capable source/playback state)
* **device-state flow** = a flow triggered from a device-state signal rather than an explicit remote intent
* **last_trigger** = sticky record of the most recent runtime trigger source

### Flow semantics

* a flow takes one snapshot at the start
* when= predicates are evaluated against that start snapshot only
* dispatch means “request/send and continue”
* wait.tv_on / wait.tv_off are the authoritative confirmation steps
* mode is set at the start of the flow
* last_flow is only updated after successful completion

---

## 🌡️ HTTP endpoint

PiHub exposes an HTTP endpoint at:

```text
http://<host>:9123
```

### Health

```text
GET http://<host>:9123/health
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
    "last_trigger": "flow.watch",
    "error": false,
    "last_error": null,
    "last_result": "ok"
  },
  "usb": {
    "status": "ok",
    "configured": true,
    "enabled": true,
    "reasons": [],
    "present": true,
    "path": "/dev/input/by-id/usb-Logitech_USB_Receiver-if02-event-kbd",
    "link_up": true,
    "link_ready": true,
    "error": false,
    "last_error": null,
    "details": {
      "paired_remote": true,
      "reader_running": true,
      "input_open": true,
      "grabbed": true
    }
  },
  "ble": {
    "status": "ok",
    "configured": true,
    "enabled": true,
    "reasons": [],
    "present": true,
    "path": "/dev/ttyACM0",
    "link_up": true,
    "link_ready": true,
    "error": false,
    "last_error": null,
    "details": {
      "transport_open": true,
      "advertising": false,
      "connected": true,
      "proto_report": true,
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
    "configured": true,
    "enabled": true,
    "reasons": [],
    "present": true,
    "link_up": true,
    "link_ready": true,
    "error": false,
    "last_error": null,
    "details": {
      "initialized": true,
      "presence_on": true,
      "presence_source": "ssdp_alive",
      "last_change_age_s": 9,
      "ws_connected": true,
      "token_present": true
    }
  },
  "speaker": {
    "status": "ok",
    "configured": true,
    "enabled": true,
    "reasons": [],
    "present": true,
    "link_up": true,
    "link_ready": true,
    "error": false,
    "last_error": null,
    "details": {
      "reachable": true,
      "connected": true,
      "ready": true,
      "playback_status": null,
      "volume_pct": 30,
      "muted": false,
      "source": "hdmi",
      "last_update_ts": 1773669131,
      "update_age_s": 6
    }
  }
}
```
* domain status may be degraded without degrading overall PiHub status if the condition is informational or non-critical (for example TV presence still unknown during startup)

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

* passive SSDP `NOTIFY` is the primary passive source of truth `ssdp_alive` and `ssdp_byebye`
* one-shot active presence reconcile runs at startup in the background, using M-SEARCH first and HTTP `/dmr` only as fallback
* websocket is a reusable control channel, not the primary source of presence truth

**Important:**
The Samsung websocket is intentionally not auto-closed just because presence becomes false or unknown. This is relied upon for recovery/power-toggle behavior around the recovery window.

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
* device-state idempotence is based on the last logical flow, to avoid flapping / “howling around”. Device-state signals compare against the last successful logical flow (last_flow), not merely the current mode.

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
* set mode watch
* request speaker stop playback if on listen source
* if TV was off at start, request TV power on
* if TV was off at start, sleep 2.0s
* if TV was off at start, request BLE power on
* if TV was off at start, sleep 1.0s
* request speaker volume
* if TV was off at start, sleep 0.5s
* request speaker HDMI source
* if TV was off at start, wait up to 20s for TV on

### `listen`
* set mode listen
* if TV was on at start, request BLE return home
* if TV was on at start, sleep 2.0s
* if TV was on at start, request TV power off
* if TV was on at start, sleep 1.0s
* request speaker volume
* request speaker preset 1
* if TV was on at start, wait up to 20s for TV off

### `power_off`
* set mode power_off
* if TV was on at start, request BLE return home
* if TV was on at start, sleep 2.5s
* if TV was on at start, request TV power off
* if TV was on at start, wait up to 20s for TV off
* if speaker source at start was wifi, airplay, or multiroom-secondary, request speaker stop, sleep 0.5s, then request speaker power off

These are the normal explicit intent flows. Separate device-state flows may also exist for signal-driven behavior such as `listen_signal` or `watch_signal`.

A flow can return `ok: false` with `tv_on_timeout` or `tv_off_timeout` when a required TV wait step does not converge in time.

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
