"""Handle Connection with Samsung TV."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import socket
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

TV_ON_SIGNAL_DEBOUNCE_S = 8.0

# --- Presence fallback probe ---


async def presence_probe_up(
    session: aiohttp.ClientSession,
    tv_ip: str,
    timeout_s: float = 1.5,
) -> bool:
    """
    Lightweight HTTP fallback probe for Samsung's renderer endpoint.

    This is *not* the primary source of truth for TV presence.
    SSDP alive/byebye plus startup M-SEARCH bootstrap remain the preferred signals.
    """
    url = f"http://{tv_ip}:9197/dmr"
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    for _ in range(2):
        try:
            async with session.get(url, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            await asyncio.sleep(0.05)
    return False


# --- WoL helpers ---


_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def send_wol(mac: str, *, port: int = 9, broadcast: str = "255.255.255.255") -> None:
    """Send a single Wake-on-LAN magic packet."""
    mac = mac.strip()
    if not _MAC_RE.match(mac):
        raise ValueError(f"Invalid MAC: {mac}")

    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, port))
    finally:
        s.close()

async def send_wol_burst(
    mac: str,
    *,
    count: int = 3,
    gap_s: float = 0.25,
    port: int = 9,
    broadcast: str = "255.255.255.255",
) -> None:
    """
    Send a short async burst of WoL packets.

    Keep the burst itself small and deterministic; controller logic decides
    whether to schedule another burst later.
    """
    for idx in range(count):
        send_wol(mac, port=port, broadcast=broadcast)
        if idx + 1 < count:
            await asyncio.sleep(gap_s)

def _default_wol_broadcasts(tv_ip: str) -> list[str]:
    """
    Return candidate broadcast targets for WoL.

    We try both limited broadcast and a simple /24 directed broadcast derived
    from the configured TV IP. This keeps behaviour explicit and avoids needing
    full subnet discovery logic for now.
    """
    out: list[str] = ["255.255.255.255"]

    parts = (tv_ip or "").strip().split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        directed = ".".join(parts[:3] + ["255"])
        if directed not in out:
            out.append(directed)

    return out

async def send_wol_once_multi(
    mac: str,
    *,
    broadcasts: list[str],
    ports: list[int],
    count: int = 3,
    gap_s: float = 0.08,
) -> None:
    """
    Send a short WoL spray to each candidate broadcast target on multiple ports.

    For each burst round:
    - send one magic packet to every broadcast target
    - on every requested port
    - wait a short gap before the next round
    """
    for burst_idx in range(count):
        for broadcast in broadcasts:
            for port in ports:
                try:
                    send_wol(mac, port=port, broadcast=broadcast)
                except Exception:
                    logger.debug(
                        "tv wol send failed broadcast=%s port=%s",
                        broadcast,
                        port,
                        exc_info=True,
                    )
        if burst_idx + 1 < count:
            await asyncio.sleep(gap_s)


# --- Samsung websocket control plane ---

def _b64_name(name: str) -> str:
    return base64.b64encode(name.encode("utf-8")).decode("ascii")


@dataclass
class TvWsState:
    connected: bool = False
    last_error: str = ""
    token_present: bool = False


class TvWsClient:
    """
    Maintains the Samsung websocket control channel.

    Important design note:
    This websocket is a control path, not the primary source of truth for TV
    presence. Presence truth comes from SSDP alive/byebye, startup/recovery
    M-SEARCH, and HTTP fallback probing.

    Also important:
    Do NOT automatically close the websocket merely because presence becomes
    false or unknown. In practice the TV may keep the websocket usable across
    power-state transitions, and PiHub relies on that behavior for recovery 
    and PiHub relies on that behavior for one-shot power-toggle wake handling.
    """

    def __init__(self, *, tv_ip: str, token_file: str, name: str) -> None:
        self._tv_ip = tv_ip
        self._token_file = token_file
        self._name = name
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._lock = asyncio.Lock()
        self.state = TvWsState()
        self._rx_task: Optional[asyncio.Task] = None

        self._logged_connected: Optional[bool] = None
        self._logged_token_present: Optional[bool] = None

        self._refresh_token_present()

    def _read_token(self) -> str:
        try:
            tok = open(self._token_file, "r", encoding="utf-8").read().strip()
            self.state.token_present = bool(tok)
            return tok
        except FileNotFoundError:
            self.state.token_present = False
            return ""
        except Exception:
            self.state.token_present = False
            return ""

    def _refresh_token_present(self) -> None:
        tok = self._read_token()
        token_present = bool(tok)
        if self._logged_token_present is None or token_present != self._logged_token_present:
            self._logged_token_present = token_present
            logger.debug(
                "initialised tv websocket tv_ip=%s token_present=%s",
                self._tv_ip,
                "true" if token_present else "false",
            )

    def _write_token(self, token: str) -> None:
        token = token.strip()
        if not token:
            return
        try:
            os.makedirs(os.path.dirname(self._token_file) or ".", exist_ok=True)
            with open(self._token_file, "w", encoding="utf-8") as f:
                f.write(token + "\n")
            self.state.token_present = True
            self._logged_token_present = True
            logger.info("token saved to %s", self._token_file)
        except Exception as exc:
            self.state.last_error = repr(exc)
            logger.exception("failed to save token to %s: %r", self._token_file, exc)

    def _ws_url(self) -> str:
        name_b64 = _b64_name(self._name)
        token = self._read_token()
        base = f"wss://{self._tv_ip}:8002/api/v2/channels/samsung.remote.control?name={name_b64}"
        if token:
            base += f"&token={token}"
        return base

    async def _rx_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    continue

                data = (payload or {}).get("data")
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:
                        data = None

                if isinstance(data, dict):
                    tok = data.get("token")
                    if isinstance(tok, str) and tok.strip():
                        self._write_token(tok)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("rx loop ended: %r", exc)
        finally:
            async with self._lock:
                if self._ws is ws:
                    self._ws = None
                    self.state.connected = False
                    if self._logged_connected is True:
                        self._logged_connected = False
                        logger.debug("tv websocket disconnected")

    async def connect(self, session: aiohttp.ClientSession, *, timeout_s: float = 2.0) -> bool:
        async with self._lock:
            if self._ws and not self._ws.closed and self.state.connected:
                return True

            url = self._ws_url()
            try:
                self._ws = await session.ws_connect(
                    url,
                    heartbeat=30,
                    autoping=True,
                    ssl=False,
                    timeout=timeout_s,
                )
                self.state.connected = True
                self.state.last_error = ""

                if self._logged_connected is None or self._logged_connected is False:
                    self._logged_connected = True
                    logger.debug("tv websocket connected")

                if self._rx_task and not self._rx_task.done():
                    self._rx_task.cancel()

                ws = self._ws
                self._rx_task = asyncio.create_task(self._rx_loop(ws), name="tvws_rx")
                return True
            except Exception as exc:
                self.state.connected = False
                self.state.last_error = repr(exc)
                if self._logged_connected is True:
                    self._logged_connected = False
                    logger.debug("tv websocket disconnected")
                logger.debug("connect failed: %r", exc)
                self._ws = None
                return False

    async def close(self) -> None:
        async with self._lock:
            task, self._rx_task = self._rx_task, None
            if task and not task.done():
                task.cancel()

            ws, self._ws = self._ws, None
            self.state.connected = False

            if self._logged_connected is True:
                self._logged_connected = False
                logger.debug("tv websocket disconnected")

        if ws and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass

    async def send_key(self, key: str) -> bool:
        payload = {
            "method": "ms.remote.control",
            "params": {
                "Cmd": "Click",
                "DataOfCmd": key,
                "Option": "false",
                "TypeOfRemote": "SendRemoteKey",
            },
        }

        ws_to_close = None
        async with self._lock:
            ws = self._ws
            if ws is None or ws.closed:
                self.state.connected = False
                self._ws = None
                return False
            try:
                await ws.send_str(json.dumps(payload))
                return True
            except Exception as exc:
                self.state.connected = False
                self.state.last_error = repr(exc)
                if self._ws is ws:
                    self._ws = None
                    ws_to_close = ws

        if ws_to_close and not ws_to_close.closed:
            try:
                await ws_to_close.close()
            except Exception:
                pass
        return False


# --- Controller ---


@dataclass
class TvSnapshot:
    initialised: bool
    presence_on: bool | None
    presence_source: str
    last_change_age_s: int | None
    ws_connected: bool
    token_present: bool
    last_error: str


class TvController:
    def __init__(
        self,
        *,
        tv_ip: str,
        tv_mac: str,
        token_file: str,
        name: str,
        state_change_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.tv_ip = tv_ip
        self.tv_mac = tv_mac
        self.token_file = token_file
        self.name = name

        # The websocket is intentionally managed as a reusable control channel.
        # It is NOT tightly coupled to cached presence state, and must not be
        # auto-closed just because presence becomes false/unknown. Real devices can
        # keep the socket usable across transitions, which is important for recovery
        # and post-power-off power-toggle behavior.

        self.ws = TvWsClient(tv_ip=tv_ip, token_file=token_file, name=name)

        logger.info(
            "initialised tv tv_ip=%s tv_mac=%s token_present=%s",
            tv_ip,
            tv_mac,
            "true" if self.ws.state.token_present else "false",
        )

        self._session: Optional[aiohttp.ClientSession] = None
        self._presence_cached: bool | None = None
        self._presence_source: str = "unknown"
        self._presence_last_change_ts: float | None = None

        # Fires immediately when any trusted presence path marks the TV on.
        # This lets power_on() stop WoL / KEY_POWER behaviour without polling.
        self._presence_on_event = asyncio.Event()

        self._power_on_active: bool = False

        # Protects the dangerous part of power_on(): KEY_POWER is a toggle and must
        # never be queued/retried within a single power-on attempt.
        self._power_key_lock = asyncio.Lock()
        self._power_key_attempt_id = 0

        self._last_power_off_request_ts: float | None = None
        self._state_change_callback = state_change_callback

        self._pending_watch_signal_task: Optional[asyncio.Task] = None
        self._ws_warm_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        # Best-effort warm-up. This must not block startup and must not be treated
        # as power truth. It only prepares the control path for TVs whose network
        # stack stays up while the panel is off.
        if self._ws_warm_task is None or self._ws_warm_task.done():
            self._ws_warm_task = asyncio.create_task(
                self._warm_ws_control_path(),
                name="tv:ws_warmup",
            )

    async def _warm_ws_control_path(self) -> None:
        if not self._session:
            return

        try:
            await self.ws.connect(self._session, timeout_s=2.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("tv websocket warm-up failed", exc_info=True)

    async def stop(self) -> None:
        self._cancel_pending_watch_signal()

        task, self._ws_warm_task = self._ws_warm_task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        await self.ws.close()

    # Presence cache is the local truth used for watch/listen logic and health.
    # Updating presence here must not implicitly tear down the websocket control
    # channel. Presence and websocket usability are related but not identical.

    def _commit_presence(self, on: bool, *, source: str) -> bool:
        prev_on = self._presence_cached is True

        # Keep the event aligned even if the cached value did not change.
        # This matters when another task starts waiting after presence is already true.
        if self._presence_cached is on:
            if on:
                self._presence_on_event.set()
            else:
                self._presence_on_event.clear()
            return False

        now = asyncio.get_running_loop().time()
        self._presence_cached = on
        self._presence_source = source
        self._presence_last_change_ts = now

        # Raw presence truth is immediate. The watch/listen mode promotion can still
        # be debounced below, but power_on() should stop instantly on true presence.
        if on:
            self._presence_on_event.set()
        else:
            self._presence_on_event.clear()

        curr_on = self._presence_cached is True

        # Passive TV-on promotions are debounced before emitting a watch signal.
        # Raw presence remains immediate for health/control truth; only the
        # device-state signal is delayed/cancelled.
        if not prev_on and curr_on:
            self._schedule_watch_signal(source=source)

        # If TV falls back off before the debounce completes, cancel the pending
        # watch signal so spurious SSDP "on" splats do not promote runtime mode.
        if prev_on and not curr_on:
            self._cancel_pending_watch_signal()

        return True

    def notify_msearch(self, *, location: str | None) -> bool:
        if location and "/dmr" not in location:
            logger.warning("tv msearch rejected location=%s", location)
            return False

        changed = self._commit_presence(True, source="msearch")
        logger.debug(
            "tv msearch accepted changed=%s location=%s presence_on=%s presence_source=%s",
            "true" if changed else "false",
            location,
            "true" if self._presence_cached is True else "false",
            self._presence_source,
        )
        return changed

    def notify_ssdp(
        self,
        *,
        nts: str,
        nt: str,
        usn: str,
        location: str | None,
        source: str = "ssdp",
    ) -> bool:
        """Return True only when discovery changed TV presence."""
        is_renderer_presence = False
        if location and "/dmr" in location:
            is_renderer_presence = True
        if "MediaRenderer" in (nt or ""):
            is_renderer_presence = True
        if not is_renderer_presence:
            return False

        if nts == "ssdp:alive":
            return self._commit_presence(True, source="ssdp_alive")
        if nts == "ssdp:byebye":
            return self._commit_presence(False, source="ssdp_byebye")
        return False

    async def ensure_ws_connected(self) -> None:
        if not self._session:
            return
        await self.ws.connect(self._session)

    async def reconcile_presence(self, *, bootstrap_timeout_s: float = 3.0) -> dict[str, Any]:
        """
        One-shot active presence reconcile.

        Returns a structured outcome:
        - present_true
        - present_false
        - unknown
        - error
        """
        if not self._session:
            await self.start()

        if not self._session:
            return {
                "outcome": "error",
                "presence_on": self._presence_cached,
                "presence_source": self._presence_source,
                "changed": False,
                "errors": ["session_unavailable"],
            }

        before_presence = self._presence_cached
        before_source = self._presence_source
        errors: list[str] = []

        logger.debug(
            "tv reconcile start tv_ip=%s presence_on=%s presence_source=%s",
            self.tv_ip,
            self._presence_cached,
            self._presence_source,
        )

        # Primary active survey path: ask the network directly.
        try:
            await msearch_bootstrap(self, timeout_s=bootstrap_timeout_s)
        except Exception as exc:
            logger.debug("tv reconcile msearch bootstrap failed", exc_info=True)
            errors.append(f"msearch_bootstrap_failed:{exc!r}")

        # If bootstrap established presence, optionally nudge command-path readiness.
        if self._presence_cached is True:
            try:
                await self.ensure_ws_connected()
            except Exception as exc:
                logger.debug("tv reconcile ws connect failed after positive presence", exc_info=True)
                errors.append(f"ws_connect_failed:{exc!r}")

            return {
                "outcome": "present_true",
                "presence_on": self._presence_cached,
                "presence_source": self._presence_source,
                "changed": (
                    self._presence_cached != before_presence
                    or self._presence_source != before_source
                ),
                "errors": errors,
            }

        # Fallback only: use HTTP renderer probe if M-SEARCH did not establish truth.
        try:
            if await presence_probe_up(self._session, self.tv_ip):
                self._commit_presence(True, source="probe_http_up")
                logger.debug(
                    "tv reconcile probe_up presence_on=%s source=%s",
                    "true" if self._presence_cached is True else "false",
                    self._presence_source,
                )

                try:
                    await self.ensure_ws_connected()
                except Exception as exc:
                    logger.debug("tv reconcile ws connect failed after http fallback", exc_info=True)
                    errors.append(f"ws_connect_failed:{exc!r}")

                return {
                    "outcome": "present_true",
                    "presence_on": self._presence_cached,
                    "presence_source": self._presence_source,
                    "changed": (
                        self._presence_cached != before_presence
                        or self._presence_source != before_source
                    ),
                    "errors": errors,
                }
        except Exception as exc:
            logger.debug("tv reconcile http probe failed", exc_info=True)
            errors.append(f"http_probe_failed:{exc!r}")

        # No positive signal established. Leave cached presence as-is.
        logger.debug(
            "tv reconcile complete tv_ip=%s presence_on=%s presence_source=%s",
            self.tv_ip,
            self._presence_cached,
            self._presence_source,
        )

        if self._presence_cached is False:
            outcome = "present_false"
        elif self._presence_cached is True:
            # Conservative fallback: truth is currently "on" from cache, but this refresh
            # did not establish a fresh positive signal. Keep the cache truthful.
            outcome = "present_true"
        else:
            outcome = "unknown"

        return {
            "outcome": outcome,
            "presence_on": self._presence_cached,
            "presence_source": self._presence_source,
            "changed": (
                self._presence_cached != before_presence
                or self._presence_source != before_source
            ),
            "errors": errors,
        }

    def snapshot(self) -> TvSnapshot:
        st = self.ws.state
        last_change_age_s: int | None = None
        if self._presence_last_change_ts is not None:
            last_change_age_s = int(asyncio.get_running_loop().time() - self._presence_last_change_ts)
        return TvSnapshot(
            initialised=self._presence_cached is not None,
            presence_on=self._presence_cached,
            presence_source=self._presence_source,
            last_change_age_s=last_change_age_s,
            ws_connected=st.connected,
            token_present=st.token_present,
            last_error=st.last_error,
        )

    def _cancel_pending_watch_signal(self) -> None:
        task, self._pending_watch_signal_task = self._pending_watch_signal_task, None
        if task and not task.done():
            task.cancel()

    async def _emit_watch_signal_after_delay(self, *, source: str, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)

            # Only emit if TV is still logically on after the debounce window.
            if self._presence_cached is not True:
                logger.debug(
                    "tv watch signal cancelled after debounce source=%s reason=presence_not_on",
                    source,
                )
                return

            self._emit_state_change(
                "watch",
                {
                    "domain": "tv",
                    "presence_source": source,
                },
            )
            logger.debug(
                "tv watch signal emitted after debounce source=%s delay_s=%.1f",
                source,
                delay_s,
            )
        except asyncio.CancelledError:
            logger.debug("tv watch signal debounce cancelled source=%s", source)
            raise
        finally:
            if self._pending_watch_signal_task is asyncio.current_task():
                self._pending_watch_signal_task = None

    def _schedule_watch_signal(self, *, source: str) -> None:
        self._cancel_pending_watch_signal()
        self._pending_watch_signal_task = asyncio.create_task(
            self._emit_watch_signal_after_delay(
                source=source,
                delay_s=TV_ON_SIGNAL_DEBOUNCE_S,
            ),
            name="tv:watch_signal_debounce",
        )

    def _spawn_state_change_callback(self, name: str, payload: dict[str, Any]) -> None:
        cb = self._state_change_callback
        if cb is None:
            return

        try:
            task = asyncio.create_task(
                cb(name, payload),
                name=f"tv_state_change:{name}",
            )
        except Exception:
            logger.exception("tv state change callback spawn failed name=%s", name)
            return

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                logger.debug("tv state change callback cancelled name=%s", name)
            except Exception:
                logger.exception("tv state change callback failed name=%s", name)

        task.add_done_callback(_done)

    def _emit_state_change(self, name: str, payload: dict[str, Any]) -> None:
        self._spawn_state_change_callback(name, payload)

    async def _wait_for_presence_true(self, *, timeout_s: float) -> bool:
        if self._presence_cached is True:
            self._presence_on_event.set()
            return True

        try:
            await asyncio.wait_for(self._presence_on_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return False

        return self._presence_cached is True

    async def power_off(self, *, wait: bool = True, timeout_s: float = 25.0) -> bool:
        if not self._session:
            return False
        if self._presence_cached is False:
            return True

        self._last_power_off_request_ts = asyncio.get_running_loop().time()

        # KEY_POWER is a toggle. Do not retry after a failed send_key(), because
        # failure can be ambiguous: the TV may have received the frame before the
        # local websocket noticed an error.
        ok = False

        try:
            if not self.ws.state.connected:
                await self.ws.connect(self._session, timeout_s=1.0)

            if self.ws.state.connected:
                ok = await self.ws.send_key("KEY_POWER")
        except Exception:
            logger.debug("tv power_off KEY_POWER failed", exc_info=True)
            ok = False

        if not wait:
            return ok

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            if self._presence_cached is False:
                return True

            try:
                if not await presence_probe_up(self._session, self.tv_ip):
                    self._commit_presence(False, source="probe_http_down")
                    return True
            except Exception:
                logger.debug("tv power_off http presence probe failed", exc_info=True)

            await asyncio.sleep(0.2)

        return False

    async def power_on(self, *, timeout_s: float = 60.0) -> bool:
        if not self._session:
            return False

        if self._presence_cached is True:
            self._presence_on_event.set()
            return True

        # Single-flight behaviour: only the first caller runs WoL / KEY_POWER.
        # Any overlapping caller just waits for presence to become true.
        if self._power_on_active:
            return await self._wait_for_presence_true(timeout_s=timeout_s)

        self._power_on_active = True
        stop_event = asyncio.Event()

        # Give this power-on attempt a unique id. The KEY_POWER guard checks this
        # immediately before sending, so a stale worker cannot send later.
        self._power_key_attempt_id += 1
        attempt_id = self._power_key_attempt_id
        key_power_sent = False

        async def wol_worker() -> None:
            WOL_LOOP_INTERVAL_S = 0.40
            WOL_PORTS = [9, 7]
            WOL_BROADCASTS = _default_wol_broadcasts(self.tv_ip)
            WOL_BURST_COUNT = 5
            WOL_BURST_GAP_S = 0.06

            while not stop_event.is_set() and self._presence_cached is not True:
                try:
                    await send_wol_once_multi(
                        self.tv_mac,
                        broadcasts=WOL_BROADCASTS,
                        ports=WOL_PORTS,
                        count=WOL_BURST_COUNT,
                        gap_s=WOL_BURST_GAP_S,
                    )
                except Exception:
                    logger.debug("tv wol send failed during power_on", exc_info=True)

                # Do not sleep blindly. Wake immediately if presence arrives.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=WOL_LOOP_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass

        async def key_power_worker() -> None:
            nonlocal key_power_sent

            # Immediate path: no artificial delay. WoL and KEY_POWER connection attempts
            # both start straight away. We may retry websocket connection, but KEY_POWER
            # itself is strictly one-shot.
            WS_CONNECT_TIMEOUT_S = 2.0
            WS_CONNECT_RETRY_INTERVAL_S = 0.35

            while not stop_event.is_set() and self._presence_cached is not True:
                try:
                    ws_connected = self.ws.state.connected

                    if not ws_connected:
                        ws_connected = await self.ws.connect(
                            self._session,
                            timeout_s=WS_CONNECT_TIMEOUT_S,
                        )

                    # SSDP alive / M-SEARCH may have landed while websocket connect was
                    # in flight. Re-check before attempting the dangerous toggle.
                    if stop_event.is_set() or self._presence_cached is True:
                        return

                    if ws_connected:
                        async with self._power_key_lock:
                            # Final guard immediately before sending the toggle.
                            if attempt_id != self._power_key_attempt_id:
                                return
                            if key_power_sent:
                                return
                            if stop_event.is_set() or self._presence_cached is True:
                                return

                            # From this point on, treat the toggle as attempted even if
                            # send_key() returns False. A local websocket send failure can
                            # be ambiguous: the TV may still have received the frame.
                            key_power_sent = True
                            sent = await self.ws.send_key("KEY_POWER")
                            logger.debug(
                                "tv power_on one-shot KEY_POWER attempted tv_ip=%s sent=%s",
                                self.tv_ip,
                                "true" if sent else "false",
                            )
                            return

                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("tv power_on websocket connect/send path failed", exc_info=True)

                # Connection failure is not a KEY_POWER attempt. Retry connection quickly
                # until presence arrives or power_on() times out/cancels this worker.
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=WS_CONNECT_RETRY_INTERVAL_S,
                    )
                except asyncio.TimeoutError:
                    pass

        async def msearch_worker() -> None:
            # Optional detection accelerator. This does not wake the TV, so it is safe.
            # It helps if SSDP alive is missed but the TV is already answering M-SEARCH.
            MSEARCH_INTERVAL_S = 2.0
            MSEARCH_TIMEOUT_S = 1.0

            while not stop_event.is_set() and self._presence_cached is not True:
                try:
                    await msearch_bootstrap(self, timeout_s=MSEARCH_TIMEOUT_S)
                except Exception:
                    logger.debug("tv power_on msearch bootstrap failed", exc_info=True)

                if self._presence_cached is True:
                    return

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=MSEARCH_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass

        try:
            wol_task = asyncio.create_task(wol_worker(), name="tv:power_on_wol")
            key_task = asyncio.create_task(key_power_worker(), name="tv:power_on_key_power")
            msearch_task = asyncio.create_task(msearch_worker(), name="tv:power_on_msearch")

            try:
                powered_on = await self._wait_for_presence_true(timeout_s=timeout_s)
                return powered_on
            finally:
                # The moment presence is true, or the attempt times out, stop all
                # power-on behaviour. This cancels WoL spray, M-SEARCH, and any
                # in-flight websocket KEY_POWER path that has not sent yet.
                stop_event.set()

                for task in (wol_task, key_task, msearch_task):
                    if not task.done():
                        task.cancel()

                await asyncio.gather(
                    wol_task,
                    key_task,
                    msearch_task,
                    return_exceptions=True,
                )

                if self._presence_cached is True:
                    try:
                        await self.ws.connect(self._session)
                    except Exception:
                        logger.debug(
                            "tv ws connect after power_on presence failed",
                            exc_info=True,
                        )

        finally:
            self._power_on_active = False

    async def volume_up(self) -> bool:
        if not self._session or self._presence_cached is not True:
            return False
        if not self.ws.state.connected:
            await self.ws.connect(self._session)
        return await self.ws.send_key("KEY_VOLUP")

    async def volume_down(self) -> bool:
        if not self._session or self._presence_cached is not True:
            return False
        if not self.ws.state.connected:
            await self.ws.connect(self._session)
        return await self.ws.send_key("KEY_VOLDOWN")

    async def mute_toggle(self) -> bool:
        if not self._session or self._presence_cached is not True:
            return False
        if not self.ws.state.connected:
            await self.ws.connect(self._session)
        return await self.ws.send_key("KEY_MUTE")

    async def send_key(self, *, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        await self.ws.send_key(key)

    async def press(self, *, key: str) -> None:
        await self.send_key(key=key)


# --- SSDP discovery and bootstrap ---


_MCAST_GRP = "239.255.255.250"
_MCAST_PORT = 1900
_MSEARCH_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
_MSEARCH_BURST_COUNT = 3
_MSEARCH_BURST_GAP_S = 0.4


async def ssdp_listener(tv: TvController) -> None:
    """Listen for SSDP NOTIFY from the configured TV IP and forward to controller."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", _MCAST_PORT))
        mreq = socket.inet_aton(_MCAST_GRP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(True)

        while True:
            try:
                data, addr = await asyncio.to_thread(sock.recvfrom, 65535)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tv:ssdp listener error")
                await asyncio.sleep(1)
                continue

            src_ip = addr[0]
            if src_ip != tv.tv_ip:
                continue

            txt = data.decode("utf-8", errors="ignore")
            if "NOTIFY * HTTP/1.1" not in txt:
                continue

            hdr: dict[str, str] = {}
            for line in txt.split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    hdr[k.strip().upper()] = v.strip()

            acted = tv.notify_ssdp(
                nts=hdr.get("NTS", ""),
                nt=hdr.get("NT", ""),
                usn=hdr.get("USN", ""),
                location=hdr.get("LOCATION"),
                source="ssdp",
            )
            if acted and hdr.get("NTS", "") == "ssdp:alive":
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    logger.debug("tv:ssdp ws connect failed after alive", exc_info=True)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _parse_headers(packet: str) -> dict[str, str]:
    hdr: dict[str, str] = {}
    for line in packet.split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            hdr[k.strip().upper()] = v.strip()
    return hdr


async def msearch_bootstrap(tv: TvController, *, timeout_s: float = 3.0) -> None:
    """Send a short targeted M-SEARCH burst and accept replies for the configured TV IP."""
    msg = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            f"HOST: {_MCAST_GRP}:{_MCAST_PORT}",
            'MAN: "ssdp:discover"',
            "MX: 2",
            f"ST: {_MSEARCH_ST}",
            "",
            "",
        ]
    ).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)

        logger.debug(
            "tv:msearch bootstrap start tv_ip=%s burst_count=%d timeout_s=%.1f",
            tv.tv_ip,
            _MSEARCH_BURST_COUNT,
            timeout_s,
        )

        for i in range(_MSEARCH_BURST_COUNT):
            try:
                await asyncio.to_thread(sock.sendto, msg, (_MCAST_GRP, _MCAST_PORT))
                logger.debug(
                    "tv:msearch probe sent tv_ip=%s seq=%d/%d",
                    tv.tv_ip,
                    i + 1,
                    _MSEARCH_BURST_COUNT,
                )
            except Exception:
                logger.exception("tv:msearch send failed seq=%d", i + 1)
                break
            if i + 1 < _MSEARCH_BURST_COUNT:
                await asyncio.sleep(_MSEARCH_BURST_GAP_S)

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                data, addr = await asyncio.to_thread(sock.recvfrom, 65535)
            except socket.timeout:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tv:msearch bootstrap error")
                return

            reply_ip = addr[0]
            txt = data.decode("utf-8", errors="ignore")
            if "HTTP/1.1 200 OK" not in txt:
                logger.debug("tv:msearch ignored non-200 reply from=%s", reply_ip)
                continue

            hdr = _parse_headers(txt)
            st = hdr.get("ST")
            location = hdr.get("LOCATION")
            location_ip = urlparse(location).hostname if location else None

            logger.debug("tv:msearch reply from=%s st=%s location=%s", reply_ip, st, location)

            if st != _MSEARCH_ST:
                logger.debug(
                    "tv:msearch ignored reply from=%s reason=st_mismatch st=%s expected=%s",
                    reply_ip,
                    st,
                    _MSEARCH_ST,
                )
                continue

            if reply_ip != tv.tv_ip and location_ip != tv.tv_ip:
                logger.debug(
                    "tv:msearch ignored reply from=%s reason=ip_mismatch location_ip=%s expected=%s",
                    reply_ip,
                    location_ip,
                    tv.tv_ip,
                )
                continue

            acted = tv.notify_msearch(location=location)
            logger.debug(
                "tv:msearch matched reply from=%s location=%s acted=%s",
                reply_ip,
                location,
                "true" if acted else "false",
            )
            if acted:
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    logger.debug("tv:msearch ws connect failed after bootstrap", exc_info=True)
            return

        logger.debug("tv:msearch bootstrap complete tv_ip=%s acted=false reason=timeout", tv.tv_ip)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def start_discovery_tasks(tv: TvController) -> list[asyncio.Task]:
    """Start long-lived passive discovery tasks for the TV domain."""
    return [
        asyncio.create_task(ssdp_listener(tv), name="tv:ssdp"),
    ]


async def stop_discovery_tasks(tasks: Iterable[asyncio.Task]) -> None:
    """Cancel and await discovery tasks."""
    tasks = list(tasks)
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("tv discovery task crashed during stop")
