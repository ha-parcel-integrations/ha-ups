"""Tests for Section 2.1's dynamic, status-driven polling algorithm.

Pure-function tests for the tiering/scheduling helpers, plus a few
integration checks that ``_async_update_data`` actually wires them up (the
full-stop condition, its resume, and the 429 backoff).
"""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ups.api import UPSApiError
from custom_components.ups.const import (
    COLD_INTERVAL_MINUTES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    JITTER_FRACTION,
    MID_INTERVAL_MINUTES,
    MIN_CYCLE_GAP_SECONDS,
    REQUEST_BUDGET_CAPACITY,
    REQUEST_BUDGET_REFILL_SECONDS,
    ParcelStatus,
)
from custom_components.ups.coordinator import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    UPSCoordinator,
    _hottest_tier_minutes,
    _next_update_interval,
    _stagger_minutes,
)

from .payloads import ACTIVE_CODE, DELIVERED_CODE, active_sample, delivered_sample

OTHER_CODE = "1Z888888888888888"

UTC = timezone.utc


def _fake_client(**kwargs) -> AsyncMock:
    """A stand-in API client that keeps the real one's request counter.

    The coordinator charges the budget from ``requests_made``, and a bare
    Mock's auto-created attribute is not a number.
    """
    client = AsyncMock(**kwargs)
    client.requests_made = 0
    return client


def _out_for_delivery(planned_from: str | None) -> dict:
    return {"status": "out_for_delivery", "planned_from": planned_from}


def _mid(status: str = "in_transit") -> dict:
    return {"status": status, "planned_from": None}


def _at_pickup_point() -> dict:
    return {"status": "at_pickup_point", "planned_from": None}


# ---------------------------------------------------------------------------
# _hottest_tier_minutes
# ---------------------------------------------------------------------------


def test_tier_is_none_when_nothing_active():
    assert _hottest_tier_minutes([], datetime(2026, 1, 1, 12, tzinfo=UTC)) is None


def test_tier_is_cold_when_everything_waits_at_a_pickup_point():
    """Nothing moves until a human collects it — don't spend requests on it."""
    parcels = [_at_pickup_point(), _at_pickup_point()]
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert _hottest_tier_minutes(parcels, now) == COLD_INTERVAL_MINUTES


def test_one_moving_parcel_keeps_the_whole_poll_off_the_cold_tier():
    parcels = [_at_pickup_point(), _mid()]
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_out_for_delivery_still_wins_over_a_waiting_parcel():
    parcels = [_at_pickup_point(), _out_for_delivery(None)]
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_mid_for_non_hot_statuses():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [_mid("registered"), _mid("problem"), _mid("returning")]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_tier_is_hot_when_out_for_delivery_without_planned_from():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [_mid(), _out_for_delivery(None)]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_within_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(minutes=30)  # inside the 1h lookahead
    parcels = [_out_for_delivery(planned.isoformat())]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_mid_before_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(hours=3)  # well outside the 1h lookahead
    parcels = [_out_for_delivery(planned.isoformat())]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


# ---------------------------------------------------------------------------
# _next_update_interval
# ---------------------------------------------------------------------------


_WAIT = 3600.0


def test_no_wait_at_all_fully_suspends():
    """``None`` means there is nothing left worth asking about."""
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert _next_update_interval(now, None, "entry-1") is None


def test_daytime_candidate_outside_window_is_wait_plus_stagger_and_jitter():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    interval = _next_update_interval(now, _WAIT, "entry-1")
    floor = timedelta(seconds=_WAIT) + timedelta(
        minutes=_stagger_minutes("entry-1")
    )
    # Jitter is additive only: never sooner than the budget allows.
    assert floor <= interval <= floor + timedelta(seconds=_WAIT * JITTER_FRACTION)


def test_an_affordable_request_still_waits_the_minimum_cycle_gap():
    """A zero wait must not turn into a spin — see MIN_CYCLE_GAP_SECONDS."""
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    interval = _next_update_interval(now, 0.0, "entry-1")
    assert interval >= timedelta(seconds=MIN_CYCLE_GAP_SECONDS)


