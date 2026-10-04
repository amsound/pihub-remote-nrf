"""Handle Connection with Samsung TV."""

from __future__ import annotations

import asyncio
import contextlib
import base64
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import aiohttp

from .initial_status import InitialStatus
from .tv import BACKEND_WS, TvSnapshot

logger = logging.getLogger(__name__)

# During a power-on the TV's own "alive" announcement is the preferred sign that it
# is up (about a second after it wakes). Only if none has arrived is the TV asked
# directly, and only at these moments after the attempt began: once in case the
# announcement was lost, and once more just before a flow's power-on step gives up.
POWER_ON_DIRECT_CHECKS_S = (3.0, 7.0)
POWER_ON_DIRECT_CHECK_TIMEOUT_S = 0.8

# --- Presence fallback probe ---


async def presence_probe_up(
    session: aiohttp.ClientSession,
    tv_ip: str,
    timeout_s: float = 1.5,
    attempts: int = 2,
) -> bool:
    """
    Lightweight HTTP fallback probe for Samsung's renderer endpoint.

    This is *not* the primary source of truth for TV presence.
    SSDP alive/byebye plus startup M-SEARCH bootstrap remain the preferred signals.
    """
    url = f"http://{tv_ip}:9197/dmr"
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    for _ in range(max(1, attempts)):
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

