from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class CompiledBleFrames:
    """Prebuilt bytes to send to the dongle (already includes opcode)."""
    down: bytes
    up: bytes