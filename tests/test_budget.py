"""Tests for the request budget."""

import pytest

from custom_components.ups.budget import RequestBudget

CAPACITY = 3
REFILL = 3600.0
T0 = 1_700_000_000.0


def _budget(tokens: float = CAPACITY, updated: float = T0) -> RequestBudget:
    budget = RequestBudget(capacity=CAPACITY, refill_seconds=REFILL)
    budget.tokens = tokens
    budget.updated_utc = updated
    return budget


def test_fresh_budget_starts_full():
    """A new install has not spent against the address yet."""
    budget = RequestBudget(capacity=CAPACITY, refill_seconds=REFILL)
    assert budget.available() == CAPACITY


def test_spending_drains_then_refuses():
    budget = _budget()
    assert [budget.try_spend(T0) for _ in range(CAPACITY)] == [True] * CAPACITY
    assert budget.try_spend(T0) is False
    assert budget.available(T0) == 0


def test_partial_interval_earns_nothing():
    """Only whole tokens count — a request cannot be part-paid for."""
    budget = _budget(tokens=0)
    assert budget.available(T0 + REFILL - 1) == 0
    assert budget.try_spend(T0 + REFILL - 1) is False


def test_token_arrives_on_the_interval():
    budget = _budget(tokens=0)
    assert budget.available(T0 + REFILL) == 1
    assert budget.try_spend(T0 + REFILL) is True


def test_refill_accrues_without_losing_the_remainder():
    """Repeated reads must not each drop their own partial interval."""
    budget = _budget(tokens=0)
    for offset in range(0, int(REFILL), 600):
        budget.available(T0 + offset)
    assert budget.available(T0 + REFILL) == 1


def test_refill_is_capped_at_capacity():
    budget = _budget(tokens=0)
    assert budget.available(T0 + REFILL * 100) == CAPACITY


def test_idle_time_does_not_bank_credit():
    """A full budget left alone for a week still only holds its capacity."""
    budget = _budget(tokens=0)
    week = T0 + 7 * 24 * 3600
    assert budget.available(week) == CAPACITY
    for _ in range(CAPACITY):
        assert budget.try_spend(week) is True
    # The banked week must not immediately refill what was just spent.
    assert budget.try_spend(week) is False
    assert budget.available(week + REFILL - 1) == 0
    assert budget.available(week + REFILL) == 1


def test_clock_moving_backwards_is_reanchored_not_banked():
    """NTP on an RTC-less Pi steps the clock; skew is not credit."""
    budget = _budget(tokens=0)
    assert budget.available(T0 - 10 * REFILL) == 0
    assert budget.available(T0 - 10 * REFILL + REFILL) == 1


def test_exhaust_zeroes_and_restarts_the_clock():
    """Reached when a request hangs although the accounting said otherwise."""
    budget = _budget()
    budget.exhaust(T0)
    assert budget.available(T0) == 0
    assert budget.available(T0 + REFILL - 1) == 0
    assert budget.available(T0 + REFILL) == 1


def test_seconds_until_token():
    budget = _budget(tokens=0)
    assert budget.seconds_until_token(T0) == pytest.approx(REFILL)
    assert budget.seconds_until_token(T0 + REFILL / 2) == pytest.approx(REFILL / 2)
    assert budget.seconds_until_token(T0 + REFILL) == 0.0


def test_seconds_until_token_is_zero_while_tokens_remain():
    assert _budget().seconds_until_token(T0) == 0.0


def test_round_trip_through_storage():
    budget = _budget()
    budget.try_spend(T0)
    restored = RequestBudget.from_dict(budget.as_dict(), CAPACITY, REFILL)
    assert restored.available(T0) == CAPACITY - 1


def test_restart_does_not_refund_what_was_spent():
    """A budget that resets on boot protects against nothing."""
    budget = _budget()
    for _ in range(CAPACITY):
        budget.try_spend(T0)
    restored = RequestBudget.from_dict(budget.as_dict(), CAPACITY, REFILL)
    assert restored.available(T0) == 0
    assert restored.available(T0 + REFILL) == 1


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "nonsense",
        {},
        {"tokens": "1", "updated_utc": T0},
        {"tokens": 1, "updated_utc": "later"},
        {"tokens": -1, "updated_utc": T0},
        {"tokens": 1, "updated_utc": 0},
    ],
)
def test_malformed_storage_falls_back_to_a_full_budget(stored):
    assert RequestBudget.from_dict(stored, CAPACITY, REFILL).available() == CAPACITY


def test_stored_tokens_above_capacity_are_clamped():
    """Capacity comes from the constant, so a shrunk capacity still binds."""
    restored = RequestBudget.from_dict(
        {"tokens": 99, "updated_utc": T0}, CAPACITY, REFILL
    )
    assert restored.available(T0) == CAPACITY


def test_stored_refill_interval_is_never_honoured():
    """A refill interval read off disk would outlive the measurement."""
    restored = RequestBudget.from_dict(
        {"tokens": 0, "updated_utc": T0, "refill_seconds": 1}, CAPACITY, REFILL
    )
    assert restored.refill_seconds == REFILL
    assert restored.available(T0 + 1) == 0


def test_a_drained_bucket_rests_a_full_refill_from_the_last_request():
    """The beat is measured between requests, not from when a token accrued.

    Three codes added at once drain a full bucket minutes apart. Anchoring
    the refill on the moment the tokens were *earned* put the fourth request
    barely an hour after the third — inside the window that hangs, and the
    hang costs a two-hour stand-down on top.
    """
    budget = RequestBudget(capacity=3, refill_seconds=4500, tokens=3.0, updated_utc=1000.0)

    assert budget.try_spend(now=1000.0)
    assert budget.try_spend(now=1300.0)
    assert budget.try_spend(now=1600.0)

    assert budget.seconds_until_token(now=1600.0) == 4500
