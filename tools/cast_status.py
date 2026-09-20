#!/usr/bin/env python3
"""Watch a Cast device's media status. Diagnostic helper, not part of the app.

Prints every media-status update the receiver sends, including idle_reason,
which is what says *why* playback stopped (ERROR / FINISHED / INTERRUPTED /
CANCELLED). Run it on the Pi alongside a listen flow:

    python3 tools/cast_status.py 192.168.70.xxx

Ctrl-C to stop.
"""

from __future__ import annotations

import sys
import time

import pychromecast


class _Listener:
    def new_media_status(self, status) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(
            f"{stamp} state={status.player_state:<10} "
            f"idle_reason={status.idle_reason!s:<12} "
            f"pos={status.current_time} dur={status.duration} "
            f"ct={status.content_type} pausable={status.supports_pause}",
            flush=True,
        )
        if status.content_id:
            print(f"          url={status.content_id}", flush=True)

    def load_media_failed(self, item, error_code) -> None:
        print(f"LOAD FAILED item={item} error_code={error_code}", flush=True)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    host = sys.argv[1]
    casts, browser = pychromecast.get_listed_chromecasts(known_hosts=[host])
    if not casts:
        print(f"no Cast device found at {host}")
        return 1

    cast = casts[0]
    cast.wait(timeout=10)
    print(f"connected: {cast.name} ({host})", flush=True)

    cast.media_controller.register_status_listener(_Listener())
    cast.media_controller.update_status()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        pychromecast.discovery.stop_discovery(browser)
        cast.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
