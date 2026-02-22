"""WireLog analytics client. Zero external dependencies — stdlib only."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class WireLogError(Exception):
    """Raised when the WireLog API returns an error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"WireLog API {status}: {message}")
        self.status = status


class WireLog:
    """WireLog analytics client.

    Zero external dependencies. Uses only the Python standard library.

    Args:
        api_key: API key (pk_, sk_, or aat_). Falls back to WIRELOG_API_KEY env var.
        host: API base URL. Falls back to WIRELOG_HOST env var or https://wirelog.ai.
        timeout: HTTP timeout in seconds. Default 30.
    """

    def __init__(
        self,
        api_key: str | None = None,
        host: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key or os.environ.get("WIRELOG_API_KEY", "")
        self.host = (
            host or os.environ.get("WIRELOG_HOST", "https://wirelog.ai")
        ).rstrip("/")
        self.timeout = timeout

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
    ) -> dict[str, Any]:
        """Track a single event. Returns {"accepted": N}."""
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
        if insert_id is not None:
            body["insert_id"] = insert_id
        else:
            body["insert_id"] = uuid.uuid4().hex
        body["time"] = _iso_now()
        return self._post("/track", body)

    def track_batch(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Track multiple events. Returns {"accepted": N}."""
        return self._post("/track", {"events": events})

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

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        """Send a POST request to the WireLog API."""
        url = f"{self.host}{path}"
        data = json.dumps(body).encode("utf-8")
        req = Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
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


def _iso_now() -> str:
    """Current UTC time in ISO 8601 format."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
