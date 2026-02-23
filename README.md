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

# Track an event
wl.track("signup", user_id="u_123", event_properties={"plan": "free"})

# Query analytics (returns Markdown by default)
result = wl.query("signup | last 7d | count by day")
print(result)

# Identify a user (bind device → user, set profile)
wl.identify("alice@acme.org", device_id="dev_abc", user_properties={"plan": "pro"})
```

## Configuration

```python
wl = WireLog(
    api_key="sk_...",         # or set WIRELOG_API_KEY env var
    host="https://api.wirelog.ai", # or set WIRELOG_HOST env var
    timeout=30,                # HTTP timeout in seconds
)
```

## API

### `wl.track(event_type, *, user_id, device_id, session_id, event_properties, user_properties, insert_id)`

Track a single event. Auto-generates `insert_id` and `time` if not provided.

### `wl.track_batch(events)`

Track multiple events in one request (up to 2000).

### `wl.query(q, *, format="llm", limit=100, offset=0)`

Run a pipe DSL query. Format: `"llm"` (Markdown), `"json"`, or `"csv"`.

### `wl.identify(user_id, *, device_id, user_properties, user_property_ops)`

Bind a device to a user and/or update profile properties.

## Zero Dependencies

This library uses only the Python standard library (`urllib.request`, `json`, `time`, `uuid`, `os`). No `requests`, no `httpx`, no `urllib3`. It works out of the box on any Python 3.9+ installation.

## Learn More

- [WireLog](https://wirelog.ai) — headless analytics for agents and LLMs
- [Query language docs](https://docs.wirelog.ai/query-language)
- [API reference](https://docs.wirelog.ai/reference/api)
