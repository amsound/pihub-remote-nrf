"""The TV interface flows, the runtime and status rely on.

Both backends implement it: TvController (older Samsung: websocket keys, WoL,
SSDP presence) and SamsungFrameTv (Samsung IP control). Both return the same
TvSnapshot, so nothing outside a backend needs to know which one it has.
Backend-specific extras (the Frame's set_input_hdmi1) are only called from a
flow step guarded by a condition on `backend`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

BACKEND_WS = "samsung_ws"
BACKEND_FRAME = "samsung_frame_ip"


@dataclass(frozen=True)
class TvSnapshot:
    backend: str                  # BACKEND_WS or BACKEND_FRAME
    presence_on: bool | None      # on / off / not known yet
    presence_source: str          # how that was learned (ssdp_alive, ip_control_power, ...)
    changed_at: float | None      # wall clock of the last on/off change
    ws_connected: bool            # older TV: the control websocket is up (always False on a Frame)
    token_present: bool
    last_error: str
    input_source: str | None = None   # Frame only


@runtime_checkable
class TvLike(Protocol):
    tv_ip: str
    initial: Any   # InitialStatus: the first reading after start-up

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def snapshot(self) -> TvSnapshot: ...
    def set_state_change_callback(
        self, callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None
    ) -> None: ...

    async def refresh_presence(self) -> None: ...
    async def reconcile_presence(self, *, bootstrap_timeout_s: float = 3.0) -> dict[str, Any]: ...
    async def power_on(self, *, timeout_s: float = ...) -> bool: ...
    async def power_off(self, *, wait: bool = True, timeout_s: float = ...) -> bool: ...

    # SSDP announcements from the TV's address arrive here (see samsung_tv.ssdp_listener).
    def notify_ssdp(self, *, nts: str, nt: str, usn: str, location: str | None, source: str = "ssdp") -> bool: ...