def test_interval_jitter_varies_between_cycles():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    seen = {_next_update_interval(now, _WAIT, "entry-1") for _ in range(50)}
    assert len(seen) > 1


def test_polling_continues_through_the_night():
    """No quiet window here, unlike every sibling — see const.py.

    The budget already caps the load, so pausing overnight would discard a
    quarter of the day's allowance while UPS is scanning parcels.
    """
    for hour in (0, 3, 5, 23):
        now = datetime(2026, 1, 1, hour, 0, tzinfo=UTC)
        interval = _next_update_interval(now, _WAIT, "entry-1")
        assert interval <= timedelta(seconds=_WAIT * (1 + JITTER_FRACTION)) + \
            timedelta(minutes=_stagger_minutes("entry-1"))


# ---------------------------------------------------------------------------
# integration: full stop, resume, and the 429 backoff
# ---------------------------------------------------------------------------


def _entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id=DOMAIN,
    )


async def test_update_interval_none_when_nothing_tracked(hass):
    entry = _entry_with([])
    entry.add_to_hass(hass)
    client = _fake_client()
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.update_interval is None
    assert coordinator.current_tier_minutes is None


async def test_update_interval_resumes_once_a_parcel_is_added(hass):
    entry = _entry_with([])
    entry.add_to_hass(hass)
    client = _fake_client()
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    assert coordinator.update_interval is None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]}
    )
    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()

    assert coordinator.update_interval is not None
    assert coordinator.current_tier_minutes == HOT_INTERVAL_MINUTES


async def test_429_raises_update_failed_with_retry_after(hass, real_budget):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError(
        "HTTP 429", status_code=429, retry_after=120
    )
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    # Clamped up to a refill: a shorter stand-down would only schedule a poll
    # the budget then declines.
    assert coordinator.update_interval >= timedelta(
        seconds=REQUEST_BUDGET_REFILL_SECONDS
    )


