"""Shared validation helpers for user-provided inputs."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

def _ctx(context: str) -> str:
    return f" ({context})" if context else ""


def parse_ms(
    value: object,
    *,
    default: Optional[int] = None,
    min: int = 0,
    max: int = 5000,
    allow_none: bool = True,
    log: logging.Logger = logger,
    context: str = "",
) -> Optional[int]:
    """Parse a permissive millisecond value with bounds checking."""
    if value is None:
        return default if allow_none else default

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        log.warning("Invalid ms value%s: %r (using default=%s)", _ctx(context), value, default)
        return default

    if parsed < min or parsed > max:
        log.warning(
            "Out-of-range ms value%s: %r (expected %s..%s, using default=%s)",
            _ctx(context),
            parsed,
            min,
            max,
            default,
        )
        return default

    return parsed
