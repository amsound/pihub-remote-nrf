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
    Maintains a websocket connection while TV is on.
    Sends KEY_* messages quickly with no reconnect per key.
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

    async def connect(self, session: aiohttp.ClientSession, *, timeout_s: float = 2.0) -> bool:
        async with self._lock:
            if self._ws and not self._ws.closed:
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

        async with self._lock:
            ws = self._ws
            if ws is None or ws.closed:
                self.state.connected = False
                return False
            try:
                await ws.send_str(json.dumps(payload))
                return True
            except Exception as exc:
                self.state.connected = False
                self.state.last_error = repr(exc)
                return False


# --- Controller ---


@dataclass
class TvSnapshot:
    initialized: bool
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
        self._power_on_active: bool = False
        self._last_power_off_request_ts: float | None = None
        self._state_change_callback = state_change_callback

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        await self.ws.close()
        sess, self._session = self._session, None
        if sess:
            await sess.close()

    def _commit_presence(self, on: bool, *, source: str) -> bool:
        prev_on = self._presence_cached is True
        if self._presence_cached is on:
            return False

        now = asyncio.get_running_loop().time()
        self._presence_cached = on
        self._presence_source = source
        self._presence_last_change_ts = now

        curr_on = self._presence_cached is True
        if not prev_on and curr_on:
            self._emit_state_change(
                "watch",
                {
                    "domain": "tv",
                    "presence_source": source,
                },
            )
        return True

    def notify_msearch(self, *, location: str | None) -> bool:
        if location and "/dmr" not in location:
            logger.info("tv msearch rejected location=%s", location)
            return False

        changed = self._commit_presence(True, source="msearch")
        logger.info(
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

    def snapshot(self) -> TvSnapshot:
        st = self.ws.state
        last_change_age_s: int | None = None
        if self._presence_last_change_ts is not None:
            last_change_age_s = int(asyncio.get_running_loop().time() - self._presence_last_change_ts)
        return TvSnapshot(
            initialized=self._presence_cached is not None,
            presence_on=self._presence_cached,
            presence_source=self._presence_source,
            last_change_age_s=last_change_age_s,
            ws_connected=st.connected,
            token_present=st.token_present,
            last_error=st.last_error,
        )

    def _emit_state_change(self, name: str, payload: dict[str, Any]) -> None:
        cb = self._state_change_callback
        if cb is None:
            return
        try:
            asyncio.create_task(cb(name, payload))
        except Exception:
            logger.exception("tv state change callback failed name=%s", name)

    async def _wait_for_presence_true(self, *, timeout_s: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            if self._presence_cached is True:
                return True
            await asyncio.sleep(0.1)
        return False

    async def power_off(self, *, wait: bool = True, timeout_s: float = 25.0) -> bool:
        if not self._session:
            return False
        if self._presence_cached is False:
            return True

        now = asyncio.get_running_loop().time()
        self._last_power_off_request_ts = now

        ok = await self.ws.send_key("KEY_POWER")
        if not ok:
            await self.ws.connect(self._session)
            ok = await self.ws.send_key("KEY_POWER")

        if not wait:
            return ok

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            if self._presence_cached is False:
                return True
            if not await presence_probe_up(self._session, self.tv_ip):
                self._commit_presence(False, source="probe_http_down")
                return True
            await asyncio.sleep(0.2)
        return False

    async def power_on(self, *, timeout_s: float = 60.0) -> bool:
        if not self._session:
            return False
        if self._presence_cached is True:
            return True
        if self._power_on_active:
            return await self._wait_for_presence_true(timeout_s=timeout_s)

        self._power_on_active = True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            start = loop.time()

            RECOVERY_WINDOW_S = 30.0
            WOL_FAST_INTERVAL_S = 0.25
            WOL_SLOW_INTERVAL_S = 1.0
            WOL_FAST_WINDOW_S = 2.0
            WS_FAST_INTERVAL_S = 0.25
            WS_SLOW_INTERVAL_S = 0.8
            WS_FAST_WINDOW_S = 2.0
            HTTP_PROBE_INTERVAL_S = 1.0
            HTTP_PROBE_START_DELAY_S = 2.0

            rescue_recent_off = (
                self._last_power_off_request_ts is not None
                and (start - self._last_power_off_request_ts) <= RECOVERY_WINDOW_S
            )
            rescue_toggle_attempted = False

            last_wol = -1e9
            last_ws_attempt = -1e9
            last_http_probe = -1e9

            while loop.time() < deadline:
                now = loop.time()
                elapsed = now - start

                if self._presence_cached is True:
                    await self.ws.connect(self._session)
                    return True

                wol_interval_s = WOL_FAST_INTERVAL_S if elapsed < WOL_FAST_WINDOW_S else WOL_SLOW_INTERVAL_S
                ws_interval_s = WS_FAST_INTERVAL_S if elapsed < WS_FAST_WINDOW_S else WS_SLOW_INTERVAL_S

                if (now - last_wol) >= wol_interval_s:
                    try:
                        await send_wol_burst(
                            self.tv_mac,
                            count=3,
                            gap_s=0.25,
                            port=9,
                            broadcast="255.255.255.255",
                        )
                    except Exception:
                        logger.debug("tv wol burst failed", exc_info=True)
                    last_wol = now

                ws_connected_now = self.ws.state.connected
                if not ws_connected_now and (now - last_ws_attempt) >= ws_interval_s:
                    last_ws_attempt = now
                    try:
                        ws_connected_now = await self.ws.connect(self._session)
                    except Exception:
                        ws_connected_now = False

                if rescue_recent_off and ws_connected_now and not rescue_toggle_attempted:
                    try:
                        rescue_toggle_attempted = True
                        await self.ws.send_key("KEY_POWER")
                    except Exception:
                        logger.debug("tv rescue power toggle failed", exc_info=True)

                if elapsed >= HTTP_PROBE_START_DELAY_S and (now - last_http_probe) >= HTTP_PROBE_INTERVAL_S:
                    last_http_probe = now
                    try:
                        if await presence_probe_up(self._session, self.tv_ip):
                            self._commit_presence(True, source="probe_http_up")
                            await self.ws.connect(self._session)
                            return True
                    except Exception:
                        logger.debug("tv http presence probe failed", exc_info=True)

                await asyncio.sleep(0.2)
            return False
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
    """Start passive SSDP listener plus one-shot M-SEARCH bootstrap."""
    return [
        asyncio.create_task(ssdp_listener(tv), name="tv:ssdp"),
        asyncio.create_task(msearch_bootstrap(tv), name="tv:msearch_bootstrap"),
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
