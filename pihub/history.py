"""Structured in-memory history and flow run reports."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
import json
import os
import time
import uuid
from typing import Any


DEFAULT_HISTORY_PATH = "/data/history.json"


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

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HistoryEvent":
        return cls(
            kind=str(raw.get("kind") or ""),
            message=str(raw.get("message") or ""),
            level=str(raw.get("level") or "info"),
            flow_name=(str(raw["flow_name"]).strip() or None) if raw.get("flow_name") is not None else None,
            trigger=(str(raw["trigger"]).strip() or None) if raw.get("trigger") is not None else None,
            metadata=dict(raw.get("metadata") or {}),
            ts=float(raw.get("ts") or _now_ts()),
        )


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

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FlowStepReport":
        return cls(
            step_id=str(raw.get("step_id") or ""),
            domain=str(raw.get("domain") or ""),
            action=str(raw.get("action") or ""),
            mode=str(raw.get("mode") or ""),
            status=str(raw.get("status") or "pending"),
            reason=(str(raw["reason"]).strip() or None) if raw.get("reason") is not None else None,
            error=(str(raw["error"]).strip() or None) if raw.get("error") is not None else None,
            ts_started=float(raw.get("ts_started") or _now_ts()),
            ts_finished=float(raw["ts_finished"]) if raw.get("ts_finished") is not None else None,
            outcome_status=(str(raw["outcome_status"]).strip() or None) if raw.get("outcome_status") is not None else None,
            outcome_reason=(str(raw["outcome_reason"]).strip() or None) if raw.get("outcome_reason") is not None else None,
            outcome_error=(str(raw["outcome_error"]).strip() or None) if raw.get("outcome_error") is not None else None,
            ts_outcome=float(raw["ts_outcome"]) if raw.get("ts_outcome") is not None else None,
        )


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

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FlowRunReport":
        report = cls(
            flow_name=str(raw.get("flow_name") or ""),
            trigger=str(raw.get("trigger") or ""),
            source=str(raw.get("source") or ""),
            id=str(raw.get("id") or uuid.uuid4().hex),
            result=str(raw.get("result") or "running"),
            warnings=[str(item) for item in (raw.get("warnings") or []) if str(item).strip()],
            error=(str(raw["error"]).strip() or None) if raw.get("error") is not None else None,
            ts_started=float(raw.get("ts_started") or _now_ts()),
            ts_finished=float(raw["ts_finished"]) if raw.get("ts_finished") is not None else None,
        )
        report.steps = [
            FlowStepReport.from_dict(step)
            for step in (raw.get("steps") or [])
            if isinstance(step, dict)
        ]
        return report


class HistoryStore:
    """Bounded in-memory store for recent events and recent flow reports."""

    def __init__(
        self,
        *,
        max_events: int = 200,
        max_flow_reports: int = 50,
        path: str = DEFAULT_HISTORY_PATH,
    ) -> None:
        self._lock = RLock()
        self._events: deque[HistoryEvent] = deque(maxlen=max_events)
        self._flow_reports: deque[FlowRunReport] = deque(maxlen=max_flow_reports)
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> None:
        with self._lock:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except FileNotFoundError:
                return
            except Exception:
                return

            events_raw = raw.get("events") if isinstance(raw, dict) else []
            flows_raw = raw.get("flows") if isinstance(raw, dict) else []

            self._events.clear()
            self._flow_reports.clear()

            for item in events_raw or []:
                if isinstance(item, dict):
                    self._events.append(HistoryEvent.from_dict(item))

            for item in flows_raw or []:
                if isinstance(item, dict):
                    self._flow_reports.append(FlowRunReport.from_dict(item))

    def flush(self) -> None:
        with self._lock:
            self._write_locked()

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._flow_reports.clear()
            self._write_locked()

    def add_event(self, event: HistoryEvent) -> None:
        with self._lock:
            self._events.appendleft(event)
            self._write_locked()

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
            self._write_locked()

    def list_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock:
            return [event.to_dict() for event in list(self._events)[:limit]]

    def list_flow_reports(self, *, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            return [report.to_dict() for report in list(self._flow_reports)[:limit]]

    def _write_locked(self) -> None:
        parent = os.path.dirname(self._path) or "."
        os.makedirs(parent, exist_ok=True)

        payload = {
            "events": [event.to_dict() for event in self._events],
            "flows": [report.to_dict() for report in self._flow_reports],
        }

        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp, self._path)