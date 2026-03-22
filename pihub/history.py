"""Structured in-memory history and flow run reports."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
import time
import uuid
from typing import Any


def _now_ts() -> float:
    return round(time.time(), 3)


def _iso_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone()
        .isoformat(timespec="milliseconds")
    )


@dataclass
class HistoryEvent:
    kind: str
    message: str
    level: str = "info"
    flow_name: str | None = None
    trigger: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=_now_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "iso_ts": _iso_ts(self.ts),
            "kind": self.kind,
            "level": self.level,
            "message": self.message,
            "flow_name": self.flow_name,
            "trigger": self.trigger,
            "metadata": dict(self.metadata),
        }


@dataclass
class FlowStepReport:
    step_id: str
    domain: str
    action: str
    mode: str
    status: str = "pending"
    reason: str | None = None
    error: str | None = None
    ts_started: float = field(default_factory=_now_ts)
    ts_finished: float | None = None

    outcome_status: str | None = None
    outcome_reason: str | None = None
    outcome_error: str | None = None
    ts_outcome: float | None = None

    def finish(
        self,
        *,
        status: str,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.error = error
        self.ts_finished = _now_ts()

    def mark_dispatched(self) -> None:
        self.finish(status="dispatched")

    def settle_outcome(
        self,
        *,
        status: str,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        self.outcome_status = status
        self.outcome_reason = reason
        self.outcome_error = error
        self.ts_outcome = _now_ts()

    def to_dict(self) -> dict[str, Any]:
        duration_ms: int | None = None
        if self.ts_finished is not None:
            duration_ms = max(0, int(round((self.ts_finished - self.ts_started) * 1000.0)))

        outcome_duration_ms: int | None = None
        if self.ts_outcome is not None:
            outcome_duration_ms = max(0, int(round((self.ts_outcome - self.ts_started) * 1000.0)))

        return {
            "step_id": self.step_id,
            "domain": self.domain,
            "action": self.action,
            "mode": self.mode,
            "status": self.status,
            "reason": self.reason,
            "error": self.error,
            "ts_started": self.ts_started,
            "iso_ts_started": _iso_ts(self.ts_started),
            "ts_finished": self.ts_finished,
            "iso_ts_finished": _iso_ts(self.ts_finished),
            "duration_ms": duration_ms,
            "outcome_status": self.outcome_status,
            "outcome_reason": self.outcome_reason,
            "outcome_error": self.outcome_error,
            "ts_outcome": self.ts_outcome,
            "iso_ts_outcome": _iso_ts(self.ts_outcome),
            "outcome_duration_ms": outcome_duration_ms,
        }


@dataclass
class FlowRunReport:
    flow_name: str
    trigger: str
    source: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    result: str = "running"
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    ts_started: float = field(default_factory=_now_ts)
    ts_finished: float | None = None
    steps: list[FlowStepReport] = field(default_factory=list)

    def add_step(
        self,
        *,
        step_id: str,
        domain: str,
        action: str,
        mode: str,
    ) -> FlowStepReport:
        step = FlowStepReport(
            step_id=step_id,
            domain=domain,
            action=action,
            mode=mode,
        )
        self.steps.append(step)
        return step

    def add_warning(self, message: str) -> None:
        text = str(message or "").strip()
        if text and text not in self.warnings:
            self.warnings.append(text)

    def promote_to_warning(self) -> None:
        if self.result == "ok":
            self.result = "ok_with_warnings"

    def finish(self, *, result: str, error: str | None = None) -> None:
        self.result = result
        self.error = (str(error).strip() or None) if error else None
        self.ts_finished = _now_ts()

    def to_dict(self) -> dict[str, Any]:
        duration_ms: int | None = None
        if self.ts_finished is not None:
            duration_ms = max(0, int(round((self.ts_finished - self.ts_started) * 1000.0)))

        return {
            "id": self.id,
            "flow_name": self.flow_name,
            "trigger": self.trigger,
            "source": self.source,
            "result": self.result,
            "warnings": list(self.warnings),
            "error": self.error,
            "ts_started": self.ts_started,
            "iso_ts_started": _iso_ts(self.ts_started),
            "ts_finished": self.ts_finished,
            "iso_ts_finished": _iso_ts(self.ts_finished),
            "duration_ms": duration_ms,
            "steps": [step.to_dict() for step in self.steps],
        }


class HistoryStore:
    """Bounded in-memory store for recent events and recent flow reports."""

    def __init__(self, *, max_events: int = 200, max_flow_reports: int = 50) -> None:
        self._lock = RLock()
        self._events: deque[HistoryEvent] = deque(maxlen=max_events)
        self._flow_reports: deque[FlowRunReport] = deque(maxlen=max_flow_reports)

    def add_event(self, event: HistoryEvent) -> None:
        with self._lock:
            self._events.appendleft(event)

    def emit(
        self,
        *,
        kind: str,
        message: str,
        level: str = "info",
        flow_name: str | None = None,
        trigger: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.add_event(
            HistoryEvent(
                kind=kind,
                message=message,
                level=level,
                flow_name=flow_name,
                trigger=trigger,
                metadata=dict(metadata or {}),
            )
        )

    def add_flow_report(self, report: FlowRunReport) -> None:
        with self._lock:
            self._flow_reports.appendleft(report)

    def list_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock:
            return [event.to_dict() for event in list(self._events)[:limit]]

    def list_flow_reports(self, *, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            return [report.to_dict() for report in list(self._flow_reports)[:limit]]