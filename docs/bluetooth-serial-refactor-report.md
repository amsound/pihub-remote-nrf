# Bluetooth transport refactor report (BlueZ -> nRF USB serial)

## Goal
Replace host-side BlueZ/DBus BLE integration with the nRF USB dongle serial protocol, using `EVT` status lines:

- `EVT READY`
- `EVT ADV`
- `EVT CONN`
- `EVT PROTO`
- `EVT CONN_PARAMS`
- `EVT PHY`
- `EVT DISC`
- `EVT ERR`

## What already exists
The current runtime path already uses a serial transport:

- `pihub/bt_le/ble_serial.py` provides serial connect/reconnect, command send (`KB`, `CC`, `PING`, `STATUS`, `UNPAIR`) and `EVT` parsing into structured state.
- `pihub/bt_le/controller.py` wraps this state and exposes BT status to health/reporting.

## Files that touch BLE/Bluetooth behavior and how to refactor

### 1) `pihub/bt_le/ble_serial.py` (core serial transport)
**Status:** already aligned with the nRF serial protocol.

**Keep / extend:**
- Keep `EVT` parser as system of record for BLE link state.
- Keep reconnection loop and handshake (`PING` + accept `PONG` or `EVT READY 1`).

**Suggested enhancements (optional):**
- Add strict line grammar checks for malformed `EVT` payloads.
- Add metrics counters for parse failures and reconnect count.
- Add configurable serial write/read timeouts via env.

### 2) `pihub/bt_le/controller.py` (transport-facing controller API)
**Status:** serial-backed, but had API drift from dispatcher/app call sites.

**Refactor done in this change:**
- Normalized controller methods to the actual call shape used by dispatcher and HA command handling:
  - `key_down(*, usage, code)`
  - `key_up(*, usage, code)`
  - `send_key(*, usage, code, hold_ms)` async
  - `run_macro(...)` async

This keeps BLE action dispatching independent from transport details (BlueZ vs serial).

### 3) `pihub/app.py` (wiring config -> controller)
**Status:** instantiation used stale keyword names.

**Refactor done in this change:**
- Updated controller construction to pass the active serial keywords:
  - `name`
  - `serial_port`
  - `baud`

This ensures the configured serial device/baud are actually used.

### 4) `pihub/dispatcher.py` (edge routing)
**Status:** already transport-agnostic at logic level, but depends on controller API shape.

**Action:** no code changes needed after controller API fix.

### 5) `pihub/config.py` (runtime settings)
**Status:** has both legacy and serial fields.

**Recommended follow-up:**
- Deprecate/remove `BLE_ADAPTER` from runtime paths if BlueZ is no longer supported.
- Keep:
  - `BLE_SERIAL_DEVICE`
  - `BLE_SERIAL_BAUD`
- Optionally add:
  - `BLE_SERIAL_FALLBACK_PORTS`
  - `BLE_SERIAL_PING_TIMEOUT_S`
  - `BLE_SERIAL_RECONNECT_DELAY_S`

### 6) `pihub/health.py` (observability)
**Status:** consumes controller `status`; still uses key `adapter_present`.

**Recommended follow-up:**
- Rename externally visible field for clarity, e.g. `transport_present` or `serial_present`.
- Preserve backward compatibility by returning both names for one release.

### 7) `README.md` / ops docs
**Status:** still contains BlueZ/DBus troubleshooting and compose mount hints.

**Recommended follow-up:**
- Remove DBus/BlueZ requirements from runtime docs for serial mode.
- Add serial troubleshooting:
  - check `/dev/ttyACM*` visibility
  - permissions (`dialout` / container device mapping)
  - firmware emits `EVT READY 1`

### 8) `pihub/bt_le/hid_device.py` (legacy BlueZ implementation)
**Status:** BlueZ/DBus implementation appears unused by current runtime path.

**Recommended follow-up options:**
- **Option A (preferred):** remove this file and BlueZ-only dependencies after confirming no external imports.
- **Option B:** keep but mark as `legacy_bluez` and exclude from normal runtime/docs.

### 9) dependencies/container runtime
**Files:** `requirements.txt`, `Dockerfile`

**Recommended follow-up:**
- Remove BlueZ/DBus packages if legacy module is removed.
- Keep `pyserial` and any minimal HID/input deps.

## Event mapping contract (serial dongle -> app state)
Use this as the canonical mapping:

- `EVT READY 1|0` -> transport ready state
- `EVT ADV 1|0` -> BLE advertising state
- `EVT CONN 1|0` -> BLE link connected state
- `EVT PROTO 1|0` -> protocol mode (`1=boot`, `0=report`)
- `EVT CONN_PARAMS ...` -> diagnostics dictionary
- `EVT PHY ...` -> diagnostics dictionary
- `EVT DISC reason=<n>` -> last disconnect reason
- `EVT ERR 1|0` -> dongle internal error flag

## Rollout plan
1. Merge API consistency fixes (controller/app) - **done here**.
2. Validate serial hotplug/reconnect with real dongle and HA command path.
3. Update docs and compose examples to serial-only guidance.
4. Remove legacy BlueZ module and packages in a cleanup PR.
5. Add small test coverage around controller API + `EVT` parsing.
