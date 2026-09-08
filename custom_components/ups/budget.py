"""The request budget UPS grants an unvalidated session, modelled as tokens.

This carrier does not throttle a rate, it grants a count: a measured three
requests per address, after which every further request hangs until the
address has rested. Spacing requests further apart buys nothing — only the
number of them matters — so pacing has to be accounted, not delayed.

Wall clock rather than monotonic, so the balance survives a restart. A budget
that resets on boot protects against nothing: restarting a few times while
configuring is exactly the burst this carrier closes on.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class RequestBudget:
    """A refilling allowance of requests, persisted across restarts."""

    capacity: int
    refill_seconds: float
    tokens: float = 0.0
    updated_utc: float = 0.0

    def __post_init__(self) -> None:
        """Start a budget that came from nowhere at full capacity."""
        if not self.updated_utc:
            # A fresh install starts full: the address has not been spent
            # against, and withholding the first lookups would only make the
            # integration look broken on the one day the user is watching it.
            self.tokens = float(self.capacity)
            self.updated_utc = time.time()

    def _accrue(self, now: float) -> None:
        elapsed = now - self.updated_utc
        if elapsed < 0:
            # Clock moved backwards (NTP on a Pi without an RTC). Re-anchor
            # rather than banking the skew as credit.
            self.updated_utc = now
            return
        if elapsed < self.refill_seconds:
            # Anchor only on whole tokens, otherwise repeated calls each drop
            # their own remainder and the budget never actually refills.
            return
        earned = elapsed // self.refill_seconds
        self.tokens = min(float(self.capacity), self.tokens + earned)
        if self.tokens >= self.capacity:
            # Re-anchor when full, or an idle week banks a week of credit and
            # the first poll after it spends the whole grace in one go.
            self.updated_utc = now
        else:
            self.updated_utc += earned * self.refill_seconds

    def available(self, now: float | None = None) -> int:
        """Whole tokens spendable right now."""
        self._accrue(time.time() if now is None else now)
        return int(self.tokens)

    def try_spend(self, now: float | None = None) -> bool:
        """Spend one token, or report that there was none to spend."""
        now = time.time() if now is None else now
        if self.available(now) < 1:
            return False
        self.tokens -= 1
        if self.updated_utc < now:
            # Restart the refill clock at this request, not at the moment the
            # token happened to be earned. The measured beat is the gap
            # between two *answered* requests, so a banked token spent late —
            # or three spent minutes apart out of a full bucket — would
            # otherwise put the next request less than a beat after the last
            # one. Three codes added at once drain the bucket in ten minutes
            # and the fourth request would fall 65 minutes after the third,
            # inside the hour that costs a hang plus a two-hour stand-down.
            #
            # It costs a little throughput: each cycle's gap becomes a refill
            # plus that cycle's stagger and jitter rather than a flat refill.
            # That is the same arithmetic that put the interval past the beat
            # instead of on it.
            self.updated_utc = now
        return True

    def exhaust(self, now: float | None = None) -> None:
        """Record that the address closed anyway, and stop asking.

        Reached when a request hangs despite the accounting saying there was
        budget left — the estimate was wrong, and the only safe reading is
        that nothing is left.
        """
        now = time.time() if now is None else now
        self._accrue(now)
        self.tokens = 0.0
        self.updated_utc = now

    def seconds_until_token(self, now: float | None = None) -> float:
        """How long until at least one token is spendable."""
        now = time.time() if now is None else now
        if self.available(now) >= 1:
            return 0.0
        return max(0.0, self.updated_utc + self.refill_seconds - now)

    def as_dict(self) -> dict[str, Any]:
        """Return the balance in a form the coordinator's Store can hold."""
        return {"tokens": self.tokens, "updated_utc": self.updated_utc}

    @classmethod
    def from_dict(
        cls, stored: Any, capacity: int, refill_seconds: float
    ) -> RequestBudget:
        """Restore a balance, falling back to a full one on anything odd.

        Capacity and refill come from the constants, never from disk: a stored
        refill interval would silently outlive the measurement that set it.
        """
        budget = cls(capacity=capacity, refill_seconds=refill_seconds)
        if not isinstance(stored, dict):
            return budget
        tokens = stored.get("tokens")
        updated = stored.get("updated_utc")
        if not isinstance(tokens, (int, float)) or not isinstance(
            updated, (int, float)
        ):
            return budget
        if updated <= 0 or tokens < 0:
            return budget
        budget.tokens = min(float(capacity), float(tokens))
        budget.updated_utc = float(updated)
        return budget
