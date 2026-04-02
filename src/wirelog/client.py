"""WireLog analytics client. Zero external dependencies — stdlib only."""

from __future__ import annotations

import atexit
import json
import os
import queue
import threading
import time
import uuid
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__version__ = "0.2.0"

_BATCH_MAX = 10
_QUEUE_MAX = 10000
_RETRY_MAX = 3
_RETRY_BASE_S = 1.0
_RETRY_MAX_DELAY_S = 30.0
_DEFAULT_FLUSH_INTERVAL = 2.0
_DEFAULT_TIMEOUT = 30
_DEFAULT_HOST = "https://api.wirelog.ai"


class WireLogError(Exception):
    """Raised when the WireLog API returns an error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"WireLog API {status}: {message}")
        self.status = status


class WireLog:
    """WireLog analytics client.

    Zero external dependencies. Uses only the Python standard library.

    By default, ``track()`` buffers events in memory and flushes them
    in batches via a background thread (non-blocking). Call ``close()``
    (or use as a context manager) to flush remaining events on shutdown.

    Set ``flush_interval=0`` to disable background batching and send
    each ``track()`` call synchronously (legacy behavior).

    Args:
        api_key: API key (pk_, sk_, or aat_). Falls back to WIRELOG_API_KEY env var.
        host: API base URL. Falls back to WIRELOG_HOST env var. Default https://api.wirelog.ai.
        timeout: HTTP timeout in seconds. Default 30.
        flush_interval: Seconds between automatic background flushes. Default 2.0.
            Set to 0 to disable background batching (every track() blocks).
        batch_size: Max events per batch request. Default 10.
        queue_size: Max events buffered in memory. Default 10000.
            When full, oldest events are dropped.
        on_error: Callback for background errors. Called from the flush thread.
            If None, background errors are silently discarded.
        disabled: If True, track() is a no-op. Useful for test environments.
    """

    def __init__(
        self,
        api_key: str | None = None,
        host: str | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
        batch_size: int = _BATCH_MAX,
        queue_size: int = _QUEUE_MAX,
        on_error: Callable[[Exception], None] | None = None,
        disabled: bool = False,
    ) -> None:
        self.api_key = api_key or os.environ.get("WIRELOG_API_KEY", "")
        self.host = (
            host or os.environ.get("WIRELOG_HOST", _DEFAULT_HOST)
        ).rstrip("/")
        self.timeout = timeout
        self.disabled = disabled
        self._on_error = on_error
        self._batch_size = min(max(batch_size, 1), 2000)
        self._flush_interval = flush_interval
        self._closed = False

        # Async mode: background thread + queue.
        self._async = flush_interval > 0
        if self._async and not disabled:
            self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
                maxsize=queue_size
            )
            self._flush_event = threading.Event()
            self._thread = threading.Thread(
                target=self._worker, daemon=True, name="wirelog-flush"
            )
            self._thread.start()
            atexit.register(self.close)

    def __enter__(self) -> WireLog:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # --- Public API ---

    def track(
        self,
        event_type: str,
        *,
        user_id: str | None = None,
        device_id: str | None = None,
        session_id: str | None = None,
        event_properties: dict[str, Any] | None = None,
        user_properties: dict[str, Any] | None = None,
        insert_id: str | None = None,
        origin: str | None = None,
        client_originated: bool | None = None,
    ) -> dict[str, Any] | None:
        """Track a single event.

        In async mode (default): enqueues the event and returns None.
        In sync mode (flush_interval=0): sends immediately, returns {"accepted": N}.
        """
        if self.disabled or self._closed:
            return None

        body: dict[str, Any] = {"event_type": event_type}
        if user_id is not None:
            body["user_id"] = user_id
        if device_id is not None:
            body["device_id"] = device_id
        if session_id is not None:
            body["session_id"] = session_id
        if event_properties is not None:
            body["event_properties"] = event_properties
        if user_properties is not None:
            body["user_properties"] = user_properties
        body["insert_id"] = insert_id or uuid.uuid4().hex
        if origin is not None:
            body["origin"] = origin
        if client_originated is not None:
            body["clientOriginated"] = client_originated
        body["time"] = _iso_now()
        body["library"] = f"wirelog-python/{__version__}"

        if not self._async:
            return self._post("/track", body)

        # Non-blocking enqueue.
        try:
            self._queue.put_nowait(body)
        except queue.Full:
            # Drop oldest event to make room.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(body)
            except queue.Full:
                self._report_error(
                    RuntimeError("wirelog: event dropped (queue full)")
                )
        return None

    def track_batch(
        self,
        events: list[dict[str, Any]],
        *,
        origin: str | None = None,
        client_originated: bool | None = None,
    ) -> dict[str, Any]:
        """Track multiple events in a single request (up to 2000).

        Always sends immediately, regardless of async/sync mode.
        """
        body: dict[str, Any] = {"events": events}
        if origin is not None:
            body["origin"] = origin
        if client_originated is not None:
            body["clientOriginated"] = client_originated
        return self._post("/track", body)

    def query(
        self,
        q: str,
        *,
        format: str = "llm",
        limit: int = 100,
        offset: int = 0,
    ) -> Any:
        """Run a pipe DSL query. Returns Markdown (default), JSON, or CSV."""
        return self._post(
            "/query",
            {"q": q, "format": format, "limit": limit, "offset": offset},
        )

    def identify(
        self,
        user_id: str,
        *,
        device_id: str | None = None,
        user_properties: dict[str, Any] | None = None,
        user_property_ops: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind device to user and/or set profile properties."""
        body: dict[str, Any] = {"user_id": user_id}
        if device_id is not None:
            body["device_id"] = device_id
        if user_properties is not None:
            body["user_properties"] = user_properties
        if user_property_ops is not None:
            body["user_property_ops"] = user_property_ops
        return self._post("/identify", body)

    def flush(self) -> None:
        """Flush all buffered events. Blocks until the queue is drained.

        No-op in sync mode or when disabled.
        """
        if not self._async or self.disabled or self._closed:
            return
        self._flush_event.set()
        # Wait for the queue to drain.
        self._queue.join()

    def close(self) -> None:
        """Flush remaining events and stop the background thread.

        Idempotent — safe to call multiple times.
        """
        if self._closed:
            return
        self._closed = True
        if not self._async or self.disabled:
            return
        # Send sentinel to stop the worker.
        self._queue.put(None)
        self._flush_event.set()
        self._thread.join(timeout=10.0)

    # --- Background worker ---

    def _worker(self) -> None:
        """Background thread that batches and sends events."""
        batch: list[dict[str, Any]] = []
        while True:
            # Wait for events or flush signal.
            try:
                event = self._queue.get(timeout=self._flush_interval)
                if event is None:
                    # Sentinel — flush and exit.
                    self._send_batch(batch)
                    self._queue.task_done()
                    return
                batch.append(event)
                self._queue.task_done()
            except queue.Empty:
                pass

            # Drain available events up to batch size.
            while len(batch) < self._batch_size:
                try:
                    event = self._queue.get_nowait()
                    if event is None:
                        self._send_batch(batch)
                        self._queue.task_done()
                        return
                    batch.append(event)
                    self._queue.task_done()
                except queue.Empty:
                    break

            # Flush if batch is full or interval elapsed.
            if len(batch) >= self._batch_size or self._flush_event.is_set():
                self._flush_event.clear()
                self._send_batch(batch)
                batch = []

    def _send_batch(self, events: list[dict[str, Any]]) -> None:
        """Send a batch of events with retry on transient errors."""
        if not events:
            return

        payload: dict[str, Any] = {"events": events}
        for attempt in range(_RETRY_MAX + 1):
            try:
                self._post("/track", payload)
                return
            except WireLogError as e:
                if not _is_retryable(e.status):
                    self._report_error(e)
                    return
                if attempt >= _RETRY_MAX:
                    self._report_error(e)
                    return
            except (URLError, OSError) as e:
                if attempt >= _RETRY_MAX:
                    self._report_error(e)
                    return

            delay = min(
                _RETRY_BASE_S * (2**attempt), _RETRY_MAX_DELAY_S
            )
            time.sleep(delay)

    # --- HTTP transport ---

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        """Send a POST request to the WireLog API."""
        url = f"{self.host}{path}"
        data = json.dumps(body).encode("utf-8")
        req = Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"wirelog-python/{__version__}",
                "X-API-Key": self.api_key,
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return json.loads(raw)
                return raw.decode("utf-8")
        except HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")
            raise WireLogError(e.code, msg) from e

    def _report_error(self, err: Exception) -> None:
        if self._on_error is not None:
            try:
                self._on_error(err)
            except Exception:  # noqa: BLE001
                pass  # never let error callback crash the worker


def _iso_now() -> str:
    """Current UTC time in ISO 8601 format."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_retryable(status: int) -> bool:
    return status == 429 or 500 <= status < 600