def _default_wol_broadcasts(tv_ip: str) -> list[str]:
    """WoL targets: the limited broadcast (tested) plus the TV's /24 directed broadcast."""
    out: list[str] = ["255.255.255.255"]

    parts = (tv_ip or "").strip().split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        directed = ".".join(parts[:3] + ["255"])
        if directed not in out:
            out.append(directed)

    return out

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
            with open(self._token_file, "r", encoding="utf-8") as f:
                tok = f.read().strip()
        except FileNotFoundError:
            tok = ""
        except Exception:
            logger.debug("failed to read tv token", exc_info=True)
            tok = ""
        self.state.token_present = bool(tok)
        return tok

    def _refresh_token_present(self) -> None:
        tok = self._read_token()
        token_present = bool(tok)
        if self._logged_token_present is None or token_present != self._logged_token_present:
            self._logged_token_present = token_present
            logger.debug(
                "started tv_ip=%s token_present=%s",
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
        close_reason = "rx_loop_ended"
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.CLOSE:
                    close_reason = "ws_close"
                    break
                if msg.type == aiohttp.WSMsgType.CLOSED:
                    close_reason = "ws_closed"
                    break
                if msg.type == aiohttp.WSMsgType.ERROR:
                    close_reason = "ws_error"
                    break

                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    continue

                data = (payload or {}).get("data")
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except ValueError:
                        data = None

                if isinstance(data, dict):
                    tok = data.get("token")
                    if isinstance(tok, str) and tok.strip():
                        self._write_token(tok)

        except asyncio.CancelledError:
            close_reason = "cancelled"
            raise
        except Exception as exc:
            close_reason = f"rx_exception:{exc!r}"
            logger.debug("rx loop ended: %r", exc)
        finally:
            async with self._lock:
                # Only clear/log if this RX loop belongs to the current websocket.
                if self._ws is ws:
                    self._ws = None
                    self.state.connected = False

                    if self._logged_connected is True:
                        self._logged_connected = False
                        logger.debug(
                            "websocket disconnected tv_ip=%s reason=%s",
                            self._tv_ip,
                            close_reason,
                        )

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
                    logger.debug("websocket connected tv_ip=%s", self._tv_ip)

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
                    logger.debug(
                        "websocket disconnected tv_ip=%s reason=connect_failed error=%r",
                        self._tv_ip,
                        exc,
                    )
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
                logger.debug("websocket disconnected tv_ip=%s reason=local_close", self._tv_ip)

        if ws and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()

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
            with contextlib.suppress(Exception):
                await ws_to_close.close()
        return False


# --- Controller ---


class TvController:
    def __init__(
        self,
        *,
        tv_ip: str,
        tv_mac: str,
        token_file: str,
        name: str,
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
            "started tv_ip=%s tv_mac=%s token_present=%s",
            tv_ip,
            tv_mac,
            "true" if self.ws.state.token_present else "false",
        )

        self._session: Optional[aiohttp.ClientSession] = None
        self._presence_cached: bool | None = None
        self._presence_source: str = "unknown"
        self._presence_changed_at: float | None = None

        # Fires immediately when any trusted presence path marks the TV on.
        # This lets power_on() stop WoL / key sends without polling.
        self._presence_on_event = asyncio.Event()

        self._power_on_active: bool = False

        # Guards the key send in power_on(): one key per power-on attempt, never
        # queued or retried.
        self._power_on_key_lock = asyncio.Lock()
        self._power_on_attempt_id = 0

        self.initial = InitialStatus(logger)

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
        task, self._ws_warm_task = self._ws_warm_task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        await self.ws.close()

        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    # Presence cache is the local truth used by flows, start-up and health. It is
    # context only: a change of power state never triggers anything by itself.
    # Updating presence here must not implicitly tear down the websocket control
    # channel. Presence and websocket usability are related but not identical.

    def _commit_presence(self, on: bool, *, source: str) -> bool:
        # Keep the event aligned even if the cached value did not change.
        # This matters when another task starts waiting after presence is already true.
        if self._presence_cached is on:
            if on:
                self._presence_on_event.set()
            else:
                self._presence_on_event.clear()
            return False

        self._presence_cached = on
        self._presence_source = source
        token = "" if self.ws.state.token_present else " token=missing"
        self.initial.mark_received(f"power={'on' if on else 'off'} via={source}{token}")
        self._presence_changed_at = time.time()

        # power_on() stops the instant presence turns true.
        if on:
            self._presence_on_event.set()
        else:
            self._presence_on_event.clear()

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

        # Neither the network search nor the /dmr probe found the TV: it is off.
        # (Same test refresh_presence uses; an SSDP alive corrects it the moment
        # the TV comes on.) This also gives a TV that is off at start-up a
        # definite first reading instead of staying unknown.
        self._commit_presence(False, source="probe_http_down")
        logger.debug("tv reconcile complete tv_ip=%s: no reply, off", self.tv_ip)

        return {
            "outcome": "present_false",
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
        return TvSnapshot(
            backend=BACKEND_WS,
            presence_on=self._presence_cached,
            presence_source=self._presence_source,
            changed_at=self._presence_changed_at,
            ws_connected=st.connected,
            token_present=st.token_present,
            last_error=st.last_error,
        )

    async def _wait_for_presence_true(self, *, timeout_s: float) -> bool:
        if self._presence_cached is True:
            self._presence_on_event.set()
            return True

        try:
            await asyncio.wait_for(self._presence_on_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return False

        return self._presence_cached is True

    async def refresh_presence(self) -> None:
        """Confirm a cached "on" before acting on it.

        Presence comes from SSDP, so a missed byebye leaves the TV looking on
        when it is off. Only "on" is re-checked: that is the state that makes a
        flow skip power-on or send the KEY_POWER toggle to a TV that is off.
        The probe answers in milliseconds when the TV really is on.
        """
        if not self._session or self._presence_cached is not True:
            return
        if not await presence_probe_up(self._session, self.tv_ip):
            logger.info("tv presence was stale (cached on, probe down); now off")
            self._commit_presence(False, source="probe_http_down")

    async def power_off(self, *, wait: bool = True, timeout_s: float = 25.0) -> bool:
        if not self._session:
            return False

        # KEY_POWER toggles: sending it to a TV that is actually off turns it on.
        await self.refresh_presence()
        if self._presence_cached is False:
            return True

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

        # Single-flight behaviour: only the first caller runs WoL / the key send.
        # Any overlapping caller just waits for presence to become true.
        if self._power_on_active:
            return await self._wait_for_presence_true(timeout_s=timeout_s)

        self._power_on_active = True
        stop_event = asyncio.Event()

        # Give this power-on attempt a unique id. The key-send guard checks this
        # immediately before sending, so a stale worker cannot send later.
        self._power_on_attempt_id += 1
        attempt_id = self._power_on_attempt_id
        key_sent = False

        async def wol_worker() -> None:
            # One magic packet per target, straight away and then every interval.
            # Tested on the kitchen QE32Q50A: WoL is ignored while the TV lingers
            # after switch-off (network up for ~17-19 s), and one packet wakes it
            # within ~1 s once the linger ends. Repeating covers the linger.
            WOL_INTERVAL_S = 0.5
            broadcasts = _default_wol_broadcasts(self.tv_ip)

            while not stop_event.is_set() and self._presence_cached is not True:
                for broadcast in broadcasts:
                    try:
                        send_wol(self.tv_mac, broadcast=broadcast)
                    except Exception:
                        logger.debug("tv wol send failed broadcast=%s", broadcast, exc_info=True)

                # Do not sleep blindly. Wake immediately if presence arrives.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=WOL_INTERVAL_S)

        async def key_worker() -> None:
            nonlocal key_sent

            # Immediate path: no artificial delay. WoL and the websocket connect both
            # start straight away. The connect may be retried, but the key is sent once.
            #
            # This is what wakes a TV during its post-switch-off linger, when WoL is
            # ignored. Tested: a key over the websocket that was already open before
            # switch-off wakes it at 4, 9 and 14 s into the linger; a freshly opened
            # socket late in the linger does not. So the open socket must be left
            # alone, never closed or replaced because presence went false.
            #
            # KEY_0, not KEY_POWER, on purpose: it wakes a lingering TV just the same,
            # and it is harmless to a TV that is in fact already on, where KEY_POWER
            # would switch it off.
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
                    # in flight. Re-check before sending the key.
                    if stop_event.is_set() or self._presence_cached is True:
                        return

                    if ws_connected:
                        async with self._power_on_key_lock:
                            # Final guard immediately before sending the toggle.
                            if attempt_id != self._power_on_attempt_id:
                                return
                            if key_sent:
                                return
                            if stop_event.is_set() or self._presence_cached is True:
                                return

                            # From this point on, treat the key as sent even if send_key()
                            # returns False. A local websocket send failure can be
                            # ambiguous: the TV may still have received the frame.
                            key_sent = True
                            sent = await self.ws.send_key("KEY_0")
                            logger.debug(
                                "tv power_on one-shot KEY_0 attempted tv_ip=%s sent=%s",
                                self.tv_ip,
                                "true" if sent else "false",
                            )
                            return

                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("tv power_on websocket connect/send path failed", exc_info=True)

                # Connection failure is not a key attempt. Retry connection quickly
                # until presence arrives or power_on() times out/cancels this worker.
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=WS_CONNECT_RETRY_INTERVAL_S,
                    )
                except asyncio.TimeoutError:
                    pass

        async def direct_check_worker() -> None:
            # Does not wake the TV. It only covers a lost "alive": the TV is asked
            # directly at fixed moments, never continuously.
            loop = asyncio.get_running_loop()
            began = loop.time()

            for at_s in POWER_ON_DIRECT_CHECKS_S:
                wait = began + at_s - loop.time()
                if wait > 0:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=wait)
                if stop_event.is_set() or self._presence_cached is True:
                    return
                try:
                    if await presence_probe_up(
                        self._session,
                        self.tv_ip,
                        timeout_s=POWER_ON_DIRECT_CHECK_TIMEOUT_S,
                        attempts=1,
                    ):
                        self._commit_presence(True, source="probe_http_up")
                        return
                except Exception:
                    logger.debug("tv power_on direct check failed", exc_info=True)

        try:
            wol_task = asyncio.create_task(wol_worker(), name="tv:power_on_wol")
            key_task = asyncio.create_task(key_worker(), name="tv:power_on_key")
            probe_task = asyncio.create_task(direct_check_worker(), name="tv:power_on_direct_check")

            try:
                powered_on = await self._wait_for_presence_true(timeout_s=timeout_s)
                return powered_on
            finally:
                # The moment presence is true, or the attempt times out, stop all
                # power-on behaviour. This cancels WoL, the direct check, and any
                # in-flight websocket key path that has not sent yet.
                stop_event.set()

                for task in (wol_task, key_task, probe_task):
                    if not task.done():
                        task.cancel()

                await asyncio.gather(
                    wol_task,
                    key_task,
                    probe_task,
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

# --- SSDP discovery and bootstrap ---


_MCAST_GRP = "239.255.255.250"
_MCAST_PORT = 1900
_MSEARCH_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
_MSEARCH_BURST_COUNT = 3
_MSEARCH_BURST_GAP_S = 0.4


class _SsdpNotifyProtocol(asyncio.DatagramProtocol):
    """Forwards SSDP NOTIFY packets from the configured TV to the controller."""

    def __init__(self, tv: TvController, closed: asyncio.Future) -> None:
        self._tv = tv
        self._closed = closed

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if addr[0] != self._tv.tv_ip:
            return

        txt = data.decode("utf-8", errors="ignore")
        if "NOTIFY * HTTP/1.1" not in txt:
            return

        hdr = _parse_headers(txt)
        acted = self._tv.notify_ssdp(
            nts=hdr.get("NTS", ""),
            nt=hdr.get("NT", ""),
            usn=hdr.get("USN", ""),
            location=hdr.get("LOCATION"),
            source="ssdp",
        )
        if acted and hdr.get("NTS", "") == "ssdp:alive":
            asyncio.create_task(self._connect_ws(), name="tv:ssdp_ws_connect")

    async def _connect_ws(self) -> None:
        try:
            await self._tv.ensure_ws_connected()
        except Exception:
            logger.debug("tv:ssdp ws connect failed after alive", exc_info=True)

    def error_received(self, exc: Exception) -> None:
        logger.debug("tv:ssdp socket error: %r", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self._closed.done():
            self._closed.set_result(exc)


async def ssdp_listener(tv: Any) -> None:
    """Listen for SSDP NOTIFY from the configured TV IP and forward to its notify_ssdp().

    Used by both TV backends (TvController, SamsungFrameTv).

    Runs on the event loop (no reader thread), so cancelling it stops it at once.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", _MCAST_PORT))
        mreq = socket.inet_aton(_MCAST_GRP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
    except Exception:
        sock.close()
        raise

    loop = asyncio.get_running_loop()
    closed: asyncio.Future = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _SsdpNotifyProtocol(tv, closed), sock=sock
    )
    try:
        exc = await closed
        if exc is not None:
            raise exc
    finally:
        transport.close()


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
        sock.close()


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
