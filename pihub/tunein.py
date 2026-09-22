"""TuneIn station recognition and resolution.

pihub no longer streams TuneIn itself: a TuneIn entry in a soundbar slot is
handed to the restreamer, which resolves it on every connect. This module
recognises TuneIn entries and keeps a resolver for anything that wants a
playable URL directly.
"""

from __future__ import annotations

import re

import aiohttp

# TuneIn only hands back an HLS rendition when "hls" is requested. Asking for
# mp3/aac alone returns a spoken "not compatible" clip for HLS-only stations.
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

RESOLVE_TIMEOUT_S = 8.0


class TuneInError(RuntimeError):
    """Raised when a station cannot be resolved."""


def parse_station_id(value: str) -> str:
    """Extract a bare TuneIn station ID from a station ID or a tunein.com URL.

    Accepts "s345724", "tunein:s345724" or
    "https://tunein.com/radio/Apple-Music-Hits-s345724/".
    """
    raw = (value or "").strip()
    if raw.lower().startswith("tunein:"):
        raw = raw[len("tunein:"):].strip()
    if not raw:
        raise TuneInError("tunein_station_id_empty")

    # A bare ID is the common case; only fall back to scanning when it is a URL.
    if re.fullmatch(r"[sgpt]\d+", raw, re.IGNORECASE):
        return raw.lower()

    if "tunein.com" not in raw.lower():
        raise TuneInError(f"tunein_station_id_unparseable:{raw}")

    matches = _STATION_ID_RE.findall(raw)
    if not matches:
        raise TuneInError(f"tunein_station_id_unparseable:{raw}")

    # In a slug like "Apple-Music-Hits-s345724" the ID is the trailing match.
    return matches[-1].lower()


def is_tunein_source(value: str) -> bool:
    """True if value names a TuneIn station rather than a stream URL."""
    try:
        parse_station_id(value)
    except TuneInError:
        return False
    return True


class TuneInResolver:
    """Resolves TuneIn stations to playable stream URLs via the TuneIn OPML API."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def resolve_stream_url(self, station_id: str) -> tuple[str, str]:
        """Resolve a station to (stream_url, media_type).

        Each call returns a freshly signed URL.
        """
        station_id = parse_station_id(station_id)
        session = await self._get_session()

        try:
            async with session.get(
                TUNE_URL.format(station_id=station_id),
                timeout=aiohttp.ClientTimeout(total=RESOLVE_TIMEOUT_S),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as exc:
            raise TuneInError(f"tunein_resolve_failed station_id={station_id}: {exc}") from exc

        body = data.get("body") or []
        entries = [e for e in body if isinstance(e, dict) and (e.get("url") or "").strip()]
        entries = [e for e in entries if _NOT_COMPATIBLE not in e["url"].lower()]
        if not entries:
            raise TuneInError(
                f"tunein_resolve_no_playable_url station_id={station_id} "
                "(TuneIn returned only its 'not compatible' placeholder)"
            )

        # Prefer HLS: HLS-only stations publish nothing else worth playing.
        entries.sort(key=lambda e: 0 if str(e.get("media_type", "")).lower() == "hls" else 1)
        chosen = entries[0]
        return chosen["url"].strip(), str(chosen.get("media_type", "")).lower()
