"""Rolling one-hour request budget for the Pstryk API.

Pstryk allows 3 requests per hour per endpoint (pstryk.pl/regulamin-api).
Prices and costs share the endpoint `meter-data/unified-metrics/`, so every
HTTP attempt is counted here before it is sent. A rolling window that stays
within the limit also stays within it for every calendar hour.

Pure module with no Home Assistant imports, so it can be tested with plain pytest.
Times are timezone-aware UTC datetimes.
"""
from datetime import datetime, timedelta

WINDOW = timedelta(hours=1)


class RequestBudget:

    def __init__(self, limit: int, window: timedelta = WINDOW):
        self.limit = limit
        self.window = window
        self._times: list[datetime] = []
        self.blocked_until: datetime | None = None

    def _prune(self, now: datetime) -> None:
        self._times = sorted(t for t in self._times if now - t < self.window)

    def used(self, now: datetime) -> int:
        self._prune(now)
        return len(self._times)

    def has_room(self, now: datetime, keep_free: int = 0) -> bool:
        """True if one more request leaves at least `keep_free` slots unused."""
        if self.blocked_until is not None and now < self.blocked_until:
            return False
        return self.used(now) + 1 + keep_free <= self.limit

    def claim(self, now: datetime, keep_free: int = 0) -> bool:
        """Record a request if there is room for it."""
        if not self.has_room(now, keep_free):
            return False
        self._times.append(now)
        return True

    def restamp(self, claimed: datetime, sent: datetime) -> datetime:
        """Move a claimed request to the time it is sent, so its slot frees an hour after that."""
        if claimed in self._times:
            self._times[self._times.index(claimed)] = sent
        return sent

    def release(self, claimed: datetime) -> None:
        """Give back a claimed slot whose request was never sent."""
        if claimed in self._times:
            self._times.remove(claimed)

    def free_at(self, now: datetime, keep_free: int = 0) -> datetime | None:
        """When `has_room(..., keep_free)` becomes true; None if it already is."""
        if self.has_room(now, keep_free):
            return None
        candidates = []
        if self.blocked_until is not None and now < self.blocked_until:
            candidates.append(self.blocked_until)
        excess = self.used(now) + 1 + keep_free - self.limit
        if excess > 0:
            candidates.append(self._times[excess - 1] + self.window)
        return max(candidates)

    def block_until(self, until: datetime) -> None:
        """No requests before `until` (after a 429)."""
        if self.blocked_until is None or until > self.blocked_until:
            self.blocked_until = until

    def to_dict(self, now: datetime) -> dict:
        self._prune(now)
        blocked = self.blocked_until if self.blocked_until and now < self.blocked_until else None
        return {
            "times": [t.isoformat() for t in self._times],
            "blocked_until": blocked.isoformat() if blocked else None,
        }

    @classmethod
    def from_dict(cls, data: dict, limit: int, window: timedelta = WINDOW) -> "RequestBudget":
        budget = cls(limit, window)
        try:
            budget._times = sorted(datetime.fromisoformat(t) for t in data.get("times", []))
            blocked = data.get("blocked_until")
            budget.blocked_until = datetime.fromisoformat(blocked) if blocked else None
        except (TypeError, ValueError):
            # A damaged log must not stop the integration; count from empty.
            return cls(limit, window)
        return budget
