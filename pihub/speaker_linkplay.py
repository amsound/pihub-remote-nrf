"""
LinkPlay/WiiM speaker controller.

Option B:
- UPnP (GENA NOTIFY / LastChange) for state updates (push)
- UPnP actions for transport + volume/mute
- LinkPlay HTTPS httpapi.asp only for "play URL" (self-signed cert => ssl=False)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

from async_upnp_client.aiohttp import AiohttpNotifyServer
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.event_handler import UpnpEventHandler
from async_upnp_client.exceptions import UpnpError
from async_upnp_client.profiles.dlna import DmrDevice, TransportState
from async_upnp_client.ssdp import SSDP_IP_V4, SSDP_PORT

logger = logging.getLogger(__name__)

_HTTPAPI_PATH = "/httpapi.asp"
_HTTP_TIMEOUT_S = 10

# These are best-effort mappings; firmwares vary.
_STORAGE_MEDIUM_TO_SOURCE = {
    "HDMI": "hdmi",
    "OPTICAL": "optical",
    "LINE-IN": "line-in",
    "BLUETOOTH": "bluetooth",
    "AIRPLAY": "airplay",
    "MULTIROOM-SLAVE": "multiroom-secondary",
    "RADIO-NETWORK": "wifi",
    "SONGLIST-NETWORK": "wifi",
}
# Treat anything ending with -NETWORK as wifi, and SONGLIST- prefix as wifi.
_NETWORK_PREFIXES = ("SONGLIST-",)


@dataclass
class SpeakerState:
    reachable: bool = False
    subscribed: bool = False
    last_error: str | None = None

    transport: str | None = None  # playing/paused/stopped/idle/unknown
    volume: float | None = None   # 0..1
    muted: bool | None = None

    source: str | None = None     # wifi/bluetooth/line-in/optical/hdmi/airplay/...
    track_uri: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None

    last_event_ts: float | None = None


try:
    from async_upnp_client.const import HttpResponse  # type: ignore
except Exception:
    class HttpResponse:  # type: ignore
        def __init__(self, status_code: int, headers: dict[str, str], body: str | bytes | None):
            self.status_code = status_code
            self.headers = headers
            self.body = body


class _LocalAiohttpRequester:
    """
    Requester compatible with async-upnp-client 0.4x.

    For normal HTTP (GET/POST etc) we use aiohttp.
    For SUBSCRIBE/UNSUBSCRIBE we send a raw HTTP/1.1 request because some
    LinkPlay/WiiM stacks disconnect on aiohttp’s wire format even when the
    same headers work with curl.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def async_http_request(self, request, **_kwargs):
        method = getattr(request, "method", None)
        url = getattr(request, "url", None)
        headers = getattr(request, "headers", None)
        body = getattr(request, "body", None)
        timeout = getattr(request, "timeout", None)

        if headers is None:
            headers = {}

        if not isinstance(method, str) or not isinstance(url, str):
            raise TypeError(f"Unsupported request object: {request!r}")

        m_upper = method.upper()

        # Normalize timeout for client request itself
        total = 10.0
        if timeout is not None:
            try:
                total = float(timeout)
            except Exception:
                try:
                    total = float(timeout.total_seconds())
                except Exception:
                    total = 10.0

        # ---- SPECIAL CASE: SUBSCRIBE/UNSUBSCRIBE via raw socket (curl-style) ----
        if m_upper in {"SUBSCRIBE", "UNSUBSCRIBE"}:
            return await self._raw_subscribe_like_curl(
                method=m_upper,
                url=url,
                headers=headers,
                timeout_s=total,
            )

        # ---- Default: aiohttp for everything else ----
        ssl = False if url.startswith("https://") else None

        async with self._session.request(
            method=method,
            url=url,
            headers=headers,
            data=body,
            timeout=aiohttp.ClientTimeout(total=total),
            ssl=ssl,
        ) as resp:
            raw = await resp.read()
            hdrs = {k: v for k, v in resp.headers.items()}
            try:
                text_body: str = raw.decode("utf-8", errors="ignore")
            except Exception:
                text_body = ""

            try:
                return HttpResponse(status_code=resp.status, headers=hdrs, body=text_body)  # type: ignore
            except TypeError:
                return HttpResponse(resp.status, hdrs, text_body)  # type: ignore

    async def _raw_subscribe_like_curl(self, *, method: str, url: str, headers, timeout_s: float):
        """
        Send SUBSCRIBE/UNSUBSCRIBE exactly like curl:
          SUBSCRIBE /path HTTP/1.1
          Host: ip:port
          ...

        Returns HttpResponse(status_code, headers, body_text).
        """
        from urllib.parse import urlsplit

        u = urlsplit(url)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")

        # Build a plain dict view for manipulation/logging without destroying original structure
        try:
            hdict = dict(headers)
        except Exception:
            hdict = {}

        # Force the “known good” bits (matching your curl)
        hdict.setdefault("Host", f"{host}:{port}")
        hdict.setdefault("User-Agent", "HomeAssistant/async_upnp_client")
        hdict["TIMEOUT"] = "Second-1800"
        hdict.setdefault("Connection", "close")

        # IMPORTANT: use CALLBACK uppercase (matching the curl that succeeded)
        if "Callback" in hdict and "CALLBACK" not in hdict:
            hdict["CALLBACK"] = hdict.pop("Callback")

        # Debug
        logger.warning("[speaker] %s(raw) %s headers=%s", method, url, hdict)

        # Compose raw HTTP request (CRLF line endings)
        lines = [f"{method} {path} HTTP/1.1"]
        for k, v in hdict.items():
            # Skip any None values
            if v is None:
                continue
            lines.append(f"{k}: {v}")
        lines.append("")  # blank line
        lines.append("")  # end
        raw_req = "\r\n".join(lines).encode("utf-8")

        reader = writer = None
        try:
            # LinkPlay UPnP services are http; but respect scheme just in case
            if u.scheme == "https":
                import ssl as _ssl
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=ctx),
                    timeout=timeout_s,
                )
            else:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=timeout_s,
                )

            writer.write(raw_req)
            await writer.drain()

            # Read response headers
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout_s)
                if not chunk:
                    break
                data += chunk
                if len(data) > 65536:
                    break

            head, _, _rest = data.partition(b"\r\n\r\n")
            text = head.decode("iso-8859-1", errors="ignore")
            lines = text.split("\r\n")
            status_line = lines[0] if lines else ""
            # Parse status
            status_code = 0
            try:
                parts = status_line.split()
                if len(parts) >= 2:
                    status_code = int(parts[1])
            except Exception:
                status_code = 0

            resp_headers: dict[str, str] = {}
            for ln in lines[1:]:
                if ":" not in ln:
                    continue
                k, v = ln.split(":", 1)
                resp_headers[k.strip()] = v.strip()

            try:
                return HttpResponse(status_code=status_code, headers=resp_headers, body="")  # type: ignore
            except TypeError:
                return HttpResponse(status_code, resp_headers, "")  # type: ignore

        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()


