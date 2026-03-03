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
import html
from xml.etree import ElementTree as ET
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

logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

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

    # Raw NOTIFY freshness (any incoming NOTIFY, even if it doesn't change state)
    last_notify_ts: float | None = None

    # Meaningful state-change freshness (only updates when state changes)
    last_event_ts: float | None = None

    last_poll_ts: float | None = None
    last_update_ts: float | None = None
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

        # LinkPlay control endpoints are fragile with keepalive.
        # Force close per-request (in addition to the connector setting).
        try:
            if "Connection" not in headers and "connection" not in headers:
                headers = dict(headers)
                headers["Connection"] = "close"
        except Exception:
            pass

        # Accept yarl.URL, bytes, etc. Normalize early.
        if method is None or url is None:
            raise TypeError(f"Unsupported request object: {request!r}")

        method = str(method)
        url = str(url)

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

        # LinkPlay endpoints are fragile with keepalive
        try:
            hdict = dict(headers)
        except Exception:
            hdict = {}
        hdict.setdefault("Connection", "close")

        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                async with self._session.request(
                    method=method,
                    url=url,
                    headers=hdict,
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

            except (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError) as err:
                last_err = err
                if attempt == 1:
                    await asyncio.sleep(0.1)
                    continue
                raise

        # Defensive (should not happen)
        raise last_err or RuntimeError("HTTP request failed")

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
        # Prefer explicit "Host" header and remove "HOST" if present to avoid duplicates
        hdict["Host"] = f"{host}:{port}"
        hdict.pop("HOST", None)
        hdict.setdefault("User-Agent", "HomeAssistant/async_upnp_client")
        hdict["TIMEOUT"] = "Second-1800"
        hdict.setdefault("Connection", "close")

        # Ensure CALLBACK is in <...> form (GENA requires angle-bracket URL list)
        cb = hdict.get("CALLBACK")
        if isinstance(cb, str) and cb:
            cb_s = cb.strip()
            if not cb_s.startswith("<"):
                hdict["CALLBACK"] = f"<{cb_s}>"

        # Ensure initial SUBSCRIBE has NT: upnp:event
        # (renewals use SID instead; async_upnp_client usually provides the right set, but be defensive)
        if method.upper() == "SUBSCRIBE" and "sid" not in {k.lower() for k in hdict.keys()}:
            hdict.setdefault("NT", "upnp:event")

        # Debug
        logger.debug("%s(raw) %s headers=%s", method, url, hdict)

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
                key = k.strip()
                val = v.strip()

                # Keep original casing AND lowercase alias.
                resp_headers[key] = val
                resp_headers[key.lower()] = val

            logger.debug(
                "SUBSCRIBE resp: status=%s sid=%s timeout=%s",
                status_code,
                resp_headers.get("SID") or resp_headers.get("sid"),
                resp_headers.get("TIMEOUT") or resp_headers.get("timeout"),
            )

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
        self._http_scheme = (http_scheme or "https").strip().lower()
        if self._http_scheme not in {"http", "https"}:
            self._http_scheme = "https"
        # Optional polling (off by default). Enable with SPEAKER_POLL_AVAILABILITY=1/true/yes/on
        self._poll_enabled = (os.getenv("SPEAKER_POLL_AVAILABILITY", "") or "").strip().lower() in {
            "1", "true", "yes", "on"
        }

        # Poll interval: default 5s when polling is enabled, override with SPEAKER_POLL_INTERVAL
        poll_interval_raw = (os.getenv("SPEAKER_POLL_INTERVAL", "") or "").strip()
        if poll_interval_raw:
            try:
                self._poll_interval_s = float(poll_interval_raw)
            except Exception:
                self._poll_interval_s = 5.0
        else:
            self._poll_interval_s = 5.0

        # Clamp to something sane
        self._poll_interval_s = max(1.0, self._poll_interval_s)

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

        self._logged_first_subscribe = False

        logger.debug(
            "speaker polling: enabled=%s interval_s=%s (env SPEAKER_POLL_AVAILABILITY=%r SPEAKER_POLL_INTERVAL=%r)",
            self._poll_enabled,
            self._poll_interval_s,
            os.getenv("SPEAKER_POLL_AVAILABILITY"),
            os.getenv("SPEAKER_POLL_INTERVAL"),
        )

    @property
    def enabled(self) -> bool:
        return bool(self._host)

    @property
    def state(self) -> SpeakerState:
        return self._state
    

    def _force_upnp_wiring(self) -> None:
        """Ensure notify server routes NOTIFYs to the same handler used for SUBSCRIBE.

        Some async_upnp_client versions can end up with the HTTP handler bound to a
        different UpnpEventHandler instance than the one used to subscribe, which
        results in NOTIFYs being accepted (200) but never dispatched.
        """
        if self._notify_server is None or self._event_handler is None:
            return

        # Ensure the notify server routes NOTIFYs into *this* event handler instance.
        for attr in ("event_handler", "_event_handler"):
            if hasattr(self._notify_server, attr):
                with contextlib.suppress(Exception):
                    setattr(self._notify_server, attr, self._event_handler)

        # Belt + suspenders: ensure handler points back at this server.
        for attr in ("notify_server", "_notify_server"):
            if hasattr(self._event_handler, attr):
                with contextlib.suppress(Exception):
                    setattr(self._event_handler, attr, self._notify_server)

    @staticmethod
    def _localname(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    @staticmethod
    def _norm_lastchange(v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        if s.upper() in {"NONE", "UNKNOWN"}:
            return None
        return s

    @staticmethod
    def _coerce_volume_to_0_1(v: Any) -> float | None:
        try:
            f = float(v)
        except Exception:
            return None
        # Many LinkPlay devices report 0..100.
        if f > 1.0:
            if f <= 100.0:
                f = f / 100.0
            else:
                f = 1.0
        return max(0.0, min(1.0, f))

    def snapshot(self) -> dict[str, Any]:
        s = self._state

        def _pct(v: float | None) -> int | None:
            if v is None:
                return None
            return int(round(max(0.0, min(1.0, v)) * 100.0))

        def _iso(ts: float | None) -> str | None:
            if not ts:
                return None
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))

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

            # Push (NOTIFY) freshness
            # - last_notify_*: any NOTIFY received
            # - last_event_*: last NOTIFY which changed our exposed state
            "last_notify_ts": s.last_notify_ts,
            "notify_age_s": int(time.time() - float(s.last_notify_ts)) if s.last_notify_ts else None,
            "last_notify_iso": _iso(s.last_notify_ts),

            "last_event_ts": s.last_event_ts,
            "event_age_s": int(time.time() - float(s.last_event_ts)) if s.last_event_ts else None,
            "last_event_iso": _iso(s.last_event_ts),

            # Pull (poll/prime) freshness
            "last_poll_ts": s.last_poll_ts,
            "poll_age_s": int(time.time() - float(s.last_poll_ts)) if s.last_poll_ts else None,
            "last_poll_iso": _iso(s.last_poll_ts),

            # Overall "last time we changed our state" (either push or pull)
            "last_update_ts": s.last_update_ts,
            "update_age_s": int(time.time() - float(s.last_update_ts)) if s.last_update_ts else None,
            "last_update_iso": _iso(s.last_update_ts),
        }

    async def start(self) -> None:
        if not self.enabled:
            return
        if self._task is not None:
            return

        self._stop.clear()
        connector = aiohttp.TCPConnector(
            force_close=True,            # critical for LinkPlay SOAP endpoints
            enable_cleanup_closed=True,  # helps avoid noisy warnings on linux
        )
        self._session = aiohttp.ClientSession(connector=connector)
        logger.info(
            "mode=%s poll_interval_s=%.1f host=%s",
            ("poll" if self._poll_enabled else "event"),
            self._poll_interval_s,
            self._host,
        )
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
            current = self._state.volume
            if current is None:
                current = getattr(dev, "volume_level", None)
    
            if current is None:
                try:
                    await dev.async_update()
                except Exception:
                    pass
                current = getattr(dev, "volume_level", None) or self._state.volume
                if current is None:
                    await self._try_update_volume_mute_via_rendering_control(dev)
                    current = self._state.volume
    
            if current is None:
                return
    
            target = min(1.0, max(0.0, float(current) + self._step))
            await dev.async_set_volume_level(target)
            self._state.volume = target
            try:
                self._state.last_update_ts = time.time()
            except Exception:
                pass
    
            await self._try_update_volume_mute_via_rendering_control(dev)
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"volume_up: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def volume_down(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            current = self._state.volume
            if current is None:
                current = getattr(dev, "volume_level", None)
    
            if current is None:
                try:
                    await dev.async_update()
                except Exception:
                    pass
                current = getattr(dev, "volume_level", None) or self._state.volume
                if current is None:
                    await self._try_update_volume_mute_via_rendering_control(dev)
                    current = self._state.volume
    
            if current is None:
                return
    
            target = min(1.0, max(0.0, float(current) - self._step))
            await dev.async_set_volume_level(target)
            self._state.volume = target
            try:
                self._state.last_update_ts = time.time()
            except Exception:
                pass
    
            await self._try_update_volume_mute_via_rendering_control(dev)
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"volume_down: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def mute_toggle(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            muted = self._state.muted
            if muted is None:
                muted = getattr(dev, "is_volume_muted", None)
    
            if muted is None:
                try:
                    await dev.async_update()
                except Exception:
                    pass
                muted = getattr(dev, "is_volume_muted", None) or self._state.muted
    
            if muted is None:
                await self._try_update_volume_mute_via_rendering_control(dev)
                muted = self._state.muted
    
            if muted is None:
                return
    
            await dev.async_mute_volume(not bool(muted))
            self._state.muted = (not bool(muted))
            try:
                self._state.last_update_ts = time.time()
            except Exception:
                pass
    
            await self._try_update_volume_mute_via_rendering_control(dev)
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"mute_toggle: {{err!r}}"
            await self._mark_unreachable_maybe(err)

    async def play(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            await dev.async_play()
            self._state.transport = "playing"
            try:
                self._state.last_update_ts = time.time()
            except Exception:
                pass
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"play: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def pause(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            await dev.async_pause()
            self._state.transport = "paused"
            try:
                self._state.last_update_ts = time.time()
            except Exception:
                pass
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"pause: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def stop_playback(self) -> None:
        """Stop playback via UPnP AVTransport Stop (SOAP only)."""
        dev = self._device
        if dev is None:
            return
        try:
            await self._async_avtransport_stop()
            # Verify/refresh state
            await asyncio.sleep(0.15)
            st3 = await self._async_get_transport_state()
            if st3:
                if st3 in {"playing", "transitioning"}:
                    self._state.transport = "playing"
                elif "pause" in st3:
                    self._state.transport = "paused"
                elif st3 == "stopped":
                    self._state.transport = "stopped"
                else:
                    self._state.transport = st3
            else:
                try:
                    await dev.async_update()
                except TypeError:
                    await dev.async_update()
                self._refresh_from_device(dev)

            self._state.last_error = None
            self._state.last_update_ts = time.time()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"stop: {err!r}"
            await self._mark_unreachable_maybe(err)
    
        
    async def next_track(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            await self._async_avtransport_next_prev("Next")
            try:
                self._state.last_update_ts = time.time()
            except Exception:
                pass
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"next: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def previous_track(self) -> None:
        dev = self._device
        if dev is None:
            return
        try:
            await self._async_avtransport_next_prev("Previous")
            try:
                self._state.last_update_ts = time.time()
            except Exception:
                pass
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"previous: {err!r}"
            await self._mark_unreachable_maybe(err)

    async def play_pause(self) -> None:
        """Toggle play/pause using UPnP AVTransport (SOAP only).

        We do not rely on cached profile state; we issue the action, then verify
        via GetTransportInfo and/or a quick profile refresh.
        """
        dev = self._device
        if dev is None:
            return
        try:
            # Decide intent from best-known state
            st = (self._state.transport or "").lower()
            if not st:
                st2 = await self._async_get_transport_state()
                if st2:
                    st = st2

            is_playing = st in {"playing", "transitioning"}

            if is_playing:
                await self._async_avtransport_pause()
            else:
                await self._async_avtransport_play()

            # Verify: ask AVTransport for current state
            await asyncio.sleep(0.15)
            st3 = await self._async_get_transport_state()
            if st3:
                if st3 in {"playing", "transitioning"}:
                    self._state.transport = "playing"
                elif "pause" in st3:
                    self._state.transport = "paused"
                elif st3 == "stopped":
                    self._state.transport = "stopped"
                else:
                    self._state.transport = st3
            else:
                # Fallback: refresh profile fields
                try:
                    await dev.async_update()
                except TypeError:
                    await dev.async_update()
                self._refresh_from_device(dev)

            self._state.last_error = None
            self._state.last_update_ts = time.time()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"play_pause: {err!r}"
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

    async def preset(self, n: int) -> None:
        if self._session is None or not self._state.reachable:
            return
        try:
            n_int = int(n)
        except Exception:
            return
        if not (1 <= n_int <= 10):
            return

        # LinkPlay: MCUKeyShortClick:<n>
        endpoint = f"{self._http_scheme}://{self._host}{_HTTPAPI_PATH}?command=MCUKeyShortClick:{n_int}"
        try:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
            async with self._session.get(endpoint, timeout=timeout, ssl=False) as resp:
                await resp.read()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"preset: {err!r}"
            self._state.reachable = False


    async def set_source(self, source: str) -> None:
        if self._session is None or not self._state.reachable:
            return
        if not isinstance(source, str) or not source.strip():
            return
        src = source.strip()

        # LinkPlay: setPlayerCmd:switchmode:<mode>
        cmd = f"setPlayerCmd:switchmode:{src}"
        cmd_q = quote(cmd, safe=":/?&=%")
        endpoint = f"{self._http_scheme}://{self._host}{_HTTPAPI_PATH}?command={cmd_q}"
        try:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
            async with self._session.get(endpoint, timeout=timeout, ssl=False) as resp:
                await resp.read()
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"set_source: {err!r}"
            self._state.reachable = False

    async def turn_off(self) -> None:
        """
        Power off / standby via LinkPlay HTTP API.

        LinkPlay expects:
        /httpapi.asp?command=setShutdown:0
        """
        if self._session is None:
            raise RuntimeError("speaker aiohttp session not initialized")

        url = f"http://{self._host}{_HTTPAPI_PATH}"
        params = {"command": "setShutdown:0"}

        try:
            async with self._session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S),
                ssl=False,
            ) as resp:
                # Consume body so the connection is cleanly released.
                _ = await resp.text(errors="ignore")

                if resp.status >= 400:
                    self._state.last_error = f"turn_off httpapi: HTTP {resp.status}"
                    raise RuntimeError(f"turn_off failed: HTTP {resp.status}")

            # Success: record that we issued the command, but don't assume state changed.
            self._state.last_error = None
            self._state.last_update_ts = time.time()

        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"turn_off httpapi: {err!r}"
            raise

    async def clear_playlist(self) -> None:
        """
        Clear the active queue/playlist via vendor PlayQueue UPnP service.

        service_type = urn:schemas-wiimu-com:service:PlayQueue:1
        action       = DeleteQueue
        args         = QueueName="CurrentQueue"
        """
        service_type = "urn:schemas-wiimu-com:service:PlayQueue:1"

        # If the device doesn't expose the vendor service, just ignore.
        res = await self._async_call_action(service_type, "DeleteQueue", QueueName="CurrentQueue")
        if res is None:
            return

        # Refresh state after clearing, same idea as HA.
        d = self._device
        if d is not None:
            try:
                await d.async_update()
                self._refresh_from_device(d)
                self._state.last_error = None
                self._state.last_update_ts = time.time()
            except Exception:
                # Not fatal; queue clear already sent.
                pass

    # -------------------- Internal runner --------------------

    async def _runner(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_subscribe()
                backoff = 1.0

                connected_at = time.time()
                next_poll = connected_at + self._poll_interval_s

                stale_after_s = 15 * 60.0
                grace_after_connect_s = 15.0

                while not self._stop.is_set() and self._device is not None:
                    await asyncio.sleep(0.25)
                    now = time.time()

                    # Optional HA-style poll: keep state fresh even if NOTIFYs don't arrive
                    if self._poll_enabled and now >= next_poll:
                        next_poll = now + self._poll_interval_s
                        d = self._device
                        if d is not None:
                            try:
                                # HA uses async_update(do_ping=...) when poll_availability is enabled  [oai_citation:2‡media_player.py](sediment://file_000000000c9c7243a430a5d2f735f7d6)
                                try:
                                    await d.async_update(do_ping=True)  # type: ignore[arg-type]
                                except TypeError:
                                    await d.async_update()
                                self._refresh_from_device(d)
                                await self._try_update_volume_mute_via_rendering_control(d)
                                self._state.last_error = None
                                now_ts = time.time()
                                self._state.last_poll_ts = now_ts
                                self._state.last_update_ts = now_ts
                            except Exception as err:  # noqa: BLE001
                                self._state.last_error = f"poll_update: {err!r}"
                                # If polling fails consistently, reconnect on next outer loop
                                await self._disconnect_upnp()
                                self._state.reachable = False
                                self._state.subscribed = False
                                break

                    # If polling is enabled, don’t force reconnect just because events are quiet.
                    if self._poll_enabled:
                        continue

                    # Event-driven watchdog (only when polling is disabled)
                    last_evt = self._state.last_notify_ts or self._state.last_event_ts
                    if last_evt is None:
                        if now - connected_at < grace_after_connect_s:
                            continue
                        self._state.last_error = "watchdog: no events received after subscribe"
                        await self._disconnect_upnp()
                        self._state.reachable = False
                        self._state.subscribed = False
                        break

                    if now - float(last_evt) > stale_after_s:
                        self._state.last_error = f"watchdog: events stale ({now - float(last_evt):.1f}s)"
                        await self._disconnect_upnp()
                        self._state.reachable = False
                        self._state.subscribed = False
                        break

            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                logger.exception("runner exception")
                self._state.last_error = f"runner: {err!r}"
                await self._disconnect_upnp()
                self._state.reachable = False
                self._state.subscribed = False
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.6)

    async def _connect_and_subscribe(self) -> None:
        await self._disconnect_upnp()
        # 1) Location: best-effort SSDP, else fall back to common LinkPlay/WiiM description URL
        location = await self._ssdp_find_location_for_host(self._host)

        if not location:
            # Common LinkPlay/WiiM default device description URL
            location = f"http://{self._host}:49152/description.xml"

        if not location:
            self._state.reachable = False
            self._state.subscribed = False
            self._state.last_error = "ssdp: no LOCATION response"
            return
        # 2) Determine callback/bind IP (best-effort from routing table)
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

        # IMPORTANT: async_upnp_client versions differ: some expose async_start(), others async_start_server()
        started = False
        for start_name in ("async_start", "async_start_server"):
            start_fn = getattr(self._notify_server, start_name, None)
            if callable(start_fn):
                await start_fn()
                started = True
                break
        if not started:
            raise RuntimeError("Notify server has no async_start/async_start_server method")

        callback_url = getattr(self._notify_server, "callback_url", None)
        logger.debug("notify callback_url=%s (pinned port=%s)", callback_url, notify_port)

        # Event handler: your version requires requester
        try:
            self._event_handler = UpnpEventHandler(self._notify_server, requester)
        except TypeError:
            self._event_handler = UpnpEventHandler(self._notify_server, requester=requester)

        logger.debug("UPnP handler wiring: notify_server=%s event_handler=%s",
                    type(self._notify_server).__name__, type(self._event_handler).__name__)
        self._force_upnp_wiring()

        # IMPORTANT: start/attach handler (method name differs by version)
        for start_name in ("async_start", "async_start_handler"):
            start_fn = getattr(self._event_handler, start_name, None)
            if callable(start_fn):
                await start_fn()
                break

        # Create factory with handler (best-effort across signatures)
        factory = None
        for ctor in (
            lambda: UpnpFactory(requester, self._event_handler),
            lambda: UpnpFactory(requester, event_handler=self._event_handler),
        ):
            try:
                factory = ctor()
                break
            except TypeError:
                continue
        if factory is None:
            # Fallback (shouldn't be needed, but avoid hard crash)
            factory = UpnpFactory(requester)

        upnp_device = await factory.async_create_device(location)

        # DmrDevice ctor differs by version
        dmr = None
        for ctor in (
            lambda: DmrDevice(upnp_device, self._event_handler),
            lambda: DmrDevice(upnp_device, event_handler=self._event_handler),
            lambda: DmrDevice(upnp_device),
        ):
            try:
                dmr = ctor()
                break
            except TypeError:
                continue
        if dmr is None:
            raise RuntimeError("Unable to construct DmrDevice with current async_upnp_client signatures")

        dmr.on_event = self._on_event

        # Subscribe (auto-renew)
        await dmr.async_subscribe_services(auto_resubscribe=True)

        logger.debug("subscribe complete: device=%r handler id=%s", dmr, id(self._event_handler))

        self._device = dmr
        self._state.reachable = True
        self._state.subscribed = True
        self._state.last_error = None

        # Prime state once
        try:
            await dmr.async_update()
            self._refresh_from_device(dmr)
            await self._try_update_volume_mute_via_rendering_control(dmr)
            now_ts = time.time()
            self._state.last_poll_ts = now_ts
            self._state.last_update_ts = now_ts
            # NOTE: Do not touch last_event_ts here; that should only move on real NOTIFY.
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"prime_update: {err!r}"

        msg = "upnp subscribed callback=%s location=%s mode=%s"
        if not self._logged_first_subscribe:
            logger.info(msg, callback_url, location, ("poll" if self._poll_enabled else "event"))
            self._logged_first_subscribe = True
        else:
            logger.debug(msg, callback_url, location, ("poll" if self._poll_enabled else "event"))


    async def _disconnect_upnp(self) -> None:
        # Unsubscribe device first
        d, self._device = self._device, None
        if d is not None:
            with contextlib.suppress(Exception):
                d.on_event = None
            with contextlib.suppress(Exception):
                await d.async_unsubscribe_services()

        # Stop event handler next (method name differs by version)
        eh, self._event_handler = self._event_handler, None
        if eh is not None:
            for stop_name in ("async_stop", "async_stop_handler"):
                stop_fn = getattr(eh, stop_name, None)
                if callable(stop_fn):
                    with contextlib.suppress(Exception):
                        await stop_fn()
                    break

        # Stop notify server last (method name differs by version)
        ns, self._notify_server = self._notify_server, None
        if ns is not None:
            for stop_name in ("async_stop", "async_stop_server"):
                stop_fn = getattr(ns, stop_name, None)
                if callable(stop_fn):
                    with contextlib.suppress(Exception):
                        await stop_fn()
                    break

    async def _mark_unreachable_maybe(self, err: Exception) -> None:
        if isinstance(err, UpnpError):
            self._state.reachable = False
            self._state.subscribed = False
            await self._disconnect_upnp()

    # -------------------- UPnP event callback + state extraction --------------------

    def _on_event(self, service: Any, state_variables: Any) -> None:
        """UPnP event callback.

        We track two timestamps:
        - last_notify_ts: any NOTIFY received
        - last_event_ts: last NOTIFY which changed our exposed state
        """
        now_ts = time.time()
        self._state.last_notify_ts = now_ts

        logger.debug(
            "NOTIFY received: svc_type=%s svc_id=%s vars=%s",
            getattr(service, "service_type", None),
            getattr(service, "service_id", None),
            len(state_variables or []),
        )

        # If the profile isn't fully attached yet, don't try to parse; just mark that events flow.
        d = self._device
        if d is None:
            return

        # Snapshot before applying updates, so we can detect "meaningful change"
        before = (
            self._state.transport,
            self._state.volume,
            self._state.muted,
            self._state.source,
            self._state.track_uri,
            self._state.title,
            self._state.artist,
            self._state.album,
        )

        # Service identifiers (best-effort)
        svc_type = (getattr(service, "service_type", "") or "")
        svc_id = (getattr(service, "service_id", "") or "")

        # 1) First, try the cheap refresh from the profile object
        try:
            self._refresh_from_device(d)
        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"on_event refresh: {err!r}"

        # 2) Now parse state vars + LastChange for LinkPlay/WiiM quirks
        try:
            # Normalize async_upnp_client callback shapes:
            # - sometimes Sequence[UpnpStateVariable]
            # - sometimes dict[name -> UpnpStateVariable]  (keep keys!)
            # - sometimes iterable of (name, value)
            svs = state_variables or []

            if isinstance(svs, dict):
                sv_iter = svs.items()
            else:
                sv_iter = svs

            for sv in sv_iter:
                name = getattr(sv, "name", None)
                val = getattr(sv, "value", None)

                # dict items or tuple shapes: (name, value)
                if name is None and isinstance(sv, tuple) and len(sv) == 2:
                    name, val = sv

                # Direct surfaced fields (some firmwares)
                if name == "PlaybackStorageMedium":
                    self._playback_storage_medium = self._norm_lastchange(val)
                    continue
                if name == "AVTransportURI":
                    self._avtransport_uri = self._norm_lastchange(val)
                    continue
                if name == "CurrentTrackURI":
                    self._current_track_uri = self._norm_lastchange(val)
                    continue
                if name == "TransportState":
                    # Prefer explicit transport state if present
                    self._state.transport = str(val).lower() if val is not None else self._state.transport
                    continue
                if name == "Mute":
                    v = val
                    if isinstance(v, dict):
                        v = v.get("Master") or v.get("master") or next(iter(v.values()), None)
                    try:
                        self._state.muted = bool(int(v))
                    except Exception:
                        pass
                    continue

                if name == "Volume":
                    v = val
                    if isinstance(v, dict):
                        v = v.get("Master") or v.get("master") or next(iter(v.values()), None)
                    if (vv := self._coerce_volume_to_0_1(v)) is not None:
                        self._state.volume = vv
                    continue

                # LastChange payloads: this is the important bit
                if name == "LastChange" and val:
                    # async_upnp_client can give us str or bytes depending on platform/version.
                    raw = val.decode("utf-8", errors="ignore") if isinstance(val, bytes) else str(val)
                    if not raw.strip():
                        continue

                    # Don't over-unescape; one pass is usually right
                    unescaped = html.unescape(raw)

                    try:
                        root = ET.fromstring(unescaped)
                    except Exception:
                        continue

                    # AVTransport LastChange (source/URI/transport/meta)
                    if "AVTransport" in svc_type or "AVTransport" in svc_id:
                        for el in root.iter():
                            ln = self._localname(el.tag)
                            v2 = el.attrib.get("val") or el.text

                            if ln == "PlaybackStorageMedium":
                                self._playback_storage_medium = self._norm_lastchange(v2)
                            elif ln == "AVTransportURI":
                                self._avtransport_uri = self._norm_lastchange(v2)
                            elif ln == "CurrentTrackURI":
                                self._current_track_uri = self._norm_lastchange(v2)
                            elif ln == "TransportState":
                                if v2 is not None:
                                    t = str(v2).strip().lower()
                                    if t in ("playing", "transitioning"):
                                        self._state.transport = "playing"
                                    elif "pause" in t:
                                        self._state.transport = "paused"
                                    elif t == "stopped":
                                        self._state.transport = "stopped"
                                    else:
                                        self._state.transport = t

                    # RenderingControl LastChange (volume/mute often only here)
                    if "RenderingControl" in svc_type or "RenderingControl" in svc_id:
                        for el in root.iter():
                            ln = self._localname(el.tag)
                            if ln not in ("Volume", "Mute"):
                                continue

                            channel = (el.attrib.get("channel") or el.attrib.get("Channel") or "").lower()
                            if channel and channel != "master":
                                continue

                            v2 = el.attrib.get("val")
                            if v2 is None:
                                continue

                            if ln == "Mute":
                                try:
                                    self._state.muted = bool(int(v2))
                                except Exception:
                                    pass
                            elif ln == "Volume":
                                if (vv := self._coerce_volume_to_0_1(v2)) is not None:
                                    self._state.volume = vv

        except Exception as err:  # noqa: BLE001
            self._state.last_error = f"on_event parse: {err!r}"

        # Recompute inferred source based on the raw fields we maintain
        self._state.source = self._infer_source()
        self._apply_source_cleanup()

        # Detect "meaningful change" and update freshness timestamps accordingly.
        after = (
            self._state.transport,
            self._state.volume,
            self._state.muted,
            self._state.source,
            self._state.track_uri,
            self._state.title,
            self._state.artist,
            self._state.album,
        )

        def _eq(a: Any, b: Any) -> bool:
            if a is None and b is None:
                return True
            if isinstance(a, float) or isinstance(b, float):
                try:
                    return a is not None and b is not None and abs(float(a) - float(b)) < 0.001
                except Exception:
                    return False
            return a == b

        changed = any(not _eq(a, b) for a, b in zip(before, after))
        if changed:
            self._state.last_event_ts = now_ts
            self._state.last_update_ts = now_ts


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
        self._apply_source_cleanup()

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


    def _apply_source_cleanup(self) -> None:
        """Clear/normalize fields that should not linger on local inputs."""
        src = self._state.source
        if src in {"hdmi", "optical", "line-in"}:
            # Local inputs should not show stale network track metadata.
            self._state.title = None
            self._state.artist = None
            self._state.album = None

            # Normalize track_uri to a stable token for local inputs.
            if src == "line-in":
                self._state.track_uri = "LINE-IN"
            else:
                self._state.track_uri = src.upper()

    async def _try_update_volume_mute_via_rendering_control(self, d: DmrDevice) -> None:
        """Best-effort volume/mute refresh using RenderingControl actions.

        Some LinkPlay firmwares do not surface volume/mute reliably via profile fields
        on async_update(), even though the values are available via GetVolume/GetMute.
        """
        upnp_dev = (
            getattr(d, "device", None)
            or getattr(d, "_device", None)
            or getattr(d, "_upnp_device", None)
            or getattr(d, "upnp_device", None)
        )
        if upnp_dev is None:
            return

        svc = None
        # Try common lookup helpers first
        try:
            if hasattr(upnp_dev, "service"):
                svc = upnp_dev.service("urn:schemas-upnp-org:service:RenderingControl:1")
        except Exception:
            svc = None

        if svc is None:
            # Fall back: scan services
            services = getattr(upnp_dev, "services", None) or []
            for s in services:
                st = (getattr(s, "service_type", "") or "")
                if "RenderingControl" in st:
                    svc = s
                    break

        if svc is None:
            return

        def _get_action(service: Any, name: str) -> Any | None:
            try:
                if hasattr(service, "action"):
                    return service.action(name)
            except Exception:
                pass
            acts = getattr(service, "actions", None)
            if isinstance(acts, dict):
                return acts.get(name)
            return None

        async def _call_action(act: Any, **kwargs: Any) -> dict[str, Any] | None:
            if act is None:
                return None
            try:
                if hasattr(act, "async_call"):
                    return await act.async_call(**kwargs)
            except Exception:
                return None
            return None

        # GetVolume
        vol_act = _get_action(svc, "GetVolume")
        res = await _call_action(vol_act, InstanceID=0, Channel="Master")
        if isinstance(res, dict):
            cv = res.get("CurrentVolume") or res.get("current_volume") or res.get("Volume")
            if cv is not None:
                vv = self._coerce_volume_to_0_1(cv)
                if vv is not None:
                    self._state.volume = vv

        # GetMute
        mute_act = _get_action(svc, "GetMute")
        res = await _call_action(mute_act, InstanceID=0, Channel="Master")
        if isinstance(res, dict):
            cm = res.get("CurrentMute") or res.get("current_mute") or res.get("Mute")
            if cm is not None:
                try:
                    self._state.muted = bool(int(cm))
                except Exception:
                    pass



    async def _async_call_action(self, service_type: str, action_name: str, **kwargs: Any) -> dict[str, Any] | None:
        """Best-effort call an UPnP action on the current device."""
        dev = self._device
        if dev is None:
            return None
        upnp_dev = (
            getattr(dev, "device", None)
            or getattr(dev, "_device", None)
            or getattr(dev, "_upnp_device", None)
            or getattr(dev, "upnp_device", None)
        )
        if upnp_dev is None:
            return None

        svc = None
        try:
            if hasattr(upnp_dev, "service"):
                svc = upnp_dev.service(service_type)
        except Exception:
            svc = None

        if svc is None:
            services = getattr(upnp_dev, "services", None) or []
            for s in services:
                st = (getattr(s, "service_type", "") or "")
                if st == service_type or service_type in st:
                    svc = s
                    break
        if svc is None:
            return None

        act = None
        try:
            if hasattr(svc, "action"):
                act = svc.action(action_name)
        except Exception:
            act = None
        if act is None:
            acts = getattr(svc, "actions", None)
            if isinstance(acts, dict):
                act = acts.get(action_name)
        if act is None or not hasattr(act, "async_call"):
            return None

        return await act.async_call(**kwargs)

    
    async def _async_get_transport_state(self) -> str | None:
        """Query AVTransport GetTransportInfo.CurrentTransportState."""
        service_type = "urn:schemas-upnp-org:service:AVTransport:1"
        try:
            res = await self._async_call_action(service_type, "GetTransportInfo", InstanceID=0)
        except Exception:
            res = None
        if not isinstance(res, dict):
            return None
        v = res.get("CurrentTransportState") or res.get("TransportState")
        if v is None:
            return None
        return str(v).strip().lower() or None

    async def _async_avtransport_play(self) -> None:
        """Call AVTransport Play like WiiM app (best-effort)."""
        service_type = "urn:schemas-upnp-org:service:AVTransport:1"
        args_with_cs = {"InstanceID": 0, "Speed": "1", "ControlSource": "WiiMApp"}
        args_no_cs = {"InstanceID": 0, "Speed": "1"}
        try:
            await self._async_call_action(service_type, "Play", **args_with_cs)
            return
        except Exception as err:  # noqa: BLE001
            msg = str(err)
            if ("ControlSource" not in msg) and ("Unknown argument" not in msg) and ("unexpected" not in msg):
                raise
        await self._async_call_action(service_type, "Play", **args_no_cs)

    async def _async_avtransport_pause(self) -> None:
        """Call AVTransport Pause like WiiM app (best-effort)."""
        service_type = "urn:schemas-upnp-org:service:AVTransport:1"
        args_with_cs = {"InstanceID": 0, "ControlSource": "WiiMApp"}
        args_no_cs = {"InstanceID": 0}
        try:
            await self._async_call_action(service_type, "Pause", **args_with_cs)
            return
        except Exception as err:  # noqa: BLE001
            msg = str(err)
            if ("ControlSource" not in msg) and ("Unknown argument" not in msg) and ("unexpected" not in msg):
                raise
        await self._async_call_action(service_type, "Pause", **args_no_cs)

    async def _async_avtransport_stop(self) -> None:
        """Call AVTransport Stop like WiiM app (best-effort)."""
        service_type = "urn:schemas-upnp-org:service:AVTransport:1"
        args_with_cs = {"InstanceID": 0, "ControlSource": "WiiMApp"}
        args_no_cs = {"InstanceID": 0}
        try:
            await self._async_call_action(service_type, "Stop", **args_with_cs)
            return
        except Exception as err:  # noqa: BLE001
            msg = str(err)
            if ("ControlSource" not in msg) and ("Unknown argument" not in msg) and ("unexpected" not in msg):
                raise
        await self._async_call_action(service_type, "Stop", **args_no_cs)

    async def _async_avtransport_next_prev(self, action: str) -> None:
            """Call AVTransport Next/Previous matching the WiiM app.

            Tries with ControlSource=WiiMApp first, then retries without it if the
            device rejects the argument.
            """
            service_type = "urn:schemas-upnp-org:service:AVTransport:1"
            args_with_cs = {"InstanceID": 0, "ControlSource": "WiiMApp"}
            args_no_cs = {"InstanceID": 0}

            try:
                res = await self._async_call_action(service_type, action, **args_with_cs)
                if res is not None:
                    return
            except Exception as err:  # noqa: BLE001
                msg = str(err)
                if ("ControlSource" in msg) or ("Unknown argument" in msg) or ("unexpected" in msg):
                    await self._async_call_action(service_type, action, **args_no_cs)
                    return
                raise

            await self._async_call_action(service_type, action, **args_no_cs)

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

                headers = LinkPlaySpeaker._parse_ssdp_response(data)
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