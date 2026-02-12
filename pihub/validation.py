"""Shared validation helpers for user-provided inputs."""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_MS_WHITELIST: Sequence[int] = (0, 40, 80, 100, 500, 1000, 1500, 2000)


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


def parse_ms_whitelist(
    value: object,
    *,
    allowed: Iterable[int] = DEFAULT_MS_WHITELIST,
    default: int = 40,
    log: logging.Logger = logger,
    context: str = "",
) -> int:
    """Parse a strict millisecond value restricted to a whitelist."""
    if value is None:
        return default

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        log.warning("Invalid ms value%s: %r (using default=%s)", _ctx(context), value, default)
        return default

    allowed_set = set(allowed)
    if parsed not in allowed_set:
        log.warning(
            "Non-whitelisted ms value%s: %r (allowed=%s, using default=%s)",
            _ctx(context),
            parsed,
            sorted(allowed_set),
            default,
        )
        return default

    return parsed
