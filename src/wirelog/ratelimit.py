"""Per-client-instance rate limiter.

Combines a token bucket (burst protection) with three rolling windows
(sustained protection: per-minute, per-hour, per-day) and a per-event
payload size cap. All public methods are thread-safe.

The limiter exposes an injectable clock (``now`` function) so tests can
deterministically advance time without ``time.sleep``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

# Defaults applied when a RateLimitConfig field is left at zero.
DEFAULT_EVENTS_PER_SECOND = 1.0
DEFAULT_BURST = 10
DEFAULT_EVENTS_PER_MINUTE = 60
DEFAULT_EVENTS_PER_HOUR = 1000
DEFAULT_EVENTS_PER_DAY = 10000
DEFAULT_MAX_EVENT_BYTES = 65536  # 64 KiB


class DropReason(Enum):
    """Why an event was dropped (or NONE if it was allowed)."""

    NONE = "ok"
    BURST = "burst"
    PER_MINUTE = "per_minute"
    PER_HOUR = "per_hour"
    PER_DAY = "per_day"
    PAYLOAD_TOO_LARGE = "payload_too_large"


@dataclass
class RateLimitConfig:
    """Per-client-instance rate limiter configuration.

    All numeric fields default to safe values when zero. Set
    ``disabled=True`` to bypass all checks. Set ``max_event_bytes`` to a
    negative value to disable the payload size check while leaving the
    rest of the limiter active.
    """

    disabled: bool = False
    events_per_second: float = 0.0
    burst: int = 0
    events_per_minute: int = 0
    events_per_hour: int = 0
    events_per_day: int = 0
    max_event_bytes: int = 0


@dataclass
class RateLimitStats:
    """Cumulative drop counters since client creation."""

    dropped_burst: int = 0
    dropped_per_minute: int = 0
    dropped_per_hour: int = 0
    dropped_per_day: int = 0
    dropped_payload_size: int = 0

    @property
    def total(self) -> int:
        """Sum of all drop counters."""
        return (
            self.dropped_burst
            + self.dropped_per_minute
            + self.dropped_per_hour
            + self.dropped_per_day
            + self.dropped_payload_size
        )


class _WindowCounter:
    """Bucketed sliding-window counter.

    Approximates a true sliding window using ``num_buckets`` fixed-size
    sub-windows. Memory and per-call cost are O(num_buckets), which is
    small (6, 12, or 24 in practice).
    """

    __slots__ = ("bucket_duration", "num_buckets", "buckets")

    def __init__(self, window_seconds: float, num_buckets: int) -> None:
        self.bucket_duration = window_seconds / num_buckets
        self.num_buckets = num_buckets
        # (bucket_start_seconds, count). Initialised to (-inf, 0) so any
        # real bucket start beats them.
        self.buckets: list[list[float | int]] = [
            [float("-inf"), 0] for _ in range(num_buckets)
        ]

    def total(self, now: float) -> int:
        cutoff = now - self.bucket_duration * self.num_buckets
        return sum(
            int(count)
            for start, count in self.buckets
            if count and start >= cutoff
        )

    def add(self, now: float) -> None:
        bucket_start = (now // self.bucket_duration) * self.bucket_duration
        for i in range(self.num_buckets):
            if self.buckets[i][0] == bucket_start:
                self.buckets[i][1] = int(self.buckets[i][1]) + 1
                return
        # Replace oldest.
        oldest_idx = min(
            range(self.num_buckets), key=lambda i: self.buckets[i][0]
        )
        self.buckets[oldest_idx] = [bucket_start, 1]


class RateLimiter:
    """Per-client-instance rate limiter.

    Combines:
      * Token bucket (L1) — burst absorption + sustained-rate drip.
      * Three rolling windows (L2) — per-minute, per-hour, per-day.
      * Per-event payload size cap (L5).

    Args:
        config: RateLimitConfig. Zero-valued fields use defaults.
        now: Optional clock function (monotonic seconds). Defaults to
            ``time.monotonic`` for production; tests inject a fake clock.
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        cfg = config or RateLimitConfig()
        self._disabled = cfg.disabled
        self._now = now or time.monotonic
        self._lock = threading.Lock()

        # Stats are always tracked; allow() short-circuits before stats
        # increment when disabled.
        self._dropped_burst = 0
        self._dropped_per_minute = 0
        self._dropped_per_hour = 0
        self._dropped_per_day = 0
        self._dropped_payload_size = 0

        if cfg.disabled:
            self._cfg = cfg
            self._tokens = 0.0
            self._last_refill = 0.0
            self._minute = _WindowCounter(60.0, 6)
            self._hour = _WindowCounter(3600.0, 12)
            self._day = _WindowCounter(86400.0, 24)
            return

        self._cfg = self._apply_defaults(cfg)
        self._tokens = float(self._cfg.burst)
        self._last_refill = self._now()
        self._minute = _WindowCounter(60.0, 6)
        self._hour = _WindowCounter(3600.0, 12)
        self._day = _WindowCounter(86400.0, 24)

    @staticmethod
    def _apply_defaults(cfg: RateLimitConfig) -> RateLimitConfig:
        return RateLimitConfig(
            disabled=cfg.disabled,
            events_per_second=cfg.events_per_second
            if cfg.events_per_second > 0
            else DEFAULT_EVENTS_PER_SECOND,
            burst=cfg.burst if cfg.burst > 0 else DEFAULT_BURST,
            events_per_minute=cfg.events_per_minute
            if cfg.events_per_minute > 0
            else DEFAULT_EVENTS_PER_MINUTE,
            events_per_hour=cfg.events_per_hour
            if cfg.events_per_hour > 0
            else DEFAULT_EVENTS_PER_HOUR,
            events_per_day=cfg.events_per_day
            if cfg.events_per_day > 0
            else DEFAULT_EVENTS_PER_DAY,
            max_event_bytes=cfg.max_event_bytes
            if cfg.max_event_bytes != 0
            else DEFAULT_MAX_EVENT_BYTES,
        )

    def allow(self) -> DropReason:
        """Check whether one event should pass; consume capacity if so.

        Returns DropReason.NONE on pass, otherwise the specific drop
        reason. Disabled limiters always return NONE without modifying
        any counters.
        """
        if self._disabled:
            return DropReason.NONE

        with self._lock:
            now = self._now()
            elapsed = now - self._last_refill
            if elapsed > 0:
                self._tokens = min(
                    float(self._cfg.burst),
                    self._tokens + elapsed * self._cfg.events_per_second,
                )
                self._last_refill = now

            if self._tokens < 1:
                self._dropped_burst += 1
                return DropReason.BURST
            if self._minute.total(now) >= self._cfg.events_per_minute:
                self._dropped_per_minute += 1
                return DropReason.PER_MINUTE
            if self._hour.total(now) >= self._cfg.events_per_hour:
                self._dropped_per_hour += 1
                return DropReason.PER_HOUR
            if self._day.total(now) >= self._cfg.events_per_day:
                self._dropped_per_day += 1
                return DropReason.PER_DAY

            self._tokens -= 1
            self._minute.add(now)
            self._hour.add(now)
            self._day.add(now)
            return DropReason.NONE

    def record_payload_drop(self) -> None:
        """Increment the payload-too-large counter without consuming tokens."""
        with self._lock:
            self._dropped_payload_size += 1

    def stats(self) -> RateLimitStats:
        """Return a snapshot of cumulative drop counters."""
        with self._lock:
            return RateLimitStats(
                dropped_burst=self._dropped_burst,
                dropped_per_minute=self._dropped_per_minute,
                dropped_per_hour=self._dropped_per_hour,
                dropped_per_day=self._dropped_per_day,
                dropped_payload_size=self._dropped_payload_size,
            )

    def max_event_bytes(self) -> int:
        """Return the configured per-event size cap, or 0 if disabled."""
        if self._disabled:
            return 0
        if self._cfg.max_event_bytes < 0:
            return 0
        return self._cfg.max_event_bytes


def parse_retry_after(header: str | None, now_epoch: float | None = None) -> float:
    """Parse a Retry-After header value to seconds.

    Supports both delta-seconds (``"5"``) and HTTP-date forms.
    Returns 0.0 for missing, unparseable, or non-positive values.
    ``now_epoch`` defaults to ``time.time()`` and is used only for
    HTTP-date deltas.
    """
    if not header:
        return 0.0
    header = header.strip()
    if not header:
        return 0.0
    try:
        seconds = int(header)
    except ValueError:
        pass
    else:
        return float(seconds) if seconds > 0 else 0.0

    # HTTP-date form (RFC 1123).
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return 0.0
    if when is None:
        return 0.0
    epoch = now_epoch if now_epoch is not None else time.time()
    delta = when.timestamp() - epoch
    return float(delta) if delta > 0 else 0.0
