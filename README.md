# PiHub – Universal Remote Bridge (Harmony Remote & Pi)

PiHub turns a Raspberry Pi into a tiny, fast “universal remote” bridge.
It listens to RF key events from a Logitech Harmony Remote (simple type, no display) paired to a Logitech Unifying receiver and sends actions to:

* **BLE HID** (Consumer + Keyboard) tested with Apple TV 4K 3rd Gen
* **Samsung TV**
* **Speaker backends**
  * **Audio Pro / LinkPlay / Arylic / WiiM** via TCP API + HTTP API
  * **Samsung soundbar** via **SmartThings cloud API**
* **Local runtime flows** over HTTP

It’s lightweight, locally stateful, and tuned for Raspberry Pi 3B+ (aarch64). No Harmony Hub or cloud required.

---

## ✨ Features

* **RF → Actions** via Linux `evdev`, mapped to canonical `rem_*` names
* **Local mode authority** with active key bindings selected by PiHub
* **HTTP control surface** on port `9123`, including `/health`, `/dashboard`, `/tools`, and `/settings`
* **BLE Output**: per-button **Consumer + Keyboard** usages
* **TV control** via Samsung WebSocket + SSDP discovery
* **Speaker control** via pluggable speaker backends:
  * **Audio Pro / LinkPlay / WiiM** via local TCP + HTTP API
  * **Samsung soundbar** via SmartThings cloud API
* **Flows**: local named flows such as `watch`, `listen`, `power_off`
* **Device-state signals**: passive state-driven routing from TV/speaker changes into local runtime behavior
* **Precise edges**: explicit **down/up**; filters kernel auto-repeat
* **Long-press** via `min_hold_ms`
* **Bounded local queueing on hot paths**: with reconnect and best-effort recovery; explicit flows now fail truthfully when important domain actions cannot be sent

---

## 🧩 Requirements

* Raspberry Pi 3B+ (tested on **aarch64** Raspberry Pi OS Lite Bookworm)
* Logitech Unifying receiver (model U-0007 recommended)
* Nordic nRF52840 Dongle `https://www.nordicsemi.com/Products/Development-hardware/nRF52840-Dongle` for BLE
* Samsung TV (same VLAN recommended for SSDP)
* One supported speaker backend:
  * **Audio Pro / LinkPlay / Arylic / WiiM** speaker - local TCP/HTTP control, should work across VLANs
  * **Samsung soundbar** with SmartThings support enabled - cloud control via SmartThings API

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
      # Speaker backend selection:
      SPEAKER_BACKEND: "audiopro"
      SPEAKER_IP: "192.168.xx.xx"

      # Samsung SmartThings soundbar
      # SMARTTHINGS_DEVICE_ID: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

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
* SmartThings token file: `/data/smartthings-token.json`

For the Samsung SmartThings speaker backend, PiHub expects a token file at `/data/smartthings-token.json` containing an access token, refresh token, expiry, and SmartApp client credentials. PiHub will refresh the SmartThings access token automatically when needed.

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
| `TV_IP` | Samsung TV IP address | required for TV support |
| `TV_MAC` | Samsung TV MAC address | required for Wake-on-LAN / power-on path |
| `TV_TOKEN_FILE` | Samsung TV token path | `/data/samsungtv-token.txt` |
| `TV_NAME` | Name presented to the Samsung TV | `PiHub Remote` |
| `TV_ENABLED` | enable Samsung TV domain | default `true` |
| `SPEAKER_BACKEND` | speaker backend selection | `audiopro` or `samsung_soundbar`; default `audiopro` |
| `SPEAKER_IP` | Audio Pro / LinkPlay / WiiM speaker IP address | required for `audiopro` backend |
| `SMARTTHINGS_DEVICE_ID` | SmartThings Samsung soundbar device ID | required for `samsung_soundbar` backend |
| `SMARTTHINGS_TOKEN_FILE` | SmartThings token path | `/data/smartthings-token.json` |
| `SMARTTHINGS_POLL_INTERVAL_S` | Samsung soundbar background refresh interval | default `30` |
| `SPEAKER_ENABLED` | enable speaker domain | default `true` |
| `DEBUG` | Debug knob | defaults to INFO/WARN |

Keymap is bundled with the application and loaded from packaged assets in production; it is not configurable at runtime.

### SmartThings token file

For `SPEAKER_BACKEND=samsung_soundbar`, PiHub uses a SmartThings token file stored at:

```text
/data/smartthings-token.json
```

Preferred file shape:

