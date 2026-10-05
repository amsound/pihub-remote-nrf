# PiHub – Universal Remote Bridge (Harmony Remote & Pi)

PiHub turns a Raspberry Pi into a tiny, fast “universal remote” bridge.
It listens to RF key events from a Logitech Harmony Remote (simple type, no display) paired to a Logitech Unifying receiver and sends actions to:

* **BLE HID** (Consumer + Keyboard) tested with Apple TV 4K 3rd Gen
* **Samsung TV**
* **Speaker backends**
  * **Audio Pro / LinkPlay / Arylic / WiiM** via TCP API + HTTP API
  * **Samsung soundbar (local)** via **Google Cast + AirPlay mDNS**
* **Local runtime flows** over HTTP

It’s lightweight, locally stateful, and tuned for Raspberry Pi 3B+ (aarch64). No Harmony Hub required.

---

## ✨ Features

* **RF → Actions** via Linux `evdev`, mapped to canonical `rem_*` names
* **Local mode authority** with active key bindings selected by PiHub
* **HTTP control surface** on port `9123`: pages (`/status`, `/remote`, `/settings`, `/history`) and a JSON API under `/api/`
* **BLE Output**: per-button **Consumer + Keyboard** usages
* **TV control** via Samsung WebSocket + SSDP discovery, or Samsung Frame IP Control on port 1516
* **Speaker control** via pluggable speaker backends:
  * **Audio Pro / LinkPlay / WiiM** via local TCP + HTTP control
  * **Samsung soundbar (local)** via Google Cast for control and playback, and AirPlay mDNS announcements for AirPlay detection
* **Backend-aware flows**: local named flows such as `watch`, `listen`, and `power_off`, with behaviour selected automatically from the active speaker backend
* **Device-state signals**: passive state-driven routing from TV/speaker/Apple TV AirPlay changes into local runtime behavior
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
  * **Samsung soundbar** with local Google Cast support (AirPlay optional, detected over mDNS)
* Logitech Unifying receiver and BLE are the core paths
* TV and speaker domains are optional integrations

---

## 🚀 Quick Start

### save docker-compose.yml & use prebuilt docker image

