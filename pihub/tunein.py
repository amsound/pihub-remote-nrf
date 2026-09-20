"""TuneIn station resolution and HLS playlist proxying.

Some TuneIn stations (notably Apple Music Hits, s345724) are HLS-only and are
served from Apple's CDN behind short-lived signed URLs. Two properties of those
streams break naive playback:

  1. The master playlist advertises several variants that mix codec profiles
     (mp4a.40.2 AAC-LC at 256k and mp4a.40.5 HE-AAC at 64k) across two regions.
     Any adaptive player that switches between them mid-stream produces a PCR
     discontinuity and an audible dropout/rebuffer.
  2. Every URL in the chain is signed with an expiring accessKey, so a URL
     handed to a Cast device stops working partway through a long session.

This module resolves a station to a single pinned variant and serves that
variant's media playlist from a stable local URL, transparently re-resolving
upstream when a signature expires.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import socket
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger(__name__)

HLS_CONTENT_TYPE = "application/vnd.apple.mpegurl"

# TuneIn only hands back an HLS rendition when "hls" is requested. Asking for
# mp3/aac alone returns a spoken "not compatible" clip for these stations.
TUNE_URL = (
    "https://opml.radiotime.com/Tune.ashx"
    "?id={station_id}"
    "&partnerId=RadioTime"
    "&version=5.38"
    "&listenId=1"
    "&formats=mp3,aac,ogg,hls"
    "&type=station"
    "&render=json"
)

# TuneIn's placeholder for "your client cannot play this station".
_NOT_COMPATIBLE = "notcompatible"

_STATION_ID_RE = re.compile(r"\b([sgpt]\d+)\b", re.IGNORECASE)
_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF:(.*)$", re.IGNORECASE)

# Preferred audio codec, most preferred first. AAC-LC gives the best quality and
# is what we want pinned; HE-AAC is only a fallback if AAC-LC is absent.
CODEC_PREFERENCE = ("mp4a.40.2", "mp4a.40.5")

RESOLVE_TIMEOUT_S = 8.0
PLAYLIST_TIMEOUT_S = 8.0
SEGMENT_TIMEOUT_S = 15.0

# Re-resolve the master proactively well before the signatures observed in the
# wild expire, rather than waiting for the first upstream failure.
VARIANT_MAX_AGE_S = 15 * 60.0



SEGMENT_CONTENT_TYPE = "audio/mp4"

# Apple's CMAF segments carry timed-metadata (emsg) boxes that crash some Cast
# receivers - notably Samsung soundbars, which quit ~8s into playback. Dropping
# them leaves the media bytes untouched.
DROP_BOXES = ("emsg",)

# Bounded so a long listening session cannot grow the map without limit. A live
# playlist rotates roughly one segment per target duration, so this holds many
# hours for several stations at once.
SEGMENT_REGISTRY_MAX = 4096


def strip_boxes(data: bytes, drop: tuple[str, ...] = DROP_BOXES) -> tuple[bytes, int]:
    """Remove top-level MP4 boxes of the given types.

    Returns (data, removed_count). Anything that does not parse cleanly as a
    box sequence is returned untouched, so non-fMP4 payloads pass through.
    """
    if len(data) < 8:
        return data, 0

    out = bytearray()
    off = 0
    removed = 0

    while off + 8 <= len(data):
        size = struct.unpack(">I", data[off:off + 4])[0]
        typ = data[off + 4:off + 8].decode("latin1", "replace")
        header = 8

        if size == 1:
            # 64-bit extended size follows the type.
            if off + 16 > len(data):
                return data, 0
            size = struct.unpack(">Q", data[off + 8:off + 16])[0]
            header = 16
        elif size == 0:
            # Box runs to end of file.
            size = len(data) - off

        if size < header or off + size > len(data):
            return data, 0

        if typ in drop:
            removed += 1
        else:
            out += data[off:off + size]

        off += size

    if off != len(data):
        return data, 0

    return bytes(out), removed


class SegmentRegistry:
    """Maps short local tokens to upstream segment URLs.

    Segment URLs are signed and hundreds of characters long, so the playlist we
    serve points at short local paths instead. Tokens are derived from the URL,
    so the same segment keeps its token across playlist refreshes.
    """

    def __init__(self, max_entries: int = SEGMENT_REGISTRY_MAX) -> None:
        self._max = max_entries
        self._urls: OrderedDict[str, str] = OrderedDict()

    def token_for(self, url: str) -> str:
        token = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        if token in self._urls:
            self._urls.move_to_end(token)
        else:
            self._urls[token] = url
            while len(self._urls) > self._max:
                self._urls.popitem(last=False)
        return token

    def url_for(self, token: str) -> str | None:
        url = self._urls.get(token)
        if url is not None:
            self._urls.move_to_end(token)
        return url


def rewrite_playlist(body: str, base_url: str, to_path: Callable[[str], str]) -> str:
    """Point a media playlist's segment and init URLs at local paths.

    Relative URIs are resolved against base_url first, so the rewritten
    playlist no longer depends on its original location.
    """
    out: list[str] = []

    for line in body.splitlines():
        stripped = line.strip()

        if stripped.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]*)"', stripped)
            if match:
                target = urljoin(base_url, match.group(1))
                line = stripped.replace(match.group(0), f'URI="{to_path(target)}"')
        elif stripped and not stripped.startswith("#"):
            line = to_path(urljoin(base_url, stripped))

        out.append(line)

    return "\n".join(out) + "\n"


class TuneInError(RuntimeError):
    """Raised when a station cannot be resolved or its playlist fetched."""


def parse_station_id(value: str) -> str:
    """Extract a bare TuneIn station ID from a station ID or a tunein.com URL.

    Accepts "s345724" or "https://tunein.com/radio/Apple-Music-Hits-s345724/".
    """
    raw = (value or "").strip()
    if not raw:
        raise TuneInError("tunein_station_id_empty")

    # A bare ID is the common case; only fall back to scanning when it is a URL.
    if re.fullmatch(r"[sgpt]\d+", raw, re.IGNORECASE):
        return raw.lower()

    matches = _STATION_ID_RE.findall(raw)
    if not matches:
        raise TuneInError(f"tunein_station_id_unparseable:{raw}")

    # In a slug like "Apple-Music-Hits-s345724" the ID is the trailing match.
    return matches[-1].lower()


def local_ip_for(host: str) -> str | None:
    """Best-effort source IP this machine would use to reach `host`.

    Used to build a proxy URL the Cast device can actually fetch.
    """
    for target in (host, "8.8.8.8"):
        if not target:
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((target, 80))
                ip = str(s.getsockname()[0])
            finally:
                s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            continue
    return None


@dataclass
class _Variant:
    url: str
    codec: str
    bandwidth: int


@dataclass
class _CachedStation:
    variant_url: str
    codec: str
    resolved_at: float


def _parse_master(body: str, master_url: str) -> list[_Variant]:
    """Parse #EXT-X-STREAM-INF entries out of an HLS master playlist."""
    variants: list[_Variant] = []
    lines = [ln.strip() for ln in body.splitlines()]

    for idx, line in enumerate(lines):
        m = _STREAM_INF_RE.match(line)
        if not m:
            continue

        # The URI is the next non-blank, non-comment line.
        uri = ""
        for candidate in lines[idx + 1:]:
            if not candidate or candidate.startswith("#"):
                continue
            uri = candidate
            break
        if not uri:
            continue

        attrs = m.group(1)
        codec_m = re.search(r'CODECS="([^"]*)"', attrs, re.IGNORECASE)
        bw_m = re.search(r"\bBANDWIDTH=(\d+)", attrs, re.IGNORECASE)

        variants.append(
            _Variant(
                url=urljoin(master_url, uri),
                codec=(codec_m.group(1) if codec_m else "").strip().lower(),
                bandwidth=int(bw_m.group(1)) if bw_m else 0,
            )
        )

    return variants


