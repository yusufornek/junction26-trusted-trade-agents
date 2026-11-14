import copy
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from .audit import utcnow
from .db import DomainError
from .models import LiveCalls


class UpstreamPacer:
    """Process-wide one-token bucket (5/s), with a strict rolling-window ceiling."""

    def __init__(self, clock=None, sleep=None, wall_clock=None):
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.wall_clock = wall_clock or utcnow
        self.lock = threading.Lock()
        self.dispatch_lock = threading.Lock()
        self.tokens, self.updated = 1.0, self.clock()
        self.calls = deque()
        self.blocked_until = 0.0
        self.blocked_at = None

    def _check_block(self, now):
        remaining = self.blocked_until - now
        if remaining > 0:
            raise DomainError("UPSTREAM_RATE_LIMITED", "Valio calls are paused after HTTP 429", 503,
                              {"retry_after_seconds": max(1, math.ceil(remaining))})

    def acquire(self):
        while True:
            with self.lock:
                now = self.clock()
                self._check_block(now)
                while self.calls and self.calls[0] <= now - 60:
                    self.calls.popleft()
                self.tokens = min(1.0, self.tokens + max(0, now - self.updated) * 5)
                self.updated = now
                recent = [at for at in self.calls if at > now - 1]
                delay = max(0, (1 - self.tokens) / 5,
                            recent[-5] + 1 - now if len(recent) >= 5 else 0)
                if delay <= 1e-9:
                    # Advance a tiny boundary remainder too, avoiding floating-point bursts.
                    if delay > 0:
                        self.sleep(delay)
                        now = self.clock()
                    self.tokens = 0.0
                    self.updated = now
                    self.calls.append(now)
                    return
                # Sleeping outside the lock lets a 429 or health read interrupt queued work.
            self.sleep(delay)

    def block(self, retry_after):
        with self.lock:
            now = self.clock()
            # Concurrent in-flight 429s belong to this same block, not new audit events.
            if self.blocked_until > now:
                return None
            try:
                seconds = float(retry_after)
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                try:
                    until = parsedate_to_datetime(retry_after)
                    until = until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until
                    seconds = max(0, (until - self.wall_clock()).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    seconds = 30
            seconds = max(1, math.ceil(seconds))
            self.blocked_until = now + seconds
            self.blocked_at = self.wall_clock() + timedelta(seconds=seconds)
            return {"blocked_until": self.blocked_at.isoformat(), "retry_after_seconds": seconds}

    def completed(self):
        # Refill from completion: a delayed sender cannot accumulate a burst of
        # already-admitted calls. The one-token bucket intentionally permits no burst.
        with self.lock:
            self.tokens, self.updated = 0.0, self.clock()

    def check(self):
        with self.lock:
            self._check_block(self.clock())

    def health(self):
        with self.lock:
            now = self.clock()
            return {"blocked_until": self.blocked_at if self.blocked_until > now else None,
                    "calls_last_minute": sum(at > now - 60 for at in self.calls)}


UPSTREAM_PACER = UpstreamPacer()


@dataclass
class RequestCounter:
    price: int = 0
    availability: int = 0
    decision_calls: int = 0
    screened_count: int = 0

    def live(self):
        return LiveCalls(price=self.price, availability=self.availability)


class ValioClient:
    def __init__(self, base, key, client=None, ttl=60, max_cache_entries=2048):
        self.base, self.key, self.ttl = base.rstrip("/"), key, ttl
        self.max_cache_entries = max_cache_entries
        self.client = client or httpx.Client(timeout=15)
        self.cache = {}
        self.lock = threading.Lock()
        self.pending_blocks = []

    def health(self):
        return UPSTREAM_PACER.health()

    def flush_rate_limit_audits(self, db):
        # Called after the command transaction has closed so a rejected order cannot
        # roll back the block event. The block itself takes effect immediately.
        from . import audit
        with self.lock:
            blocks, self.pending_blocks = self.pending_blocks, []
        if not blocks:
            return
        try:
            with db.connect(write=True) as conn:
                for block in blocks:
                    audit.append(conn, "system", "UPSTREAM_RATE_LIMITED", "Valio HTTP 429 paused outbound calls.", data=block)
        except BaseException:
            with self.lock:
                self.pending_blocks = blocks + self.pending_blocks
            raise

    def close(self):
        self.client.close()

    def fetch(self, endpoint, customer, deliver_by, ids, counter, *, fresh=False):
        return self._fetch(endpoint, customer, deliver_by, ids, counter, fresh=fresh)

    def _fetch(self, endpoint, customer, deliver_by, ids, counter, *, fresh=False):
        ids = list(dict.fromkeys(str(i) for i in ids))
        result, missing = {}, []
        day = deliver_by.isoformat()
        clock = time.monotonic()
        with self.lock:
            self.cache = {k: v for k, v in self.cache.items() if clock - v[0] < self.ttl}
            for pid in ids:
                cached = self.cache.get((endpoint, customer, day, pid))
                if not fresh and cached:
                    result[pid] = copy.deepcopy(cached[1])
                    result[pid]["_cache_age"] = max(0, clock - cached[0])
                else:
                    missing.append(pid)
        if not missing:
            return result
        if not self.key:
            raise DomainError("VALIO_UNAVAILABLE", "VALIO_API_KEY is not configured", 503)
        try:
            from .limits import SUPPLIER_GUARD
            UPSTREAM_PACER.check()
            with SUPPLIER_GUARD.slot():
                with UPSTREAM_PACER.dispatch_lock:
                    UPSTREAM_PACER.acquire()
                    setattr(counter, endpoint, getattr(counter, endpoint) + 1)
                    try:
                        response = self.client.post(
                            f"{self.base}/api/{endpoint}", headers={"X-API-Key": self.key},
                            json={"customer": customer, "requestedDate": day, "products": missing})
                        if response.status_code == 429:
                            block = UPSTREAM_PACER.block(response.headers.get("Retry-After"))
                            if block is not None:
                                with self.lock:
                                    self.pending_blocks.append(block)
                            UPSTREAM_PACER.check()
                    finally:
                        UPSTREAM_PACER.completed()
            response.raise_for_status()
            body = response.json()
            rows = body["products"]
            if not isinstance(rows, list):
                raise ValueError("Invalid products")
            fetched = body.get("timeNow") or utcnow().isoformat()
            by_id = {str(row["productId"]): row for row in rows}
            for pid in missing:
                row = copy.deepcopy(by_id.get(pid, {"productId": pid, "error": True}))
                row["_fetched_at"] = fetched
                row["_currency"] = body.get("currency")
                result[pid] = row
            with self.lock:
                for pid in missing:
                    self.cache[(endpoint, customer, day, pid)] = (time.monotonic(), copy.deepcopy(result[pid]))
                while len(self.cache) > self.max_cache_entries:
                    self.cache.pop(next(iter(self.cache)))
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # Never include upstream error bodies/headers, which could expose credentials.
            raise DomainError("VALIO_UNAVAILABLE", f"Upstream {endpoint} request failed", 502) from None
        return result