```yaml
services:
  pihub-nrf:
    image: ghcr.io/amsound/pihub-remote-nrf:latest
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
| `SPEAKER_BACKEND` | speaker backend and flow-profile selection | `audiopro` or `samsung_soundbar`; default `audiopro` |
| `SPEAKER_IP` | speaker IP address | required for `audiopro` and `samsung_soundbar` |
| `KNOWN_SPEAKER_IPS` | Audio Pro peers checked when leaving a native multiroom group (comma-separated) | defaults to the original house's three speakers |
| `SPEAKER_ENABLED` | enable speaker domain | default `true` |
| `APPLE_TV_IP` | Static Apple TV IP used for AirPlay mDNS session detection | empty disables Apple TV AirPlay domain |
| `APPLE_TV_AIRPLAY_ENABLED` | enable Apple TV AirPlay session detector | default `true` |
| `APPLE_TV_AIRPLAY_DEBOUNCE_S` | debounce before emitting `watch` from Apple TV AirPlay session | default `2.5` |
| `ROOM_NAME` | Room name shown on the web pages | defaults from the hostname (`living-room-pihub` → Living Room) |
| `ROOMS` | Rooms in the Status page's room strip: `Name=host,Name=host` | e.g. `Living Room=192.168.90.42,Kitchen=192.168.90.44,Office=192.168.90.46` |
| `DEBUG` | Debug knob | defaults to INFO/WARN |

Keymap is bundled with the application and loaded from packaged assets in production; it is not configurable at runtime.

## 🖼️ Samsung Frame IP Control backend

For newer Samsung Frame TVs exposing IP Remote on port `1516`, set `TV_FRAME_IP` instead of `TV_IP`/`TV_MAC`.

The backend uses Samsung IP Control G2 over HTTPS JSON-RPC and keeps to a deliberately narrow surface:

* discrete `powerControl` on and off, each verified by reading the power back
* reading the power state: at start-up, before every flow, and whenever the TV announces itself on the network
  (SSDP). It is not polled
* `inputSourceControl`: straight after a Watch flow switches the TV on (never when the TV was already on),
  the input is read, and only if it is not HDMI1 is the TV told to switch, which is then verified by reading
  it back. The TV shows its input banner on every switch command, even to the input it is already on
* reading the active input source at start-up (reported by `POST /api/refresh/tv`)

It sends no remote keys.

Create the token once with the TV on and save it to `TV_FRAME_TOKEN_FILE`:

```bash
curl -k -X POST https://<tv-ip>:1516 \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  --data '{"method":"createAccessToken","id":"1","jsonrpc":"2.0"}'
```

Accept the prompt on the TV, then write `result.AccessToken` into `/data/samsung-frame-token.txt`.

## 🔊 Local Samsung soundbar backend

For `SPEAKER_BACKEND=samsung_soundbar`. Everything is local; no SmartThings or cloud.

* **Google Cast** (port 8009, connected directly by IP with no discovery polling):
  volume, mute, playback of stream slots, stop, and following what the soundbar is playing.
* **AirPlay detection** from the soundbar's `_airplay._tcp` mDNS announcements
  (their `flags` value). The AirPlay port is not fixed on this soundbar and changes
  over time, so pihub never uses it directly and only reads the announcements.
* **Stream slots:** remote keys 1–9 and 0 play `soundbar_stream_url_1..10`, set on
  the Settings page. The Listen flow plays one of them. TuneIn stations and HLS
  playlists go through the local [restreamer](https://github.com/amsound/restreamer)
  (tick **Restream**); it must run on the same host as pihub, on port 8000.
* **Wiring:** the TV's sound reaches the soundbar over **optical**, with no HDMI between
  them. The soundbar has no input command: it plays the TV (D.IN) whenever nothing is
  using its network input.
* **Handing the soundbar to the TV** (`release_to_tv`, used by Watch, Power off and the
  stop key): a Cast app that is open is stopped and quit, and the bar drops to D.IN.
  AirPlay cannot be told to stop, so the default Cast receiver is opened, which takes
  the audio from it, and then quit. With neither, nothing is sent.
* **Source shown:** AirPlay, Wi-Fi (a Cast app is open) or Optical (neither).

---

## 🌡️ HTTP endpoint

PiHub exposes an HTTP endpoint at:

```text
http://<host>:9123
```

### Web UI pages

Plain static pages in `pihub/web/` (no build step), following the system light/dark setting:

* `/status`: one screen with mode, last trigger, system vitals, TV and speaker cards, Apple TV and remote,
  flow/mode/maintenance buttons, recent flows, and the other rooms (`ROOMS`)
* `/remote`: phone remote: Off / Listen / Watch, touch pad, volume. Keys are taps
* `/settings`: volumes, what Listen plays, stream slots (per speaker backend)
* `/history`: recent flows with their steps, and warnings/errors

### HTTP API

Pages live at the top level; everything a program calls is under `/api/` (JSON).

| Call | What it does |
|---|---|
| `GET /api/status` | Compact status: `mode`, last flow/trigger/result, `tv`, `speaker`, `apple_tv`, `remote`, `system`. Times (`*_at`) are Unix seconds, set only when that thing actually changed. Other rooms' pages read it too (CORS open). |
| `POST /api/flow/{name}` | Run a flow: `watch`, `listen` or `power_off`. Optional body `{"trigger": "http.ha"}` names the caller in Recent flows. |
| `POST /api/mode/{name}` | Set the mode without running the flow. |
| `POST /api/command` | Generic form: `{"domain": "flow", "action": "run", "args": {"name": "watch"}}`. |
| `POST /api/key/tap` | Press and release a remote key: `{"key": "rem_vol_up", "hold_ms": 60}`. |
| `POST /api/refresh/tv`, `/api/refresh/speaker` | Re-check that device now (e.g. the soundbar's Cast/AirPlay state after an outside change). |
| `GET /api/history/flows`, `/api/history/events`; `POST /api/history/clear` | Flow history and warnings/errors. |
| `GET`/`POST /api/settings` | Volumes, what Listen plays, stream slots. |
| `POST /api/restart` | Restart PiHub. |

```bash
curl -X POST http://pihub.local:9123/api/flow/watch -H 'Content-Type: application/json' -d '{"trigger": "http.ha"}'
```

### Home Assistant

The current activity comes from `/api/status` (`mode`); starting one is `POST /api/flow/{name}`. A template
select shows the activity and changes it:

```yaml
rest:
  - resource: http://192.168.90.42:9123/api/status
    scan_interval: 10
    sensor:
      - name: "Living Room Activity"
        unique_id: pihub_living_room_activity
        value_template: "{{ value_json.mode }}"

rest_command:
  pihub_flow:
    url: "http://{{ host }}:9123/api/flow/{{ flow }}"
    method: POST
    content_type: "application/json"
    payload: '{"trigger": "http.ha"}'
    timeout: 30

template:
  - select:
      - name: "Living Room Activity Select"
        unique_id: pihub_living_room_activity_select
        state: "{{ states('sensor.living_room_activity') }}"
        options: "{{ ['power_off', 'listen', 'watch'] }}"
        select_option:
          - action: rest_command.pihub_flow
            data:
              host: 192.168.90.42
              flow: "{{ option }}"
          - action: homeassistant.update_entity
            target:
              entity_id: sensor.living_room_activity
```

Repeat the `rest` sensor and the select per room with that room's PiHub address.

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
  * `speaker` (runs in the background, so a slow speaker never delays the next key)
  * `noop`

---

## 🔀 Startup and device-state behavior

### Startup

Nothing is sent to any device at start-up, and no flow runs.

* PiHub **remembers its mode** (`/data/mode.json`) and restores it immediately, so the remote works in the
  right mode from the first second. With nothing remembered (first ever start) it uses `power_off`.
* Every backend reports its **first reading** once, in the same words: `initial status received: ...`, or
  `no initial status yet: ...` if its first attempt failed (unreachable, no token, not plugged in).
* Once the TV and speaker have reported (or after 5 s), the mode is **checked against the room, mode only**:
  a listen session in progress → `listen`; otherwise TV on → `watch`; otherwise `power_off`. Anything not
  known leaves the remembered mode alone, and the check is skipped if anyone has acted since start-up.
* A first reading during start-up is where things stand, not a change, so it never sends a listen or watch
  signal. Changes after that do, exactly as described below. A device that was unreachable at start-up and
  turns up later counts as a change.

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

Current signal sources depend on install/backend:

* **Audio Pro / LinkPlay / WiiM backend**
  * Apple TV AirPlay connected-session detection may emit a `watch` device-state signal
  * TV remains available for power/control/health, but does not emit the automatic `watch` state-change signal
  * speaker entering a listen-capable state emits a `listen` device-state signal

* **Samsung soundbar backend**
  * TV logical off → on emits a `watch` device-state signal
  * AirPlay connected-session detection may emit a `listen` device-state signal

These signals are edge-triggered and intended to behave more like live state changes than periodic polling.

Routing behavior:

* explicit remote intent flows remain authoritative and may always be run again
* device-state signals are routed through runtime and may trigger dedicated device-state flows
* device-state signals are suppressed while another sequence is already running
* device-state idempotence is based on the last logical flow, to avoid flapping / “howling around”. Device-state signals compare against the last successful logical flow (last_flow), not merely the current mode.

Logical activity normalisation:

* `listen` and `listen_signal` both normalise to logical last flow `listen`
* `watch` and `watch_signal` both normalise to logical last flow `watch`

### Apple TV AirPlay watch signal

PiHub can listen for Apple TV AirPlay receiver-session activity over mDNS and emit
a debounced `watch` device-state signal. This is the only source of the watch
signal, in every room: a TV switching on or off never triggers anything.

This is intended for rooms where the Apple TV no longer wakes the display via
CEC, and PiHub should run the `watch_signal` flow when AirPlay mirroring or
video AirPlay connects.

The detector uses the Apple TV AirPlay TXT `flags` value. PiHub treats
`flags & 0x20000` as an active connected AirPlay receiver session. It does not
require a playback bit, because AirPlay mirroring/video sessions may show an
active receiver session without reporting separate playback activity.

The detector runs for either speaker backend whenever `APPLE_TV_IP` is set (and
`APPLE_TV_AIRPLAY_ENABLED` is not turned off). Without an address it is not started.

---

## Current terminology

* **mode** = current active keymap / button behavior set
* **flow** = named local sequence of ordered steps; some steps block, while dispatch steps send work at a specific point in the sequence and settle later before final flow completion
* **device-state signal** = a live edge emitted by a domain: the speaker entering a listen-capable source/playback state (`listen`), or the Apple TV's AirPlay session starting (`watch`). TV power is never one
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

Current explicit intent flows:

* `watch`
* `listen`
* `power_off`

Flow behaviour is selected automatically from `SPEAKER_BACKEND`.

### Audio Pro / LinkPlay / WiiM flow profile

When `SPEAKER_BACKEND=audiopro`, PiHub uses the full room-control flow profile.

This profile is intended for rooms where the TV needs to initiate the HDMI / CEC path for the speaker chain. The flows may control:

* Samsung TV power
* Apple TV BLE macros
* speaker volume
* speaker source selection
* listen target playback
* LinkPlay native multiroom stop / leave behaviour
* speaker power-off when the speaker started on a listen source

Speaker stop / group handling is based on the speaker state snapshot taken at the start of the flow:

* local listen source: stop playback
* multiroom host on a listen source: stop playback, then break/leave the group
* multiroom guest on a listen source: leave the group only
* HDMI/watch source: leave the speaker alone

### Samsung soundbar flow profile

When `SPEAKER_BACKEND=samsung_soundbar`, the flows mirror the Audio Pro ones: PiHub
powers the TV itself (a wake packet on, the websocket off) and drives the Apple TV
over BLE. The only difference is the speaker step, where "switch to HDMI" becomes
"hand the soundbar to the TV" (see the soundbar backend above).

* `watch`: hand the soundbar to the TV; if the TV is off, Apple TV on and TV on; set
  the watch volume.
* `listen`: if the TV is on, Apple TV home and TV off; set the listen volume; play the
  listen slot.
* `power_off`: if the TV is on, Apple TV home and TV off; hand the soundbar to the TV
  if it was on a network source.
* `listen_signal` (AirPlay or Cast started on the soundbar by other means): if the TV
  is on, TV off and Apple TV home.
* `watch_signal` (the Apple TV's AirPlay session): hand the soundbar to the TV; TV on
  if it is off; set the watch volume.

Every flow re-checks the TV's real state first, so "is the TV on?" is never stale.

A flow can return `ok: false` when important domain steps fail, for example if BLE is unavailable, speaker commands cannot be sent, the Samsung TV token is missing, or a bounded TV power command does not succeed in time.

---

## 🧪 Troubleshooting

* **No input events?** Look for `/dev/input/by-id/*event-kbd` (often `usb-Logitech_USB_Receiver-*event-kbd`). Ensure the relevant `/dev/input` paths are bind-mounted read-only into the container.
* **No device-state flow action?** Check whether the same logical flow already ran recently, or whether another sequence was already active and the signal was skipped intentionally.
* **Mode changed but `last_flow` is null?** That is expected when mode changed by startup reconcile or direct mode set rather than by a successfully completed flow.
* **TV flow steps fail immediately with `tv_token_missing`?** That is expected. Explicit TV power commands inside flows now require a saved Samsung TV token. First-time pairing/bootstrap should be done separately with the TV on and correctly configured network details.
* **Mode wrong after a restart?** The log line `startup: mode ...` shows what was restored, and a second `startup: tv ..., speaker ...: mode a -> b` line appears if the devices disagreed. `/api/status` shows `tv.on` and the speaker's `source`.
* **PiHub froze or restarted by itself?** A watchdog logs `event loop held up for over 250 ms, in: <file:line>` whenever everything was blocked, and `running again after N s` when it clears (1 s or more also appears on `/history`). If nothing runs for 15-20 s it writes where every thread was to `/data/watchdog-stall.txt` and exits so Docker restarts it; the next start logs `the previous run was restarted by the watchdog at ...`, adds it to `/history`, and keeps the report as `/data/watchdog-stall-<date>-<time>.txt` (last 5).
* **TV discovery confusion?** `presence_source` shows the most recent TV discovery source, not the current mode source of truth.
* **Samsung soundbar state looks stale or blank?** `POST /api/refresh/speaker` wakes the Cast watchdog; `/api/status` shows the speaker state.
* **Samsung soundbar AirPlay not detected?** Check the soundbar's `_airplay._tcp` mDNS announcement (for example `dns-sd -L "<name>" _airplay._tcp`) and that its `flags` change when AirPlay starts.
* **Restreamed slot won't play?** The restreamer must be running on the pihub host (port 8000); see its log for the station.

---

## 🏗️ Dev Notes

* Built with `aiohttp`
* Local-only control plane
* Runtime is the authority for:
  * current mode
  * last flow
  * sticky last trigger
* Dispatcher owns key bindings and hot-path action dispatch

* The first log line on startup is `pihub starting (built <date>, python <version>, <loop> loop)`, which says which build a house is running.

* Images are built by GitHub Actions (`.github/workflows/image.yml`) on every push to `main` that touches the code, and published to `ghcr.io/amsound/pihub-remote-nrf`:
  * `:latest` is the newest build
  * `:sha-<commit>` pins one exact build, for rolling back

Update a house:

```bash
docker compose pull && docker compose up -d
```

Roll back by setting `image:` to a `:sha-<commit>` tag and running the same command.

Build locally instead (e.g. to test before pushing):

```bash
docker build -t ghcr.io/amsound/pihub-remote-nrf:latest .
```