"""A small in-process rate limiter for the abuse-prone auth endpoints.

Scope, stated plainly: this is a **per-process, in-memory** sliding-window
counter. It is the right size for this service (a single Uvicorn process behind
nginx) and it adds no dependency and no infrastructure. It is *not* correct
across multiple workers or replicas — each would keep its own counters, so the
effective limit multiplies by the worker count. If this service ever scales out,
swap :class:`SlidingWindowLimiter` for a Redis-backed implementation; the call
sites only use :func:`enforce`, so nothing else changes.

What it protects:
  * login          — password guessing
  * register       — bulk account creation
  * forgot-password / resend-verification — using our mail server to spam a
    third party's inbox, and probing which addresses exist

Keys are namespaced per bucket, and separately per client IP and per target
address, so one noisy IP cannot lock a victim out of their own account: the
IP-scoped counter trips first for the attacker, while the address-scoped counter
covers a distributed attempt.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from app.config import settings


@dataclass(frozen=True)
class Rule:
    """`limit` events allowed per `window_seconds`, per key."""

    limit: int
    window_seconds: int


# Tuned to be generous for a human and tight for a script.
RULES: dict[str, Rule] = {
    # 10 sign-in attempts per 15 minutes.
    "login_ip": Rule(limit=10, window_seconds=15 * 60),
    "login_email": Rule(limit=10, window_seconds=15 * 60),
    # 5 new accounts per hour from one address.
    "register_ip": Rule(limit=5, window_seconds=60 * 60),
    # Mail-sending endpoints: strict, because the cost lands on someone else's
    # inbox and on our sending reputation.
    "forgot_ip": Rule(limit=10, window_seconds=60 * 60),
    "forgot_email": Rule(limit=3, window_seconds=60 * 60),
    "resend_user": Rule(limit=3, window_seconds=60 * 60),
}


class SlidingWindowLimiter:
    """Timestamp deque per key. Memory is bounded by pruning on access plus a
    periodic sweep of keys whose windows have fully drained."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()
        self._sweep_interval = 600.0  # 10 minutes

    def check(self, key: str, rule: Rule) -> tuple[bool, int]:
        """Record an attempt. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - rule.window_seconds
        with self._lock:
            self._maybe_sweep(now)
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= rule.limit:
                retry_after = int(hits[0] + rule.window_seconds - now) + 1
                return False, max(retry_after, 1)
            hits.append(now)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        """Clear one key, or everything. Used by the test suite."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < self._sweep_interval:
            return
        self._last_sweep = now
        widest = max((r.window_seconds for r in RULES.values()), default=3600)
        cutoff = now - widest
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            self._hits.pop(key, None)


limiter = SlidingWindowLimiter()


def client_ip(request: Request) -> str:
    """Best-effort client address.

    nginx is configured with ``proxy_set_header X-Forwarded-For
    $proxy_add_x_forwarded_for``, which *appends* the peer address to whatever
    the client sent. So the trustworthy entry is the **last** one — a client
    that forges ``X-Forwarded-For: 1.2.3.4`` only succeeds in adding noise to
    the left of its real address. Taking the first entry (the common mistake)
    would let an attacker rotate the header and bypass the limit entirely.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def enforce(bucket: str, identifier: str) -> None:
    """Count one attempt against ``bucket``; raise 429 when over the limit."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    rule = RULES.get(bucket)
    if rule is None:
        return
    allowed, retry_after = limiter.check(f"{bucket}:{identifier}", rule)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )
