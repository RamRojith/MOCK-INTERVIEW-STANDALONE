"""Structured latency telemetry for the mock-interview pipeline.

Every timing event is written as a single `[latency-metrics]` INFO log line in
the form `event=... session=... question=... answer=... duration_ms=...`.
No student or transcript content is logged; only IDs and durations. Events can
be disabled entirely via MOCK_INTERVIEW["LATENCY_LOG"] = False.
"""

import logging
import threading
import time

from django.conf import settings

logger = logging.getLogger(__name__)

_state = threading.local()

LATENCY_LOG_PREFIX = "[latency-metrics]"


def latency_events_enabled():
    return bool(settings.MOCK_INTERVIEW.get("LATENCY_LOG", True))


def _stringify(value, default=""):
    if value is None:
        return default
    value = str(value)
    return default if value in {"", "None"} else value


def record_latency(
    event,
    *,
    session_id="",
    question_id="",
    answer_id="",
    duration_ms=0.0,
    notes="",
):
    """Write one latency event to the log. Returns duration_ms for chaining."""
    if not latency_events_enabled():
        return duration_ms
    logger.info(
        "%s event=%s session=%s question=%s answer=%s duration_ms=%.2f notes=%s",
        LATENCY_LOG_PREFIX,
        _stringify(event),
        _stringify(session_id),
        _stringify(question_id),
        _stringify(answer_id),
        float(duration_ms or 0.0),
        _stringify(notes, default="-")[:500],
    )
    return duration_ms


class timed_latency:
    """Context manager recording a duration on exit, even on exceptions.

    Example:
        with timed_latency("submit.total", session_id=session.public_id):
            ...
    """

    def __init__(
        self,
        event,
        *,
        session_id="",
        question_id="",
        answer_id="",
        notes="",
    ):
        self.event = event
        self.session_id = session_id
        self.question_id = question_id
        self.answer_id = answer_id
        self.notes = notes
        self._started = None

    def __enter__(self):
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._started is None:
            return False
        duration_ms = (time.perf_counter() - self._started) * 1000.0
        record_latency(
            self.event,
            session_id=self.session_id,
            question_id=self.question_id,
            answer_id=self.answer_id,
            duration_ms=duration_ms,
            notes=self.notes,
        )
        return False


class Timer:
    """Manual start/stop timer for spans that cross helper boundaries."""

    def __init__(self, event, *, session_id="", question_id="", answer_id=""):
        self.event = event
        self.session_id = session_id
        self.question_id = question_id
        self.answer_id = answer_id
        self._started = None

    def start(self):
        self._started = time.perf_counter()
        return self

    def stop(self, notes=""):
        if self._started is None:
            return 0.0
        duration_ms = (time.perf_counter() - self._started) * 1000.0
        record_latency(
            self.event,
            session_id=self.session_id,
            question_id=self.question_id,
            answer_id=self.answer_id,
            duration_ms=duration_ms,
            notes=notes,
        )
        return duration_ms


class event_buffer(threading.local):
    """Collects ordered latency events during one request/worker round."""

    def __init__(self):
        self.events = []


_buffer = event_buffer()


def begin_span(label, event):
    """Starts an ordered span attached to the current thread."""
    record = {
        "label": label,
        "event": event,
        "start": time.perf_counter(),
    }
    _buffer.events.append(record)
    return record


def end_span(record, **context):
    duration_ms = (time.perf_counter() - record["start"]) * 1000.0
    record_latency(record["event"], duration_ms=duration_ms, **context)
    record["duration_ms"] = duration_ms
    return duration_ms


def drain_events():
    events = list(_buffer.events)
    _buffer.events = []
    return events