```json
{
  "access_token": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "refresh_token": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "expires_at": 1774170582,
  "client_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "client_secret": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

PiHub accepts this flat PiHub-owned format and will automatically refresh the SmartThings access token when needed.

Legacy nested token files from other integrations may still be accepted for compatibility, but the flat format above is the canonical PiHub format.

---

## 🌡️ HTTP endpoint

PiHub exposes an HTTP endpoint at:

```text
http://<host>:9123
```

### Web UI pages

PiHub also exposes a small built-in web UI on the same HTTP port:

```text
http://<host>:9123/dashboard
http://<host>:9123/tools
http://<host>:9123/settings
```

Current pages:

* `/dashboard` — high-level live status view for PiHub, including runtime mode/flow state, domain health, and system information
* `/tools` — operator page for manually running flows, setting modes, refreshing networked domains, and restarting PiHub
* `/settings` — local runtime settings for speaker levels and listen-target behavior
* `/health` — raw JSON health/status payload

These pages are intended as lightweight local operator tools rather than a full external control surface.

### Health

```text
GET http://<host>:9123/health
```

<details>
  <summary>Example json response:</summary>

  ```json
  {
    "pihub_id": "living-room-pihub",
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
        "initialised": true,
        "presence_on": true,
        "presence_source": "ssdp_alive",
        "last_change_age_s": 4008,
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
      "backend": "audiopro",
      "reachable": true,
      "connected": true,
      "ready": true,
      "playback_status": null,
      "volume_pct": 28,
      "muted": false,
      "source": "hdmi",
      "last_update_ts": 1773866846,
      "update_age_s": 34
    }
    "system": {
      "hostname": "living-room-pihub",
      "primary_ip": "192.168.90.42",
      "system_uptime_s": 102721,
      "system_uptime_human": "1d 04:32:01",
      "process_uptime_s": 14684,
      "process_uptime_human": "04:04:44",
      "cpu_temp_c": 53.7,
      "throttling": {
        "available": false,
        "raw": null,
        "status": "unknown"
      },
      "load": {
        "1m": 0.08,
        "5m": 0.04,
        "15m": 0.06
      },
      "memory": {
        "total_bytes": 950181888,
        "available_bytes": 687480832,
        "used_bytes": 262701056,
        "total_human": "906.2 MB",
        "available_human": "655.6 MB",
        "used_human": "250.5 MB"
      },
      "disk": {
        "path": "/",
        "total_bytes": 62437576704,
        "used_bytes": 3164504064,
        "free_bytes": 56082489344,
        "total_human": "58.1 GB",
        "used_human": "2.9 GB",
        "free_human": "52.2 GB"
      }
    }
  }
  ```
</details>

* Speaker `details` vary slightly by backend. For example, the Samsung SmartThings backend also reports fields such as `power_on`, `raw_input_source`, `sound_from`, and `listen_active`.
* Domain status may be degraded without degrading overall PiHub status if the condition is informational or non-critical (for example TV presence still unknown during startup)

---

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

#### Refresh networked domains

```text
POST /refresh/tv
POST /refresh/speaker
POST /refresh/networked
````

Examples:

```bash
curl -X POST http://pihub.local:9123/refresh/tv
curl -X POST http://pihub.local:9123/refresh/speaker
curl -X POST http://pihub.local:9123/refresh/networked
```

These endpoints trigger an immediate best-effort refresh of the relevant networked domain state.

This is particularly useful with the Samsung SmartThings speaker backend, where external automation such as Home Assistant may send PiHub a local refresh hint after receiving upstream cloud push updates.

That’s now an important part of the actual deployment shape.

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

Current listen-capable speaker behavior depends on backend:

* **Audio Pro / LinkPlay / WiiM backend**
  * listen-capable sources include `airplay`, `wifi`, and `multiroom-secondary`
  * a `listen` device-state signal is emitted when the speaker enters one of those sources with active playback truth

* **Samsung SmartThings backend**
  * `listen` is derived from SmartThings speaker state
  * Samsung AirPlay/listen mode is inferred from cloud state such as soundbar power and source/sound-from details
  * Samsung speaker commands that are not supported by the public SmartThings API may be treated as unsupported or no-op

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

## Current terminology

* **mode** = current active keymap / button behavior set
* **flow** = named local sequence of ordered steps; some steps block, while dispatch steps send work at a specific point in the sequence and settle later before final flow completion
* **device-state signal** = a live edge emitted by a domain (for example TV on, or speaker entering a listen-capable source/playback state)
* **device-state flow** = a flow triggered from a device-state signal rather than an explicit remote intent
* **last_trigger** = sticky record of the most recent runtime trigger source