async def test_a_long_retry_after_is_honoured_over_the_floor(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError(
        "HTTP 429", status_code=429, retry_after=REQUEST_BUDGET_REFILL_SECONDS * 3
    )
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.update_interval >= timedelta(
        seconds=REQUEST_BUDGET_REFILL_SECONDS * 3
    )


def _refill(coordinator) -> None:
    """Hand the budget back and let the stand-down lapse, as hours would.

    A failed cycle empties the budget and arms a stand-down on purpose, so
    without this a test that drives several cycles in a row would find the
    second one declining to make a request at all — and assert on a backoff
    that never grew.
    """
    coordinator._budget.tokens = float(coordinator._budget.capacity)
    coordinator._standdown_until_utc = None


async def test_backoff_grows_with_consecutive_failures(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError(
        "HTTP 429", status_code=429, retry_after=None
    )
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    first = coordinator.update_interval

    _refill(coordinator)
    await coordinator._async_update_data()

    assert first >= timedelta(seconds=BACKOFF_BASE_SECONDS * 2)
    assert coordinator.update_interval >= timedelta(seconds=BACKOFF_BASE_SECONDS * 4)


async def test_backoff_is_capped(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("request timed out")
    coordinator = UPSCoordinator(hass, client, entry)

    for _ in range(10):
        _refill(coordinator)
        await coordinator._async_update_data()

    assert coordinator.update_interval >= timedelta(seconds=BACKOFF_CAP_SECONDS)


async def test_a_run_of_hangs_backs_off_even_though_no_429_is_sent(hass):
    """The throttle's real symptom is a timeout, not a status code."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("request timed out")
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator._consecutive_failures == 1
    assert coordinator.update_interval == timedelta(
        seconds=BACKOFF_BASE_SECONDS * 2
    )
    # Crucially, slower than every normal tier — not left on the tier cadence,
    # which would keep hammering an endpoint that has stopped answering.
    assert coordinator.update_interval > timedelta(minutes=MID_INTERVAL_MINUTES)


async def test_a_success_clears_the_backoff(hass, freezer):
    freezer.move_to("2026-05-04 20:00:00")
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("request timed out")
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()

    _refill(coordinator)
    client.async_get_parcel.side_effect = None
    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()

    assert coordinator._consecutive_failures == 0
    assert coordinator.current_tier_minutes is not None
    assert coordinator.update_interval < timedelta(seconds=BACKOFF_BASE_SECONDS)


async def test_all_fetches_failing_serves_cache_and_still_backs_off(hass):
    """A populated cache keeps the parcels visible, but not the cadence."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()

    client.async_get_parcel.side_effect = UPSApiError("request timed out")
    data = await coordinator._async_update_data()

    assert data  # cached parcel still published
    assert coordinator.update_interval == timedelta(
        seconds=BACKOFF_BASE_SECONDS * 2
    )


# ---------------------------------------------------------------------------
# the request budget
# ---------------------------------------------------------------------------


async def test_a_cycle_fetches_one_parcel_not_all_of_them(hass):
    """A sweep would spend the whole day's allowance in one go."""
    codes = [{CONF_TRACKING_CODE: f"1Z99999999999999{n:02d}"} for n in range(5)]
    entry = _entry_with(codes)
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    client.async_get_parcel.assert_called_once()


async def test_every_tracked_parcel_is_still_published(hass):
    """Only one is fetched, but none may vanish from the sensors."""
    codes = [{CONF_TRACKING_CODE: f"1Z99999999999999{n:02d}"} for n in range(4)]
    entry = _entry_with(codes)
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) + len(coordinator.delivered) == len(codes)


async def test_refreshes_stop_once_the_budget_is_spent(hass, real_budget):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    for _ in range(REQUEST_BUDGET_CAPACITY):
        await coordinator._async_update_data()
    assert client.async_get_parcel.call_count == REQUEST_BUDGET_CAPACITY

    first = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first)
    second = await coordinator._async_update_data()

    assert client.async_get_parcel.call_count == REQUEST_BUDGET_CAPACITY
    assert second == first


async def test_the_budget_refills_with_time(hass, real_budget):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    for _ in range(REQUEST_BUDGET_CAPACITY + 1):
        await coordinator._async_update_data()
    spent = client.async_get_parcel.call_count

    coordinator._budget.updated_utc -= REQUEST_BUDGET_REFILL_SECONDS + 1
    await coordinator._async_update_data()

    assert client.async_get_parcel.call_count == spent + 1


async def test_a_hang_empties_the_budget_even_if_it_looked_affordable(hass, real_budget):
    """The accounting said there was room and there was not — trust the wire."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError(
        "request timed out", timed_out=True
    )
    coordinator = UPSCoordinator(hass, client, entry)
    assert coordinator._budget.available() == REQUEST_BUDGET_CAPACITY

    await coordinator._async_update_data()

    assert coordinator._budget.available() == 0


async def test_tracking_more_codes_than_the_soft_limit_warns(hass, caplog):
    codes = [{CONF_TRACKING_CODE: f"1Z99999999999999{n:02d}"} for n in range(12)]
    entry = _entry_with(codes)
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert "only refresh about every" in caplog.text


# ---------------------------------------------------------------------------
# restarts, new codes, and the queue
# ---------------------------------------------------------------------------


async def test_a_restart_serves_cache_and_issues_no_http(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()

    first = UPSCoordinator(hass, client, entry)
    await first.async_load_cache()
    await first._async_update_data()
    await hass.async_block_till_done()
    await first._store.async_save(
        {
            "last_fetch_utc": first._last_fetch_utc,
            "raw_cache": first._raw_cache,
            "delivered_codes": sorted(first._delivered_codes),
            "attempted_codes": sorted(first._attempted_codes),
            "budget": first._budget.as_dict(),
        }
    )

    # A fresh coordinator is what a restart produces.
    client.async_get_parcel.reset_mock()
    restarted = UPSCoordinator(hass, client, entry)
    await restarted.async_load_cache()

    assert restarted.seed_from_cache() is True
    client.async_get_parcel.assert_not_called()
    assert restarted.data  # parcels visible immediately, no empty sensors


async def test_a_restart_never_polls_however_long_ago_the_last_one_was(hass):
    """Setup issues no HTTP at all — that sweep is what lost the endpoint."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()

    first = UPSCoordinator(hass, client, entry)
    await first.async_load_cache()
    await first._async_update_data()
    await first._store.async_save(
        {
            "last_fetch_utc": first._last_fetch_utc - 86400,
            "raw_cache": first._raw_cache,
            "delivered_codes": sorted(first._delivered_codes),
            "attempted_codes": sorted(first._attempted_codes),
        }
    )

    client.async_get_parcel.reset_mock()
    restarted = UPSCoordinator(hass, client, entry)
    await restarted.async_load_cache()

    assert restarted.seed_from_cache() is True
    client.async_get_parcel.assert_not_called()


async def test_a_cold_start_seeds_placeholders_rather_than_polling(hass):
    """A code with no cached payload still gets its sensor, showing unknown."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator.async_load_cache()

    assert coordinator.seed_from_cache() is True
    client.async_get_parcel.assert_not_called()
    assert len(coordinator.data) == 1
    assert coordinator.data[0]["status"] == ParcelStatus.UNKNOWN


async def test_seeding_nothing_tracked_does_nothing(hass):
    entry = _entry_with([])
    entry.add_to_hass(hass)
    coordinator = UPSCoordinator(hass, _fake_client(), entry)
    await coordinator.async_load_cache()

    assert coordinator.seed_from_cache() is False


async def test_a_newly_added_code_goes_to_the_front_of_the_queue(hass):
    """Observed live: 3 codes tracked, 1 cached and delivered, 2 invisible.

    The old floor swallowed the two new codes, and because the only cached
    parcel was delivered the tier went to None — so polling suspended and
    never picked them up. A code the user just added is the one case where
    waiting reads as a broken integration.
    """
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = lambda code, **kw: (
        delivered_sample() if code == DELIVERED_CODE else active_sample(code)
    )
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    assert coordinator.update_interval is None  # everything delivered

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: DELIVERED_CODE},
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: OTHER_CODE},
            ],
        },
    )
    client.async_get_parcel.reset_mock()
    await coordinator._async_update_data()

    # One of the two new codes — never the delivered one.
    assert client.async_get_parcel.call_count == 1
    assert client.async_get_parcel.call_args.args[0] in (ACTIVE_CODE, OTHER_CODE)
    assert coordinator.update_interval is not None  # polling resumed


async def test_the_queue_takes_turns_rather_than_starving_anyone(hass):
    """Staleness is the tie-break, so nothing waits behind a hotter parcel."""
    codes = [ACTIVE_CODE, OTHER_CODE, DELIVERED_CODE]
    entry = _entry_with([{CONF_TRACKING_CODE: c} for c in codes])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = lambda code, **kw: active_sample(code)
    coordinator = UPSCoordinator(hass, client, entry)

    for _ in range(len(codes)):
        await coordinator._async_update_data()

    asked = [c.args[0] for c in client.async_get_parcel.call_args_list]
    assert sorted(asked) == sorted(codes)


async def test_a_parcel_out_for_delivery_outranks_one_at_a_pickup_point(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: OTHER_CODE}]
    )
    entry.add_to_hass(hass)
    coordinator = UPSCoordinator(hass, _fake_client(), entry)
    coordinator._attempted_codes = {ACTIVE_CODE, OTHER_CODE}
    coordinator._status_by_code = {
        ACTIVE_CODE: ParcelStatus.AT_PICKUP_POINT,
        OTHER_CODE: ParcelStatus.OUT_FOR_DELIVERY,
    }

    assert coordinator._fetch_queue([ACTIVE_CODE, OTHER_CODE])[0] == OTHER_CODE


async def test_a_hotter_parcel_cannot_starve_a_colder_one(hass, real_budget):
    """Observed live 2026-09-07: one parcel reached in_transit and took all
    three of the address's requests in twenty-two minutes, while five on
    unknown — none of which had ever returned a payload — waited behind it."""
    codes = [f"1Z{n}" for n in range(5)]
    entry = _entry_with(
        [{CONF_TRACKING_CODE: code} for code in [*codes, ACTIVE_CODE]]
    )
    entry.add_to_hass(hass)
    coordinator = UPSCoordinator(hass, _fake_client(), entry)
    now = time.time()
    coordinator._attempted_codes = {*codes, ACTIVE_CODE}
    coordinator._status_by_code = {
        **{code: ParcelStatus.UNKNOWN for code in codes},
        ACTIVE_CODE: ParcelStatus.IN_TRANSIT,
    }
    # The hot one was just served; the cold ones have waited a working day.
    coordinator._last_fetch_by_code = {
        **{code: now - 8 * 3600 for code in codes},
        ACTIVE_CODE: now - 60,
    }

    assert coordinator._fetch_queue([*codes, ACTIVE_CODE])[0] != ACTIVE_CODE


async def test_the_hotter_parcel_still_wins_once_nobody_is_overdue(hass):
    """The overdue band orders stale parcels; it does not outrank status."""
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: OTHER_CODE}]
    )
    entry.add_to_hass(hass)
    coordinator = UPSCoordinator(hass, _fake_client(), entry)
    now = time.time()
    coordinator._attempted_codes = {ACTIVE_CODE, OTHER_CODE}
    coordinator._status_by_code = {
        ACTIVE_CODE: ParcelStatus.UNKNOWN,
        OTHER_CODE: ParcelStatus.OUT_FOR_DELIVERY,
    }
    coordinator._last_fetch_by_code = {ACTIVE_CODE: now, OTHER_CODE: now}

    assert coordinator._fetch_queue([ACTIVE_CODE, OTHER_CODE])[0] == OTHER_CODE


async def test_a_code_that_keeps_failing_does_not_hold_the_front(hass, real_budget):
    """The attempt is stamped before the request, so a fetch that never comes
    back still counts as a turn taken."""
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: OTHER_CODE}]
    )
    entry.add_to_hass(hass)
    coordinator = UPSCoordinator(hass, _fake_client(), entry)
    now = time.time()
    coordinator._attempted_codes = {ACTIVE_CODE, OTHER_CODE}
    coordinator._status_by_code = {
        ACTIVE_CODE: ParcelStatus.UNKNOWN,
        OTHER_CODE: ParcelStatus.UNKNOWN,
    }
    coordinator._last_fetch_by_code = {ACTIVE_CODE: now, OTHER_CODE: now - 8 * 3600}

    assert coordinator._fetch_queue([ACTIVE_CODE, OTHER_CODE])[0] == OTHER_CODE


async def test_a_delivered_code_never_enters_the_queue(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    coordinator = UPSCoordinator(hass, _fake_client(), entry)
    coordinator._delivered_codes = {DELIVERED_CODE}

    assert coordinator._fetch_queue([DELIVERED_CODE]) == []


async def test_every_parcel_stays_visible_while_others_are_refreshed(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = lambda code, **kw: active_sample(code)
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: OTHER_CODE},
            ],
        },
    )
    data = await coordinator._async_update_data()

    assert len(data) == 2  # the cached one plus the newly fetched one


async def test_a_failed_new_code_is_not_retried_straight_away(hass, real_budget):
    """Observed live: ConfigEntryNotReady turned into a retry storm.

    A timed-out fetch leaves the code without data, and treating that as
    "still new" let HA's own 10 s retry issue a fresh 20 s request every
    time — feeding the very cooldown it was waiting out.
    """
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("request timed out")
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.call_count == 1

    client.async_get_parcel.reset_mock()
    # What HA's retry does next, 10 seconds later.
    await coordinator._async_update_data()

    client.async_get_parcel.assert_not_called()


async def test_seeding_from_cache_does_not_cut_a_standdown_short(
    hass, real_budget
):
    """Observed live: a restart cut a 2 h stand-down to 68 minutes.

    ``seed_from_cache`` computed its own interval from the budget alone, so
    publishing from cache overwrote the deadline the load had just restored.
    """
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("request timed out")
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    standdown = coordinator.update_interval
    assert standdown is not None

    # What a restart does: publish everything from cache, issuing no HTTP.
    assert coordinator.seed_from_cache()

    assert coordinator.update_interval >= standdown - timedelta(seconds=60)


async def test_a_refresh_during_a_standdown_does_not_postpone_it(
    hass, real_budget
):
    """Observed live: adding two parcels moved the next poll 33 min later.

    The stand-down used to be a duration recomputed from ``now`` every
    cycle, so every options change restarted it at full length and a user
    adding parcels through the morning could defer it indefinitely.
    """
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("request timed out")
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    deadline = coordinator._standdown_until_utc
    assert deadline is not None
    first_interval = coordinator.update_interval

    # A second parcel arrives, which funnels through a refresh.
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: OTHER_CODE},
            ],
        },
    )
    await coordinator._async_update_data()

    assert coordinator._standdown_until_utc == deadline
    assert coordinator.update_interval <= first_interval


async def test_a_failed_attempt_survives_a_restart(hass, real_budget):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("request timed out")

    first = UPSCoordinator(hass, client, entry)
    await first.async_load_cache()
    await first._async_update_data()
    await hass.async_block_till_done()
    await first._store.async_save(
        {
            "last_fetch_utc": first._last_fetch_utc,
            "raw_cache": first._raw_cache,
            "delivered_codes": sorted(first._delivered_codes),
            "attempted_codes": sorted(first._attempted_codes),
            "consecutive_failures": first._consecutive_failures,
            "budget": first._budget.as_dict(),
        }
    )

    client.async_get_parcel.reset_mock()
    restarted = UPSCoordinator(hass, client, entry)
    await restarted.async_load_cache()
    await restarted._async_update_data()

    # A reboot must not refund the spent budget and try again straight away.
    client.async_get_parcel.assert_not_called()
    # And the backoff carries over rather than resetting to the normal cadence.
    # The stand-down runs to a deadline, so it counts down rather than up:
    # near its full length, never over it.
    assert restarted.update_interval > timedelta(
        seconds=BACKOFF_BASE_SECONDS * 2 - 60
    )


async def test_fetch_durations_are_recorded_for_diagnostics(hass):
    """Both outcomes land in the ring buffer — a report needs the failures."""
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = [
        active_sample(),
        UPSApiError("request timed out"),
    ]
    coordinator = UPSCoordinator(hass, client, entry)

    # One request per cycle, so two cycles to record two outcomes.
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    recorded = coordinator.recent_fetches
    assert [f["ok"] for f in recorded] == [True, False]
    assert "timed out" in recorded[1]["error"]
    assert all(isinstance(f["seconds"], float) for f in recorded)


async def test_a_recorded_failure_never_carries_the_tracking_code(hass):
    """recent_fetches is the one diagnostics field TO_REDACT cannot reach.

    Most of these messages are ours and name no parcel, but one is UPS's own
    errorText passed through verbatim — and diagnostics get pasted into public
    issues.
    """
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError(
        f"Tracking number {ACTIVE_CODE} is not available"
    )
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    error = coordinator.recent_fetches[0]["error"]
    assert ACTIVE_CODE not in error
    assert "not available" in error  # the diagnosis itself survives


async def test_a_parcel_added_with_no_budget_still_gets_its_sensor(hass, real_budget):
    """The whole point of the placeholder: never an invisible parcel.

    Returning the previously published list here would leave a parcel the user
    just added missing from the sensors until the next affordable cycle —
    which, on this carrier, can be hours away.
    """
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = lambda code, **kw: active_sample(code)
    coordinator = UPSCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    coordinator._budget.tokens = 0.0

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: OTHER_CODE},
            ],
        },
    )
    client.async_get_parcel.reset_mock()
    data = await coordinator._async_update_data()

    client.async_get_parcel.assert_not_called()
    assert {p["barcode"] for p in data} == {ACTIVE_CODE, OTHER_CODE}
    added = next(p for p in data if p["barcode"] == OTHER_CODE)
    assert added["status"] == ParcelStatus.UNKNOWN


async def test_a_stand_down_is_not_cut_short_by_the_budget(hass, real_budget):
    """The address stays shut ~2 h; the next token is due in 1¼.

    Recomputing the cadence from the budget alone would poll straight back
    into the cooldown being waited out.
    """
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("request timed out")
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    # A later cycle that can afford nothing must not shorten the stand-down.
    await coordinator._async_update_data()

    assert coordinator.update_interval > timedelta(
        seconds=BACKOFF_BASE_SECONDS * 2 - 60
    )


async def test_three_codes_pasted_at_once_are_not_polled_at_once(hass):
    """The options form takes a whole list, so a bulk add is one submit.

    Three codes arriving together must still become three cycles minutes
    apart, not one burst — and all three must be visible immediately.
    """
    entry = _entry_with([])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = lambda code, **kw: active_sample(code)
    coordinator = UPSCoordinator(hass, client, entry)

    codes = [ACTIVE_CODE, OTHER_CODE, DELIVERED_CODE]
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_PARCELS: [
            {CONF_TRACKING_CODE: c} for c in codes
        ]},
    )
    data = await coordinator._async_update_data()

    assert client.async_get_parcel.call_count == 1
    # None of them may be missing from the sensors while they wait their turn.
    assert {p["barcode"] for p in data} == set(codes)
    # And the next cycle is minutes away, not immediate.
    assert coordinator.update_interval >= timedelta(seconds=MIN_CYCLE_GAP_SECONDS)


async def test_a_bulk_add_drains_the_budget_no_faster_than_it_allows(hass, real_budget):
    """Three codes, three tokens — then it stops, rather than hanging."""
    entry = _entry_with([])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = lambda code, **kw: active_sample(code)
    coordinator = UPSCoordinator(hass, client, entry)

    codes = [ACTIVE_CODE, OTHER_CODE, DELIVERED_CODE, "1Z777777777777777"]
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_PARCELS: [
            {CONF_TRACKING_CODE: c} for c in codes
        ]},
    )
    for _ in range(len(codes)):
        await coordinator._async_update_data()

    assert client.async_get_parcel.call_count == REQUEST_BUDGET_CAPACITY


async def test_an_answered_failure_does_not_empty_the_budget(hass, real_budget):
    """A body we could not parse is still an answer — only silence is proof.

    Five failure paths carry no status code and were answered all the same
    (unparseable body, no trackDetails, and so on). Emptying on one of them
    threw away hours of allowance because UPS returned something odd.
    """
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("unparseable body")
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator._budget.available() == REQUEST_BUDGET_CAPACITY - 1


async def test_a_standdown_blocks_the_request_not_only_the_schedule(hass, real_budget):
    """A stand-down outlasts the next token, and must gate on its own.

    Two hours of stand-down against a token due in one and a quarter leaves
    three quarters of an hour in which every manual route — an options
    change, ``track_parcel``, ``update_entity`` — used to find a token and
    spend it straight back into the cooldown being waited out.
    """
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError(
        "request timed out", timed_out=True
    )
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    calls = client.async_get_parcel.call_count

    # The token arrives well before the stand-down ends.
    coordinator._budget.tokens = float(coordinator._budget.capacity)
    await coordinator._async_update_data()

    assert client.async_get_parcel.call_count == calls
    assert coordinator._consecutive_failures == 1
    assert coordinator._standdown_until_utc is not None


async def test_a_401_retry_is_charged_twice(hass, real_budget):
    """One lookup, two POSTs — the address was asked twice, so charge twice."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()

    async def _double(*args, **kwargs):
        client.requests_made += 2
        return active_sample()

    client.async_get_parcel.side_effect = _double
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator._budget.available() == REQUEST_BUDGET_CAPACITY - 2


async def test_the_soft_limit_warns_once_not_every_cycle(hass, caplog):
    codes = [{CONF_TRACKING_CODE: f"1Z99999999999999{n:02d}"} for n in range(12)]
    entry = _entry_with(codes)
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    caplog.clear()
    coordinator._budget.tokens = float(coordinator._budget.capacity)
    await coordinator._async_update_data()

    assert "parcels that still need updates" not in caplog.text
