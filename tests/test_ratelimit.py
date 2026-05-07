"""Tests for the per-instance rate limiter and Retry-After parsing."""

from __future__ import annotations

import json
import threading
import time
import unittest
from email.utils import format_datetime
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from wirelog import (
    DropReason,
    PayloadTooLargeError,
    RateLimitConfig,
    RateLimitedError,
    WireLog,
)
from wirelog.ratelimit import RateLimiter, _WindowCounter, parse_retry_after


class FakeClock:
    """Deterministic clock for tests. ``advance`` moves time forward."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


# --- _WindowCounter ---


class TestWindowCounter(unittest.TestCase):
    def test_empty_total_is_zero(self) -> None:
        w = _WindowCounter(60.0, 6)
        self.assertEqual(w.total(0.0), 0)

    def test_adds_and_counts(self) -> None:
        w = _WindowCounter(60.0, 6)
        for _ in range(5):
            w.add(1000.0)
        self.assertEqual(w.total(1000.0), 5)

    def test_evicts_old_buckets(self) -> None:
        w = _WindowCounter(60.0, 6)  # 6 x 10s buckets
        w.add(1000.0)
        w.add(1000.0)
        # 65s later, the original bucket falls outside the 60s window.
        w.add(1065.0)
        self.assertEqual(w.total(1065.0), 1)

    def test_distributes_across_buckets(self) -> None:
        w = _WindowCounter(60.0, 6)
        for i in range(6):
            w.add(1000.0 + i * 10)
        self.assertEqual(w.total(1055.0), 6)


# --- RateLimiter token bucket (L1) ---


class TestRateLimiterBurst(unittest.TestCase):
    def test_allows_burst_up_to_capacity(self) -> None:
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1,
                burst=10,
                events_per_minute=1000,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
            now=clock.now,
        )
        for _ in range(10):
            self.assertEqual(r.allow(), DropReason.NONE)
        self.assertEqual(r.allow(), DropReason.BURST)

    def test_refills_over_time(self) -> None:
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1,
                burst=5,
                events_per_minute=1000,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
            now=clock.now,
        )
        for _ in range(5):
            r.allow()
        self.assertEqual(r.allow(), DropReason.BURST)
        clock.advance(3.0)
        for _ in range(3):
            self.assertEqual(r.allow(), DropReason.NONE)
        self.assertEqual(r.allow(), DropReason.BURST)

    def test_refill_caps_at_burst(self) -> None:
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1,
                burst=3,
                events_per_minute=1000,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
            now=clock.now,
        )
        clock.advance(100.0)
        for _ in range(3):
            self.assertEqual(r.allow(), DropReason.NONE)
        self.assertEqual(r.allow(), DropReason.BURST)


# --- RateLimiter sustained windows (L2) ---


class TestRateLimiterWindows(unittest.TestCase):
    def test_per_minute(self) -> None:
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1000,
                burst=1000,
                events_per_minute=5,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
            now=clock.now,
        )
        for _ in range(5):
            self.assertEqual(r.allow(), DropReason.NONE)
        self.assertEqual(r.allow(), DropReason.PER_MINUTE)

    def test_per_minute_recovers_after_window(self) -> None:
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1000,
                burst=1000,
                events_per_minute=3,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
            now=clock.now,
        )
        for _ in range(3):
            r.allow()
        self.assertEqual(r.allow(), DropReason.PER_MINUTE)
        clock.advance(61.0)
        self.assertEqual(r.allow(), DropReason.NONE)

    def test_per_hour(self) -> None:
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1000,
                burst=1000,
                events_per_minute=1000,
                events_per_hour=10,
                events_per_day=1_000_000,
            ),
            now=clock.now,
        )
        for _ in range(10):
            self.assertEqual(r.allow(), DropReason.NONE)
        self.assertEqual(r.allow(), DropReason.PER_HOUR)

    def test_per_day(self) -> None:
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1000,
                burst=1000,
                events_per_minute=1000,
                events_per_hour=1000,
                events_per_day=7,
            ),
            now=clock.now,
        )
        for _ in range(7):
            clock.advance(3600.0)
            self.assertEqual(r.allow(), DropReason.NONE)
        self.assertEqual(r.allow(), DropReason.PER_DAY)


class TestRateLimiterSustainedDrip(unittest.TestCase):
    def test_catches_one_per_second_leak_for_two_hours(self) -> None:
        # Defaults: 1/s burst+rate, 60/min, 1000/hr, 10000/day.
        # Even though token bucket refills at 1/s, the per-hour cap of 1000
        # should kick in well before 2 hours of attempted sends.
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1,
                burst=10,
                events_per_minute=60,
                events_per_hour=1000,
                events_per_day=10000,
            ),
            now=clock.now,
        )
        allowed = 0
        dropped = 0
        for _ in range(7200):
            clock.advance(1.0)
            if r.allow() is DropReason.NONE:
                allowed += 1
            else:
                dropped += 1
        # At 1/sec sustained for 2h we'd want 7200; per-hour cap of 1000
        # should throttle us down to roughly ~2000 allowed.
        self.assertLess(allowed, 2200, f"allowed={allowed} dropped={dropped}")
        self.assertGreater(dropped, 5000)


class TestRateLimiterDisabled(unittest.TestCase):
    def test_disabled_allows_everything(self) -> None:
        r = RateLimiter(RateLimitConfig(disabled=True))
        for _ in range(100_000):
            self.assertEqual(r.allow(), DropReason.NONE)
        self.assertEqual(r.stats().total, 0)


class TestRateLimiterStats(unittest.TestCase):
    def test_stats_count_by_reason(self) -> None:
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1,
                burst=2,
                events_per_minute=5,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
            now=clock.now,
        )
        for _ in range(5):
            r.allow()
        self.assertEqual(r.stats().dropped_burst, 3)

        clock.advance(5.0)
        for _ in range(3):
            r.allow()
        clock.advance(5.0)
        for _ in range(3):
            r.allow()
        self.assertGreater(r.stats().dropped_per_minute, 0)

        r.record_payload_drop()
        r.record_payload_drop()
        self.assertEqual(r.stats().dropped_payload_size, 2)


class TestRateLimiterConcurrency(unittest.TestCase):
    def test_concurrent_access_is_safe(self) -> None:
        clock = FakeClock()
        r = RateLimiter(
            RateLimitConfig(
                events_per_second=1000,
                burst=1000,
                events_per_minute=1_000_000,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
            now=clock.now,
        )

        allowed = [0]
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(100):
                if r.allow() is DropReason.NONE:
                    with lock:
                        allowed[0] += 1

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 5000 attempts, burst of 1000, no clock advance — exactly 1000 pass.
        self.assertEqual(allowed[0], 1000)


# --- parse_retry_after ---


class TestParseRetryAfter(unittest.TestCase):
    def test_seconds(self) -> None:
        self.assertEqual(parse_retry_after("5"), 5.0)

    def test_empty(self) -> None:
        self.assertEqual(parse_retry_after(""), 0.0)
        self.assertEqual(parse_retry_after(None), 0.0)
        self.assertEqual(parse_retry_after("   "), 0.0)

    def test_negative(self) -> None:
        self.assertEqual(parse_retry_after("-5"), 0.0)
        self.assertEqual(parse_retry_after("0"), 0.0)

    def test_http_date(self) -> None:
        now_ts = 1_000_000.0
        future = datetime.fromtimestamp(now_ts + 30, tz=timezone.utc)
        header = format_datetime(future, usegmt=True)
        got = parse_retry_after(header, now_epoch=now_ts)
        self.assertGreater(got, 28.0)
        self.assertLess(got, 32.0)

    def test_past_http_date(self) -> None:
        now_ts = 1_000_000.0
        past = datetime.fromtimestamp(now_ts - 30, tz=timezone.utc)
        header = format_datetime(past, usegmt=True)
        self.assertEqual(parse_retry_after(header, now_epoch=now_ts), 0.0)

    def test_garbage(self) -> None:
        self.assertEqual(parse_retry_after("not a date"), 0.0)


# --- WireLog client integration ---


class _MockHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_body = b'{"accepted": 1}'
    response_headers: dict[str, str] = {}
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _MockHandler.requests.append(
            {"path": self.path, "body": json.loads(body) if body else {}}
        )
        self.send_response(_MockHandler.response_status)
        self.send_header("Content-Type", "application/json")
        for k, v in _MockHandler.response_headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(_MockHandler.response_body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class TestClientIntegration(unittest.TestCase):
    server: HTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _MockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        _MockHandler.response_status = 200
        _MockHandler.response_body = b'{"accepted": 1}'
        _MockHandler.response_headers = {}
        _MockHandler.requests = []

    def _url(self) -> str:
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}"

    def test_track_honours_burst_limit(self) -> None:
        clock = FakeClock()
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            rate_limit=RateLimitConfig(
                events_per_second=1,
                burst=5,
                events_per_minute=1000,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
            _now=clock.now,
        )
        for _ in range(100):
            client.track("spam")
        stats = client.rate_limit_stats()
        self.assertEqual(stats.dropped_burst, 95)
        # Only the 5 allowed events should have hit the server.
        self.assertEqual(len(_MockHandler.requests), 5)

    def test_track_rejects_oversized_payload(self) -> None:
        errors: list[Exception] = []
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            rate_limit=RateLimitConfig(
                events_per_second=1000,
                burst=1000,
                events_per_minute=1_000_000,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
                max_event_bytes=200,
            ),
            on_error=errors.append,
        )
        client.track("small")
        client.track("big", event_properties={"blob": "x" * 1000})
        too_large = [e for e in errors if isinstance(e, PayloadTooLargeError)]
        self.assertEqual(len(too_large), 1)
        self.assertEqual(client.rate_limit_stats().dropped_payload_size, 1)

    def test_track_disabled_rate_limit_passes_all(self) -> None:
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            rate_limit=RateLimitConfig(disabled=True),
        )
        for _ in range(100):
            client.track("test")
        self.assertEqual(client.rate_limit_stats().total, 0)
        self.assertEqual(len(_MockHandler.requests), 100)

    def test_default_rate_limit_is_active(self) -> None:
        # Defaults (1/s, burst 10) must drop a 100-event flood.
        errors: list[Exception] = []
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            on_error=errors.append,
        )
        for _ in range(100):
            client.track("flood")
        rate_drops = [e for e in errors if isinstance(e, RateLimitedError)]
        self.assertGreater(len(rate_drops), 80)
        self.assertEqual(client.rate_limit_stats().dropped_burst, len(rate_drops))

    def test_identify_honours_burst_limit(self) -> None:
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            rate_limit=RateLimitConfig(
                events_per_second=1,
                burst=3,
                events_per_minute=1000,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
        )
        for _ in range(3):
            client.identify("u")
        with self.assertRaises(RateLimitedError):
            client.identify("u")
        self.assertEqual(client.rate_limit_stats().dropped_burst, 1)

    def test_track_batch_disabled_or_closed_short_circuits(self) -> None:
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            disabled=True,
        )
        result = client.track_batch([{"event_type": "x"}])
        self.assertEqual(result, {"accepted": 0})
        self.assertEqual(len(_MockHandler.requests), 0)

    def test_track_batch_rejects_over_2000(self) -> None:
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            rate_limit=RateLimitConfig(disabled=True),
        )
        events = [{"event_type": "e"} for _ in range(2001)]
        with self.assertRaises(ValueError):
            client.track_batch(events)
        self.assertEqual(len(_MockHandler.requests), 0)

    def test_track_batch_drops_oversize_events_individually(self) -> None:
        errors: list[Exception] = []
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            rate_limit=RateLimitConfig(
                events_per_second=1000,
                burst=1000,
                events_per_minute=1_000_000,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
                max_event_bytes=200,
            ),
            on_error=errors.append,
        )
        events = [
            {"event_type": "small"},
            {"event_type": "big", "event_properties": {"blob": "x" * 500}},
            {"event_type": "small2"},
        ]
        client.track_batch(events)
        too_large = [e for e in errors if isinstance(e, PayloadTooLargeError)]
        self.assertEqual(len(too_large), 1)
        self.assertEqual(client.rate_limit_stats().dropped_payload_size, 1)
        # Server should have received the 2 surviving events.
        self.assertEqual(len(_MockHandler.requests), 1)
        sent = _MockHandler.requests[-1]["body"]["events"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["event_type"], "small")
        self.assertEqual(sent[1]["event_type"], "small2")

    def test_track_batch_burst_drops_in_excess_of_capacity(self) -> None:
        errors: list[Exception] = []
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            rate_limit=RateLimitConfig(
                events_per_second=1,
                burst=5,
                events_per_minute=1000,
                events_per_hour=1_000_000,
                events_per_day=1_000_000,
            ),
            on_error=errors.append,
        )
        events = [{"event_type": "e"} for _ in range(20)]
        client.track_batch(events)
        rate_drops = [e for e in errors if isinstance(e, RateLimitedError)]
        self.assertEqual(len(rate_drops), 15)
        self.assertEqual(client.rate_limit_stats().dropped_burst, 15)
        # Server should have received only the 5 that fit in the burst.
        self.assertEqual(len(_MockHandler.requests), 1)
        sent = _MockHandler.requests[-1]["body"]["events"]
        self.assertEqual(len(sent), 5)

    def test_track_batch_disabled_rate_limit_passes_all(self) -> None:
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
            rate_limit=RateLimitConfig(disabled=True),
        )
        events = [{"event_type": "e"} for _ in range(50)]
        client.track_batch(events)
        self.assertEqual(client.rate_limit_stats().total, 0)
        self.assertEqual(len(_MockHandler.requests), 1)
        self.assertEqual(len(_MockHandler.requests[-1]["body"]["events"]), 50)

    def test_retry_after_is_parsed(self) -> None:
        _MockHandler.response_status = 429
        _MockHandler.response_body = b'{"error":"slow down"}'
        _MockHandler.response_headers = {"Retry-After": "7"}
        client = WireLog(
            api_key="sk_test",
            host=self._url(),
            flush_interval=0,
        )
        try:
            client.track("test")
        except Exception as e:  # noqa: BLE001
            from wirelog import WireLogError

            self.assertIsInstance(e, WireLogError)
            assert isinstance(e, WireLogError)
            self.assertEqual(e.retry_after, 7.0)
        else:
            self.fail("expected WireLogError")


if __name__ == "__main__":
    unittest.main()
