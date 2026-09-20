#!/usr/bin/env python3
"""Watch a Cast device's media status. Diagnostic helper, not part of the app.

Prints every media-status update the receiver sends, including idle_reason,
which is what says *why* playback stopped (ERROR / FINISHED / INTERRUPTED /
CANCELLED).

Run it inside the pihub container, which is where pychromecast lives:

    docker cp tools/cast_status.py <container>:/tmp/cast_status.py
    docker exec -it <container> /opt/venv/bin/python /tmp/cast_status.py <speaker-ip>

Ctrl-C to stop.
"""

from __future__ import annotations

import contextlib
import sys
import time

import pychromecast


def _cast_host(cast) -> str | None:
    """IP of a discovered cast device, across pychromecast layouts."""
    cast_info = getattr(cast, "cast_info", None)
    for obj in (cast_info, cast):
        for attr in ("host", "uri"):
            value = getattr(obj, attr, None)
            if isinstance(value, str) and value:
                return value.split(":")[0]
    return None


class _Listener:
    def new_media_status(self, status) -> None:
        print(
            f"{time.strftime('%H:%M:%S')} "
            f"state={status.player_state:<10} "
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

    # Same approach the app uses: discover with known_hosts, then filter by IP.
    # get_listed_chromecasts() needs a uuid/friendly_name to match on, so it is
    # not usable when all we have is an address.
    casts, browser = pychromecast.get_chromecasts(known_hosts=[host])

    matching = [c for c in casts if _cast_host(c) == host]
    if not matching:
        seen = [f"{_cast_host(c)} ({getattr(c, 'name', '?')})" for c in casts]
        print(f"no Cast device at {host}")
        print(f"discovered instead: {seen or 'nothing'}")
        with contextlib.suppress(Exception):
            pychromecast.discovery.stop_discovery(browser)
        return 1

    cast = matching[0]
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
        with contextlib.suppress(Exception):
            pychromecast.discovery.stop_discovery(browser)
        with contextlib.suppress(Exception):
            cast.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