### Flow semantics

* a flow takes one snapshot at the start
* `when=` predicates are evaluated against that start snapshot only
* `dispatch` means “request/send at this point in the sequence, then continue”
* dispatch outcomes are still awaited before the final flow result is returned
* a strict step failure does not necessarily stop the flow immediately; later steps may still run
* the overall flow result is failed if important steps failed
* mode is committed only after successful flow completion
* `last_flow` is only updated after successful completion

## 🧠 Flows

Current named flows:

* `watch`
* `listen`
* `power_off`

Current intent:

### `watch`
* if speaker source at start was a listen source, request speaker stop playback
* if TV was off at start, request TV power on
* if TV was off at start, sleep 2.0s
* if TV was off at start, request BLE power on macro
* if TV was off at start, sleep 1.0s
* request speaker volume
* if TV was off at start, sleep 0.5s
* request speaker HDMI source
* on success, commit mode `watch`

### `listen`
* if TV was on at start, request BLE return home macro
* if TV was on at start, sleep 2.0s
* if TV was on at start, request TV power off
* if TV was on at start, sleep 1.0s
* request speaker volume
* request speaker listen target
* on success, commit mode `listen`

The exact effect of `speaker listen target` depends on backend:

* **Audio Pro / LinkPlay / WiiM**: may resolve to a native preset or configured stream URL
* **Samsung SmartThings soundbar**: listen-target style calls are intentionally ignored; Samsung listen mode is inferred from state rather than driven by a preset/stream primitive

### `power_off`
* if TV was on at start, request BLE return home macro
* if TV was on at start, sleep 2.5s
* if TV was on at start, request TV power off
* if speaker source at start was wifi, airplay, or multiroom-secondary, request speaker stop, sleep 0.5s, then request speaker power off
* on success, commit mode `power_off`

These are the normal explicit intent flows. Separate device-state flows may also exist for signal-driven behavior such as `listen_signal` or `watch_signal`.

A flow can return `ok: false` when important domain steps fail, for example if BLE is unavailable, speaker commands cannot be sent, the Samsung TV token is missing, or a bounded TV power command does not succeed in time.

---

## 🧪 Troubleshooting

* **No input events?** Look for `/dev/input/by-id/*event-kbd` (often `usb-Logitech_USB_Receiver-*event-kbd`). Ensure the relevant `/dev/input` paths are bind-mounted read-only into the container.
* **TV flow steps fail immediately with `tv_token_missing`?** That is expected. Explicit TV power commands inside flows now require a saved Samsung TV token. First-time pairing/bootstrap should be done separately with the TV on and correctly configured network details.
* **TV already on at boot but mode stays `power_off`?** Check `/health` for `tv.details.presence_on` and `presence_source`. Startup remains conservative until an explicit flow or later device-state signal acts.
* **TV discovery confusion?** `presence_source` shows the most recent TV discovery source, not the current mode source of truth.
* **No device-state flow action?** Check whether the same logical flow already ran recently, or whether another sequence was already active and the signal was ignored as busy.
* **Mode changed but `last_flow` is null?** That is expected when mode changed by startup reconcile or direct mode set rather than by a successfully completed flow.
* **Samsung soundbar state looks stale or blank?** Ensure the SmartThings backend is pulling the live status endpoint, not a less-current device metadata payload. If `power_on` is parsed as false, PiHub intentionally clears source-derived Samsung fields such as `source`, `raw_input_source`, `sound_from`, and `listen_active`.
* **Samsung soundbar refreshes feel slow?** `POST /refresh/speaker` triggers an immediate best-effort speaker refresh. In practice this works well when paired with an external refresh hint source such as a Home Assistant automation reacting to SmartThings entity changes.
* **Samsung soundbar transport commands fail?** Some Samsung-specific playback capabilities exposed in SmartThings metadata may still be restricted by the public API and can return `403` even though they appear available in Samsung’s own apps.
* **TV watching volume feels slow on Samsung soundbar backend?** If the TV is on, the Samsung soundbar backend will send volume/mute to the Samsung TV integration instead of using slower SmartThings cloud volume commands.

---

## 🏗️ Dev Notes

* Built with `aiohttp`
* Local-only control plane
* Runtime is the authority for:
  * current mode
  * last flow
  * sticky last trigger
* Dispatcher owns key bindings and hot-path action dispatch

* Build then push to docker hub

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
``