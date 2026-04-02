# wirelog

[WireLog](https://wirelog.ai) analytics client for Python. **Zero dependencies** — stdlib only.

## Install

```bash
pip install wirelog
```

## Quick Start

```python
from wirelog import WireLog

wl = WireLog(api_key="sk_your_secret_key")

# Track an event (non-blocking, batched automatically)
wl.track("signup", user_id="u_123", event_properties={"plan": "free"})

# Query analytics (returns Markdown by default)
result = wl.query("signup | last 7d | count by day")
print(result)

# Identify a user (bind device → user, set profile)
wl.identify("alice@acme.org", device_id="dev_abc", user_properties={"plan": "pro"})

# Flush remaining events on shutdown
wl.close()
```

## Design Principles

This client is designed to **never break your application**:

- **Non-blocking by default**: `track()` buffers events and returns immediately
- **Automatic batching**: Events are sent in batches (default 10 per batch, or every 2 seconds)
- **Bounded memory**: Queue capped at 10,000 events — oldest events are dropped when full
- **Retry with backoff**: Transient failures (429, 5xx) are retried up to 3 times
- **Graceful shutdown**: `close()` flushes remaining events; also works as a context manager
- **Background thread**: Flush worker is a daemon thread — won't block process exit

## Context Manager

```python
with WireLog(api_key="sk_...") as wl:
    wl.track("signup", user_id="u_123")
# Events are flushed automatically on exit
```

## Configuration

```python
wl = WireLog(
    api_key="sk_...",              # Falls back to WIRELOG_API_KEY env var
    host="https://api.wirelog.ai", # Falls back to WIRELOG_HOST env var
    timeout=30,                    # HTTP timeout in seconds
    flush_interval=2.0,            # Seconds between auto-flushes (0 = sync mode)
    batch_size=10,                 # Max events per batch
    queue_size=10000,              # Max buffered events
    on_error=lambda e: print(e),   # Background error callback
    disabled=False,                # True = track() is a no-op
)
```

## Synchronous Mode

Set `flush_interval=0` to send each `track()` call immediately (blocking):

```python
wl = WireLog(api_key="sk_...", flush_interval=0)
result = wl.track("signup", user_id="u_123")  # blocks, returns {"accepted": 1}
```

## API

### `wl.track(event_type, *, user_id, device_id, session_id, event_properties, user_properties, insert_id, origin, client_originated)`

Track a single event. Auto-generates `insert_id` and `time` if not provided.

### `wl.track_batch(events, *, origin=None, client_originated=None)`

Track multiple events in one request (up to 2000). Always sends immediately.

### `wl.query(q, *, format="llm", limit=100, offset=0)`

Run a pipe DSL query. Format: `"llm"` (Markdown), `"json"`, or `"csv"`.

### `wl.identify(user_id, *, device_id, user_properties, user_property_ops)`

Bind a device to a user and/or update profile properties.

### `wl.flush()`

Flush all buffered events. Blocks until the queue is drained.

### `wl.close()`

Flush remaining events and stop the background thread. Idempotent.

## Zero Dependencies

This library uses only the Python standard library (`urllib.request`, `json`, `threading`, `queue`, `time`, `uuid`, `os`). No `requests`, no `httpx`, no `urllib3`. It works out of the box on any Python 3.9+ installation.

## Learn More

- [WireLog](https://wirelog.ai) — headless analytics for agents and LLMs
- [Query language docs](https://docs.wirelog.ai/query-language/overview/)
- [API reference](https://docs.wirelog.ai/reference/api/)
