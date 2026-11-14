"""Single-instance token buckets and a bounded supplier-call queue."""
import math
import threading
import time
from contextlib import contextmanager

from .db import DomainError


class Limiter:
    def __init__(self, policy, db, clock=None):
        self.policy, self.db = policy, db
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.buckets = {}

    def limits(self, principal):
        policy = self.policy.get()
        agent = policy.agents.get(principal)
        return agent.rate_limits if agent and agent.rate_limits is not None else policy.rate_limits

    def _bucket(self, principal, category, config):
        now = self.clock()
        key = principal, category
        before = self.buckets.get(key)
        settings = config.model_dump()
        if before is None:
            state = [float(config.capacity), now, settings]
        else:
            accrued = min(before[2]["capacity"], before[0] + max(0, now - before[1]) * before[2]["refill_per_minute"] / 60)
            state = [min(float(config.capacity), accrued), now, settings]
        self.buckets[key] = state
        return state

    def consume(self, principal, category):
        config = getattr(self.limits(principal), category)
        with self.lock:
            state = self._bucket(principal, category, config)
            if state[0] >= 1:
                state[0] -= 1
                return
            retry = max(1, math.ceil((1 - state[0]) * 60 / config.refill_per_minute))
        from . import audit
        from .telemetry import metric
        metric("throttles")
        with self.db.connect(write=True) as conn:
            audit.append(conn, principal, "RATE_LIMITED", "Principal token bucket exhausted.",
                         data={"category": category, "retry_after_seconds": retry})
        raise DomainError("RATE_LIMITED", "Request limit reached; retry after the interval", 429,
                          {"retry_after_seconds": retry})

    def snapshot(self, principal):
        limits = self.limits(principal)
        with self.lock:
            return {name: {**getattr(limits, name).model_dump(),
                           "remaining": self._bucket(principal, name, getattr(limits, name))[0]}
                    for name in ("lookup", "mutation", "read")}


class SourceLimiter:
    """Bound unknown-credential attempts before hashing/comparing credentials."""
    def __init__(self, capacity=60, refill_per_minute=60, clock=None, max_sources=4096):
        from collections import OrderedDict
        self.capacity, self.rate = capacity, refill_per_minute / 60
        self.clock = clock or time.monotonic
        self.max_sources, self.sources, self.lock = max_sources, OrderedDict(), threading.Lock()

    def check(self, source):
        with self.lock:
            now = self.clock()
            tokens, at = self.sources.pop(source, (self.capacity, now))
            tokens = min(self.capacity, tokens + max(0, now - at) * self.rate)
            allowed = tokens >= 1
            self.sources[source] = (tokens - 1 if allowed else tokens, now)
            while len(self.sources) > self.max_sources:
                self.sources.popitem(last=False)
        if not allowed:
            from .telemetry import metric
            metric("throttles")
            raise DomainError("RATE_LIMITED", "Authentication attempt limit reached", 429,
                              {"retry_after_seconds": max(1, math.ceil((1 - tokens) / self.rate))})

    def refund(self, source):
        with self.lock:
            if source in self.sources:
                tokens, at = self.sources[source]
                self.sources[source] = (min(self.capacity, tokens + 1), at)


class UpstreamGuard:
    def __init__(self, concurrency=8, queue_size=16, wait_seconds=5):
        self.slots = threading.BoundedSemaphore(concurrency)
        self.admitted = threading.BoundedSemaphore(concurrency + queue_size)
        self.wait_seconds = wait_seconds

    @contextmanager
    def slot(self):
        if not self.admitted.acquire(blocking=False):
            raise DomainError("UPSTREAM_BUSY", "Supplier queue is full", 503)
        acquired = False
        try:
            acquired = self.slots.acquire(timeout=self.wait_seconds)
            if not acquired:
                raise DomainError("UPSTREAM_BUSY", "Supplier queue wait expired", 503)
            yield
        finally:
            if acquired:
                self.slots.release()
            self.admitted.release()


SUPPLIER_GUARD = UpstreamGuard()
