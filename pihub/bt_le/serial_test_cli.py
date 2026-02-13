"""Minimal serial CLI for validating PiHub <-> nRF command channel."""
from __future__ import annotations

import argparse
import time

import serial

from .ble_serial import discover_cmd_ports


def _choose_port(requested: str | None) -> str:
    if requested:
        return requested
    ports = discover_cmd_ports()
    if not ports:
        raise SystemExit("No candidate command ports found. Pass --port explicitly.")
    return ports[0]


def _write_line(ser: serial.Serial, line: str) -> None:
    framed = f"{line.strip()}\n"
    encoded = framed.encode("ascii")
    print(f"TX[{ser.port}]: {framed.rstrip()}\\n")
    wrote = ser.write(encoded)
    print(f"TX[{ser.port}]: write()={wrote}")
    ser.flush()
    print(f"TX[{ser.port}]: flush() done")


def _drain(ser: serial.Serial, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        line = ser.readline()
        if not line:
            continue
        try:
            text = line.decode("utf-8", errors="replace").strip()
        except Exception:
            text = repr(line)
        print(f"RX[{ser.port}]: {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send known-good KB/CC test commands")
    parser.add_argument("--port", help="Serial CMD port (default: auto-discover /dev/serial/by-id/*if02* then if00)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--read-seconds", type=float, default=0.75)
    args = parser.parse_args()

    port = _choose_port(args.port)
    print(f"Using port: {port}")

    with serial.Serial(port=port, baudrate=args.baud, timeout=0.2, write_timeout=0.5, exclusive=True) as ser:
        _write_line(ser, "PING")
        _drain(ser, args.read_seconds)

        # Keyboard: HID keycode 0x04 (a) down then release.
        _write_line(ser, "KB 0000040000000000")
        _drain(ser, args.read_seconds)
        _write_line(ser, "KB 0000000000000000")
        _drain(ser, args.read_seconds)

        # Consumer: play/pause 0x00CD in little-endian, then release.
        _write_line(ser, "CC CD00")
        _drain(ser, args.read_seconds)
        _write_line(ser, "CC 0000")
        _drain(ser, args.read_seconds)


if __name__ == "__main__":
    main()
