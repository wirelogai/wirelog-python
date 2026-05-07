"""WireLog analytics client — zero dependencies."""

from wirelog.client import (
    PayloadTooLargeError,
    RateLimitedError,
    WireLog,
    WireLogError,
    __version__,
)
from wirelog.ratelimit import (
    DropReason,
    RateLimitConfig,
    RateLimitStats,
)

__all__ = [
    "DropReason",
    "PayloadTooLargeError",
    "RateLimitConfig",
    "RateLimitStats",
    "RateLimitedError",
    "WireLog",
    "WireLogError",
    "__version__",
]
