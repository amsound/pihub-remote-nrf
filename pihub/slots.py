"""What the remote's number keys (and the listen flow) play, per speaker backend.

Keys 1–9 and 0 are slots 1–10. Each backend decides what a slot is:

  audiopro           slots 1–6 are the speaker's own presets, 7–10 are stream_url_1–4
  samsung_soundbar   slots 1–10 are soundbar_stream_url_1–10

The listen flow's target is expressed as a slot too, so keys and the flow play
things the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SLOT_COUNT = 10
AUDIOPRO_PRESET_SLOTS = 6
SOUNDBAR_BACKEND = "samsung_soundbar"


_TUNEIN_ID_RE = re.compile(r"[sgpt]\d+", re.IGNORECASE)
_TUNEIN_URL_RE = re.compile(r"tunein\.com/.*?\b[sgpt]\d+\b", re.IGNORECASE)


def is_tunein_source(value: str) -> bool:
    """True for a TuneIn station ("s345724", "tunein:s345724" or a tunein.com URL).

    pihub doesn't resolve these itself; the restreamer does, on every connect.
    """
    raw = (value or "").strip()
    if raw.lower().startswith("tunein:"):
        raw = raw[len("tunein:"):].strip()
    return bool(_TUNEIN_ID_RE.fullmatch(raw) or _TUNEIN_URL_RE.search(raw))


class SlotEmptyError(ValueError):
    """The slot has nothing configured to play."""


@dataclass(frozen=True)
class SlotTarget:
    slot: int
    preset: int | None = None  # the speaker's own preset, or...
    url: str = ""              # ...a stream to play
    restream: bool = False     # send the stream via the local restreamer


def resolve_slot(settings: Any, backend: str, slot: int) -> SlotTarget:
    """Return what `slot` plays on `backend`."""
    slot = int(slot)
    if not 1 <= slot <= SLOT_COUNT:
        raise ValueError(f"slot must be between 1 and {SLOT_COUNT}")

    if backend == SOUNDBAR_BACKEND:
        url = settings.get_soundbar_stream_url(slot)
        # pihub cannot play a TuneIn station directly; only the restreamer can.
        restream = settings.get_soundbar_restream(slot) or is_tunein_source(url)
        return SlotTarget(slot=slot, url=url, restream=restream)

    if slot <= AUDIOPRO_PRESET_SLOTS:
        return SlotTarget(slot=slot, preset=slot)
    return SlotTarget(slot=slot, url=settings.get_stream_url(slot - AUDIOPRO_PRESET_SLOTS))


def listen_slot(settings: Any, backend: str) -> int:
    """The slot the listen flow plays."""
    target = settings.get_listen_target()
    if backend == SOUNDBAR_BACKEND:
        return int(target["stream"])
    if target["type"] == "preset":
        return int(target["preset"])
    return AUDIOPRO_PRESET_SLOTS + int(target["stream"])


async def play_slot(speaker: Any, target: SlotTarget) -> None:
    """Play a resolved slot on the speaker."""
    if target.preset is not None:
        await speaker.preset(target.preset)
        return

    if not target.url:
        raise SlotEmptyError(f"slot {target.slot} has no stream URL")

    if target.restream:
        await speaker.play_url(target.url, restream=True)
    else:
        await speaker.play_url(target.url)