def _variant_key(url: str) -> str:
    """Stable identity for a variant, ignoring its expiring signature.

    Apple publishes the same bitrate from more than one region (e.g. .../usw/256
    and .../use/256), so the path is what distinguishes them.
    """
    return url.split("?", 1)[0]


def _pick_variant(variants: list[_Variant], previous_key: str | None = None) -> _Variant:
    """Pick one variant to pin, preferring AAC-LC and then highest bitrate.

    When re-resolving, stick to the variant already being played if it is still
    offered. Two variants can tie on codec and bandwidth (the same bitrate from
    different regions), and silently moving between them mid-stream would hand
    the player a different timeline.
    """
    if not variants:
        raise TuneInError("tunein_master_no_variants")

    if previous_key:
        for v in variants:
            if _variant_key(v.url) == previous_key:
                return v

    def rank(item: tuple[int, _Variant]) -> tuple[int, int, int]:
        # Ties break on the master's own ordering, which is Apple's preference.
        order, v = item
        for i, codec in enumerate(CODEC_PREFERENCE):
            if codec in v.codec:
                return (i, -v.bandwidth, order)
        return (len(CODEC_PREFERENCE), -v.bandwidth, order)

    return sorted(enumerate(variants), key=rank)[0][1]


class TuneInResolver:
    """Resolves TuneIn stations and proxies their pinned HLS media playlist."""

    def __init__(self, *, session_factory=None) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._session_factory = session_factory
        self._cache: dict[str, _CachedStation] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                self._session = aiohttp.ClientSession()
        return self._session

    def _lock_for(self, station_id: str) -> asyncio.Lock:
        lock = self._locks.get(station_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[station_id] = lock
        return lock

    async def resolve_stream_url(self, station_id: str) -> tuple[str, str]:
        """Resolve a station to (stream_url, media_type) via the TuneIn OPML API.

        Each call returns a freshly signed URL.
        """
        station_id = parse_station_id(station_id)
        url = TUNE_URL.format(station_id=station_id)
        session = await self._get_session()

        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=RESOLVE_TIMEOUT_S)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as exc:
            raise TuneInError(f"tunein_resolve_failed station_id={station_id}: {exc}") from exc

        body = data.get("body") or []
        if not body:
            raise TuneInError(f"tunein_resolve_empty station_id={station_id}")

        # Prefer a real stream over TuneIn's "not compatible" placeholder, and
        # prefer HLS since that is what these stations actually publish.
        entries = [e for e in body if isinstance(e, dict) and (e.get("url") or "").strip()]
        entries = [e for e in entries if _NOT_COMPATIBLE not in e["url"].lower()]
        if not entries:
            raise TuneInError(
                f"tunein_resolve_no_playable_url station_id={station_id} "
                "(TuneIn returned only its 'not compatible' placeholder)"
            )

        entries.sort(key=lambda e: 0 if str(e.get("media_type", "")).lower() == "hls" else 1)
        chosen = entries[0]
        return chosen["url"].strip(), str(chosen.get("media_type", "")).lower()

    async def _resolve_variant(self, station_id: str) -> _CachedStation:
        """Resolve the station and pin a single HLS variant playlist URL."""
        previous = self._cache.get(station_id)
        previous_key = _variant_key(previous.variant_url) if previous else None

        stream_url, media_type = await self.resolve_stream_url(station_id)

        if media_type != "hls" and not stream_url.split("?")[0].lower().endswith(".m3u8"):
            # Not an HLS station: there is no master to pin, so serve it as-is.
            # Callers that need a direct URL should use resolve_stream_url().
            raise TuneInError(
                f"tunein_not_hls station_id={station_id} media_type={media_type}"
            )

        session = await self._get_session()
        try:
            async with session.get(
                stream_url, timeout=aiohttp.ClientTimeout(total=PLAYLIST_TIMEOUT_S)
            ) as resp:
                resp.raise_for_status()
                body = await resp.text()
        except Exception as exc:
            raise TuneInError(f"tunein_master_fetch_failed station_id={station_id}: {exc}") from exc

        if "#EXT-X-STREAM-INF" not in body.upper():
            # Already a media playlist rather than a master; nothing to pin.
            return _CachedStation(variant_url=stream_url, codec="", resolved_at=time.time())

        variant = _pick_variant(_parse_master(body, stream_url), previous_key)
        variant_key = _variant_key(variant.url)
        logger.info(
            "tunein pinned station_id=%s codec=%s bandwidth=%d variant=%s%s",
            station_id,
            variant.codec or "unknown",
            variant.bandwidth,
            "/".join(variant_key.rsplit("/", 2)[-2:]),
            " (kept)" if previous_key == variant_key else "",
        )
        return _CachedStation(
            variant_url=variant.url,
            codec=variant.codec,
            resolved_at=time.time(),
        )

    async def is_hls(self, station_id: str) -> tuple[bool, str]:
        """Return (is_hls, stream_url) for a station.

        Stations that publish a plain MP3/AAC stream have no playlist to pin or
        rewrite, so callers should hand the URL straight to the player.
        """
        stream_url, media_type = await self.resolve_stream_url(station_id)
        hls = media_type == "hls" or stream_url.split("?", 1)[0].lower().endswith(".m3u8")
        return hls, stream_url

    async def media_playlist(
        self,
        station_id: str,
        to_path: Callable[[str], str] | None = None,
    ) -> str:
        """Return the pinned variant's media playlist body for `station_id`.

        Re-resolves upstream when the cached entry is stale or its signature has
        expired, so the local URL stays valid indefinitely. When `to_path` is
        given, segment and init URLs are rewritten through it, which lets the
        caller serve the segments itself.
        """
        station_id = parse_station_id(station_id)

        async with self._lock_for(station_id):
            cached = self._cache.get(station_id)
            if cached is not None and (time.time() - cached.resolved_at) > VARIANT_MAX_AGE_S:
                cached = None

            if cached is None:
                cached = await self._resolve_variant(station_id)
                self._cache[station_id] = cached

            session = await self._get_session()
            try:
                async with session.get(
                    cached.variant_url,
                    timeout=aiohttp.ClientTimeout(total=PLAYLIST_TIMEOUT_S),
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.text()
                return self._maybe_rewrite(body, cached.variant_url, to_path)
            except Exception as exc:
                logger.info(
                    "tunein playlist fetch failed station_id=%s, re-resolving: %s",
                    station_id,
                    exc,
                )

            # Signature likely expired: resolve again and retry exactly once.
            cached = await self._resolve_variant(station_id)
            self._cache[station_id] = cached

            try:
                async with session.get(
                    cached.variant_url,
                    timeout=aiohttp.ClientTimeout(total=PLAYLIST_TIMEOUT_S),
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.text()
                return self._maybe_rewrite(body, cached.variant_url, to_path)
            except Exception as exc:
                self._cache.pop(station_id, None)
                raise TuneInError(
                    f"tunein_playlist_fetch_failed station_id={station_id}: {exc}"
                ) from exc

    @staticmethod
    def _maybe_rewrite(
        body: str,
        base_url: str,
        to_path: Callable[[str], str] | None,
    ) -> str:
        if to_path is None:
            return body
        return rewrite_playlist(body, base_url, to_path)

    async def fetch_segment(self, url: str) -> tuple[bytes, int]:
        """Fetch one media segment, dropping boxes that break Cast receivers.

        Returns (body, boxes_removed).
        """
        session = await self._get_session()
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=SEGMENT_TIMEOUT_S)
            ) as resp:
                resp.raise_for_status()
                raw = await resp.read()
        except Exception as exc:
            raise TuneInError(f"tunein_segment_fetch_failed: {exc}") from exc

        return strip_boxes(raw)