class LinkPlaySpeaker:
    def __init__(
        self,
        *,
        host: str,
        http_scheme: str = "https",
        volume_step_pct: int = 2,
    ) -> None:
        self._host = host.strip()
        self._location_override = (os.getenv("SPEAKER_LOCATION", "") or "").strip()  # optional full URL
        self._http_scheme = (http_scheme or "https").strip().lower()
        if self._http_scheme not in {"http", "https"}:
            self._http_scheme = "https"

        # MUST be 2%: hard-force.
        self._step = 0.02

        self._session: aiohttp.ClientSession | None = None

        # UPnP pieces
        self._notify_server: AiohttpNotifyServer | None = None
        self._event_handler: UpnpEventHandler | None = None
        self._device: DmrDevice | None = None

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        # snapshot state
        self._state = SpeakerState()

        # last-known UPnP “raw” fields used for source inference
        self._playback_storage_medium: str | None = None
        self._avtransport_uri: str | None = None
        self._current_track_uri: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._host)

    @property
    def state(self) -> SpeakerState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        s = self._state

        def _pct(v: float | None) -> int | None:
            if v is None:
                return None
            return int(round(max(0.0, min(1.0, v)) * 100.0))

        return {
            "reachable": s.reachable,
            "subscribed": s.subscribed,
            "last_error": s.last_error,
            "transport": s.transport,
            "volume_pct": _pct(s.volume),
            "muted": s.muted,
            "source": s.source,
            "track_uri": s.track_uri,
            "title": s.title,
            "artist": s.artist,
            "album": s.album,
            "last_event_ts": s.last_event_ts,
        }

    async def start(self) -> None:
        if not self.enabled:
            return
        if self._task is not None:
            return

        self._stop.clear()
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._runner(), name="speaker_linkplay")

    async def stop(self) -> None:
        self._stop.set()
        t, self._task = self._task, None
        if t:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        await self._disconnect_upnp()

        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None

    # -------------------- Public actions (drop if unreachable) --------------------

    async def volume_up(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            current = dev.volume_level
            if current is None:
                await dev.async_update()
                current = dev.volume_level
            if current is None:
                return
            target = min(1.0, max(0.0, float(current) + self._step))
            await dev.async_set_volume_level(target)
            self._state.volume = target
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"volume_up: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def volume_down(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            current = dev.volume_level
            if current is None:
                await dev.async_update()
                current = dev.volume_level
            if current is None:
                return
            target = min(1.0, max(0.0, float(current) - self._step))
            await dev.async_set_volume_level(target)
            self._state.volume = target
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"volume_down: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def mute_toggle(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            muted = dev.is_volume_muted
            if muted is None:
                await dev.async_update()
                muted = dev.is_volume_muted
            if muted is None:
                return
            await dev.async_mute_volume(not bool(muted))
            self._state.muted = (not bool(muted))
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"mute_toggle: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def play(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            await dev.async_play()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"play: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def pause(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            await dev.async_pause()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"pause: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def stop_playback(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            await dev.async_stop()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"stop: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def next_track(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            await dev.async_next_track()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"next: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def previous_track(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            await dev.async_previous_track()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"previous: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def toggle_play(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            st = dev.transport_state
            if st in (TransportState.PLAYING, TransportState.TRANSITIONING):
                await dev.async_pause()
            else:
                await dev.async_play()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"toggle_play: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def play_url(self, url: str) -> None:
        if not url or not isinstance(url, str):
            return
        if self._session is None:
            return
        if not self._state.reachable:
            return

        cmd = f"setPlayerCmd:play:{url}"
        cmd_q = quote(cmd, safe=":/?&=%")
        endpoint = f"{self._http_scheme}://{self._host}{_HTTPAPI_PATH}?command={cmd_q}"

        try:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
            async with self._session.get(endpoint, timeout=timeout, ssl=False) as resp:
                await resp.read()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"play_url: {err!r}"
            self._state.reachable = False

    # -------------------- Internal runner --------------------

    async def _runner(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_subscribe()
                backoff = 1.0
                while not self._stop.is_set() and self._device is not None:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                logger.exception("[speaker] runner exception")
                self._state.last_error = f"runner: {err!r}"
                # TEMP DEBUG: keep notify server up for manual reachability testing
                # await self._disconnect_upnp()
                self._state.reachable = False
                self._state.subscribed = False
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.6)

    async def _connect_and_subscribe(self) -> None:
        await self._disconnect_upnp()

        # 1) Location: prefer explicit URL (no discovery), else try SSDP
        location = (os.getenv("SPEAKER_LOCATION", "") or "").strip()
        if not location:
            location = await self._ssdp_find_location_for_host(self._host)

        if not location:
            self._state.reachable = False
            self._state.subscribed = False
            self._state.last_error = "ssdp: no LOCATION response (set SPEAKER_LOCATION to bypass discovery)"
            return

        # 2) Determine callback/bind IP (keep your env override)
        cb_ip = (os.getenv("SPEAKER_CALLBACK_IP", "") or "").strip()
        if cb_ip:
            local_ip = cb_ip
        else:
            local_ip = self._get_local_ip_for_peer(self._host)

        if self._session is None:
            raise RuntimeError("speaker aiohttp session not initialized")

        requester = _LocalAiohttpRequester(self._session)

        # ---- PINNED notify callback port (so VLAN rules can be fixed) ----
        notify_port_raw = (os.getenv("SPEAKER_NOTIFY_PORT", "") or "19090").strip()
        try:
            notify_port = int(notify_port_raw)
        except ValueError:
            notify_port = 19090

        # Notify server: force a stable (ip, port) source tuple.
        # Also try to force the callback host/public IP to be routable.
        notify_server = None
        last_exc: Exception | None = None

        for ctor in (
            # Preferred: explicit source tuple + explicit public/callback host where supported
            lambda: AiohttpNotifyServer(requester, source=(local_ip, notify_port), public_ip=local_ip),
            lambda: AiohttpNotifyServer(requester, source=(local_ip, notify_port), callback_host=local_ip),
            lambda: AiohttpNotifyServer(requester, source=(local_ip, notify_port)),
            # Older variants: positional tuple / listen tuple
            lambda: AiohttpNotifyServer(requester, (local_ip, notify_port), public_ip=local_ip),
            lambda: AiohttpNotifyServer(requester, (local_ip, notify_port)),
            lambda: AiohttpNotifyServer(requester, listen=(local_ip, notify_port)),
        ):
            try:
                notify_server = ctor()
                break
            except TypeError as exc:
                last_exc = exc
                continue

        if notify_server is None:
            raise RuntimeError(f"Unable to construct AiohttpNotifyServer: {last_exc!r}")

        self._notify_server = notify_server
        await self._notify_server.async_start_server()

        callback_url = getattr(self._notify_server, "callback_url", None)
        logger.warning("[speaker] notify callback_url=%s (pinned port=%s)", callback_url, notify_port)

        # Event handler: your version requires requester
        try:
            self._event_handler = UpnpEventHandler(self._notify_server, requester)
        except TypeError:
            self._event_handler = UpnpEventHandler(self._notify_server, requester=requester)

        factory = UpnpFactory(requester)
        upnp_device = await factory.async_create_device(location)
        dmr = DmrDevice(upnp_device, self._event_handler)
        dmr.on_event = self._on_event

        # Subscribe (auto-renew)
        await dmr.async_subscribe_services(auto_resubscribe=True)

        self._device = dmr
        self._state.reachable = True
        self._state.subscribed = True
        self._state.last_error = None

        # Prime state once
        with contextlib.suppress(Exception):
            await dmr.async_update()
            self._refresh_from_device(dmr)

        logger.info("[speaker] subscribed via UPnP callback=%s location=%s", callback_url, location)

    async def _disconnect_upnp(self) -> None:
        d, self._device = self._device, None
        if d is not None:
            with contextlib.suppress(Exception):
                d.on_event = None
            with contextlib.suppress(Exception):
                await d.async_unsubscribe_services()

        eh, self._event_handler = self._event_handler, None
        if eh is not None:
            with contextlib.suppress(Exception):
                await eh.async_stop()

        ns, self._notify_server = self._notify_server, None
        if ns is not None:
            with contextlib.suppress(Exception):
                await ns.async_stop_server()

    async def _mark_unreachable_maybe(self, err: Exception) -> None:
        if isinstance(err, UpnpError):
            self._state.reachable = False
            self._state.subscribed = False
            await self._disconnect_upnp()

    # -------------------- UPnP event callback + state extraction --------------------

    def _on_event(self, service: Any, state_variables: Any) -> None:
        self._state.last_event_ts = time.time()

        d = self._device
        if d is None:
            return

        try:
            self._refresh_from_device(d)
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"on_event refresh: {err!r}"

        try:
            for sv in (state_variables or []):
                name = getattr(sv, "name", None)
                val = getattr(sv, "value", None)
                if name == "PlaybackStorageMedium":
                    self._playback_storage_medium = self._norm(val)
                elif name == "AVTransportURI":
                    self._avtransport_uri = self._norm(val)
                elif name == "CurrentTrackURI":
                    self._current_track_uri = self._norm(val)
        except Exception:
            pass

        self._state.source = self._infer_source()

    def _refresh_from_device(self, d: DmrDevice) -> None:
        ts = d.transport_state
        if ts in (TransportState.PLAYING, TransportState.TRANSITIONING):
            self._state.transport = "playing"
        elif ts in (TransportState.PAUSED_PLAYBACK, TransportState.PAUSED_RECORDING):
            self._state.transport = "paused"
        elif ts == TransportState.STOPPED:
            self._state.transport = "stopped"
        elif ts is None:
            self._state.transport = "unknown"
        else:
            self._state.transport = str(ts).lower()

        if d.volume_level is not None:
            self._state.volume = float(d.volume_level)
        if d.is_volume_muted is not None:
            self._state.muted = bool(d.is_volume_muted)

        uri = getattr(d, "current_track_uri", None)
        if isinstance(uri, str) and uri.strip():
            self._state.track_uri = uri
            self._current_track_uri = self._norm(uri)

        title = getattr(d, "media_title", None)
        if isinstance(title, str) and title.strip():
            self._state.title = title

        artist = getattr(d, "media_artist", None)
        if isinstance(artist, str) and artist.strip():
            self._state.artist = artist

        album = getattr(d, "media_album_name", None)
        if isinstance(album, str) and album.strip():
            self._state.album = album

        self._state.source = self._infer_source()

    def _infer_source(self) -> str | None:
        m = (self._playback_storage_medium or "").strip().upper()
        if m:
            if m in _STORAGE_MEDIUM_TO_SOURCE:
                return _STORAGE_MEDIUM_TO_SOURCE[m]
            if m.endswith("-NETWORK") or any(m.startswith(p) for p in _NETWORK_PREFIXES):
                return "wifi"

        for u in (self._current_track_uri, self._avtransport_uri, self._state.track_uri):
            s = (u or "").strip()
            if not s:
                continue
            sl = s.lower()
            su = s.upper()

            if "wiimu_airplay" in sl:
                return "airplay"

            if su in _STORAGE_MEDIUM_TO_SOURCE:
                return _STORAGE_MEDIUM_TO_SOURCE[su]

            if su.endswith("-NETWORK") or any(su.startswith(p) for p in _NETWORK_PREFIXES):
                return "wifi"

            if sl.startswith("http://") or sl.startswith("https://"):
                return "wifi"

        return None

    @staticmethod
    def _norm(v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        if s.upper() in {"NONE", "UNKNOWN"}:
            return None
        return s

    # -------------------- SSDP helper (static host -> LOCATION) --------------------

    async def _ssdp_find_location_for_host(self, host: str) -> str | None:
        st_list = [
            "urn:schemas-upnp-org:device:MediaRenderer:1",
            "urn:schemas-upnp-org:device:MediaRenderer:2",
            "urn:schemas-upnp-org:device:MediaRenderer:3",
        ]

        for st in st_list:
            loc = await self._ssdp_msearch_once(host, st=st, mx=2, timeout=2.5)
            if loc:
                return loc

        return await self._ssdp_msearch_once(host, st="ssdp:all", mx=2, timeout=2.5)

    async def _ssdp_msearch_once(self, host: str, *, st: str, mx: int, timeout: float) -> str | None:
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_IP_V4}:{SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {mx}\r\n"
            f"ST: {st}\r\n"
            "\r\n"
        ).encode("utf-8")

        loop = asyncio.get_running_loop()
        found_location: str | None = None
        done = asyncio.Event()

        class _SSDPProto(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                try:
                    transport.sendto(msg, (SSDP_IP_V4, SSDP_PORT))
                except Exception:
                    pass

            def datagram_received(self, data: bytes, addr):
                nonlocal found_location
                try:
                    sender_ip = addr[0]
                except Exception:
                    return
                if sender_ip != host:
                    return

                headers = self._parse_ssdp_response(data)
                loc = headers.get("location")
                if loc:
                    found_location = loc
                    done.set()

        transport = None
        try:
            transport, _proto = await loop.create_datagram_endpoint(
                lambda: _SSDPProto(),
                local_addr=("0.0.0.0", 0),
                allow_broadcast=True,
            )
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        finally:
            if transport is not None:
                transport.close()

        return found_location

    @staticmethod
    def _parse_ssdp_response(data: bytes) -> dict[str, str]:
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            return {}
        lines = [ln.strip() for ln in text.split("\r\n") if ln.strip()]
        out: dict[str, str] = {}
        for ln in lines[1:]:
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            out[k.strip().lower()] = v.strip()
        return out

    @staticmethod
    def _get_local_ip_for_peer(peer_ip: str) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((peer_ip, 80))
            local_ip = s.getsockname()[0]
            if local_ip and local_ip != "0.0.0.0":
                return local_ip
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                s.close()
        return "0.0.0.0"