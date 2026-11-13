import copy
import threading
import time
from dataclasses import dataclass

import httpx

from .audit import utcnow
from .db import DomainError
from .models import LiveCalls


@dataclass
class RequestCounter:
    price: int = 0
    availability: int = 0
    decision_calls: int = 0

    def live(self):
        return LiveCalls(price=self.price, availability=self.availability)


class ValioClient:
    def __init__(self, base, key, client=None, ttl=60, max_cache_entries=2048):
        self.base, self.key, self.ttl = base.rstrip("/"), key, ttl
        self.max_cache_entries = max_cache_entries
        self.client = client or httpx.Client(timeout=15)
        self.cache = {}
        self.lock = threading.Lock()

    def close(self):
        self.client.close()

    def fetch(self, endpoint, customer, deliver_by, ids, counter, *, fresh=False):
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
                else:
                    missing.append(pid)
        if not missing:
            return result
        if not self.key:
            raise DomainError("VALIO_UNAVAILABLE", "VALIO_API_KEY is not configured", 503)
        setattr(counter, endpoint, getattr(counter, endpoint) + 1)
        try:
            response = self.client.post(
                f"{self.base}/api/{endpoint}", headers={"X-API-Key": self.key},
                json={"customer": customer, "requestedDate": day, "products": missing})
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
                row["_currency"] = body.get("currency", "EUR")
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
