"""Tests for the UPS coordinator: fetching, caching and events.

The parcel mapping itself is covered by ``test_parcels.py``.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ups.api import UPSApiError
from custom_components.ups.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.ups.coordinator import UPSCoordinator

from .payloads import ACTIVE_CODE, DELIVERED_CODE, active_sample, delivered_sample

OTHER_CODE = "1Z888888888888888"


def _fake_client(**kwargs) -> AsyncMock:
    """A stand-in API client that keeps the real one's request counter.

    The coordinator charges the budget from ``requests_made``, and a bare
    Mock's auto-created attribute is not a number.
    """
    client = AsyncMock(**kwargs)
    client.requests_made = 0
    return client


def _entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id=DOMAIN,
    )


def _in_transit(code: str = ACTIVE_CODE) -> dict:
    """A ladder position short of out_for_delivery."""
    sample = delivered_sample(code)
    sample.update(
        {
            "packageStatus": "In Transit",
            "packageStatusType": "I",
            "packageStatusCode": "IT",
            "isDelivered": False,
            "currentMilestone": {"nameKey": "cms.stapp.inTransit"},
            # drop the delivered + out-for-delivery scans, keep the rest
            "shipmentProgressActivities": sample["shipmentProgressActivities"][2:],
        }
    )
    sample["milestones"] = [
        {**m, "isCurrent": m["nameKey"] == "cms.stapp.inTransit"}
        for m in sample["milestones"]
    ]
    return sample


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_update_merges_multiple_parcels(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = lambda code, **kwargs: (
        active_sample() if code == ACTIVE_CODE else delivered_sample()
    )
    coordinator = UPSCoordinator(hass, client, entry)

    # One request per cycle, so both codes need two turns to be known.
    await coordinator._async_update_data()
    data = await coordinator._async_update_data()

    assert len(data) == 1  # one active
    assert data[0]["barcode"] == ACTIVE_CODE
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_not_found_shows_pending_placeholder(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = None  # not found
    coordinator = UPSCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["barcode"] == OTHER_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_update_keeps_cached_payload_on_error(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()  # populates the cache

    client.async_get_parcel.side_effect = UPSApiError("HTTP 500")
    await coordinator._async_update_data()  # error -> cached raw reused
    assert len(coordinator.delivered) == 1


async def test_a_total_failure_publishes_placeholders_instead_of_raising(hass):
    """Three codes added, three sensors — even when the first fetch fails.

    Raising ``UpdateFailed`` here discarded every parcel built for this cycle
    and took the entities unavailable. Observed live: three codes added at
    once, the first fetch 401'd on a stale CSRF token, and the user saw
    nothing at all rather than three parcels reading `unknown`.
    """
    codes = [DELIVERED_CODE, ACTIVE_CODE, "1Z888888888888888"]
    entry = _entry_with([{CONF_TRACKING_CODE: c} for c in codes])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = UPSApiError("HTTP 500")
    coordinator = UPSCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert {p["barcode"] for p in data} == set(codes)
    assert all(p["status"] == ParcelStatus.UNKNOWN for p in data)
    # The failure still stands the coordinator down and is not counted as a
    # successful poll.
    assert coordinator.consecutive_failures == 1
    assert coordinator.last_success_time is None


async def test_update_reraises_unexpected_exceptions(hass):
    """Only API and network errors are tolerated; a bug must not be swallowed."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = ValueError("boom")
    coordinator = UPSCoordinator(hass, client, entry)

    with pytest.raises(ValueError):
        await coordinator._async_update_data()


async def test_update_skips_items_missing_a_tracking_code(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ""}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1  # empty item never fetched


async def test_update_backfills_missing_tracking_number(hass):
    """An edge payload without requestedTrackingNumber keeps the requested code."""
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    sample = active_sample()
    del sample["requestedTrackingNumber"]
    del sample["trackingNumber"]
    client = _fake_client()
    client.async_get_parcel.return_value = sample
    coordinator = UPSCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()
    assert data[0]["barcode"] == OTHER_CODE


async def test_update_prunes_cache_for_untracked_parcels(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UPSCoordinator(hass, client, entry)
    coordinator._raw_cache["GONE"] = {"trackingNumber": "GONE"}

    await coordinator._async_update_data()

    assert "GONE" not in coordinator._raw_cache
    assert DELIVERED_CODE in coordinator._raw_cache


async def test_update_fetches_parcels_sequentially_not_concurrently(hass):
    """Only single-number calls are known to work, and firing them
    concurrently is unverified — unlike the template's default
    asyncio.gather, this carrier fetches one tracking number at a time."""
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    in_flight = 0
    peak = 0

    async def _slow_fetch(code, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return active_sample(code)

    client = _fake_client()
    client.async_get_parcel.side_effect = _slow_fetch
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert peak == 1  # never more than one in-flight fetch at a time


async def test_a_delivered_code_is_never_fetched_again(hass):
    """Delivered is terminal — re-polling it is pure throttle budget."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.call_count == 1
    client.async_get_parcel.reset_mock()

    await coordinator._async_update_data()

    client.async_get_parcel.assert_not_called()


async def test_a_delivered_parcel_stays_visible_while_it_is_skipped(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    first = list(coordinator.delivered)
    await coordinator._async_update_data()

    assert coordinator.delivered == first


async def test_only_the_delivered_code_is_skipped_not_the_active_one(hass):
    entry = _entry_with([
        {CONF_TRACKING_CODE: DELIVERED_CODE},
        {CONF_TRACKING_CODE: ACTIVE_CODE},
    ])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = lambda code, **kw: (
        delivered_sample() if code == DELIVERED_CODE else active_sample()
    )
    coordinator = UPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    client.async_get_parcel.reset_mock()
    data = await coordinator._async_update_data()

    assert [c.args[0] for c in client.async_get_parcel.call_args_list] == [
        ACTIVE_CODE
    ]
    assert len(data) == 1  # the active parcel is still published
    assert len(coordinator.delivered) == 1  # and so is the delivered one


async def test_untracking_a_delivered_code_clears_it_from_the_skip_set(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_PARCELS: []})
    await coordinator._async_update_data()
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_PARCELS: [{CONF_TRACKING_CODE: DELIVERED_CODE}]},
    )
    client.async_get_parcel.reset_mock()
    await coordinator._async_update_data()

    # Re-added after removal: we no longer know its state, so fetch it again.
    client.async_get_parcel.assert_called_once()


async def test_cache_only_poll_does_not_stamp_last_success(hass):
    """A poll served entirely from cache must not look like a success."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    stamp = coordinator.last_success_time
    assert stamp is not None

    client.async_get_parcel.side_effect = UPSApiError("HTTP 500")
    await coordinator._async_update_data()  # served from cache
    assert coordinator.last_success_time == stamp


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    """Otherwise every restart floods the user with "registered" events."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample()
    coordinator = UPSCoordinator(hass, client, entry)

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = _fake_client()
    coordinator = UPSCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _in_transit()
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_fires_status_changed_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    coordinator = UPSCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _in_transit()
    await coordinator._async_update_data()  # first refresh: suppressed

    client.async_get_parcel.return_value = active_sample()  # out for delivery
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    """The hop to delivered fires exactly one, dedicated event."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    coordinator = UPSCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = delivered_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == ACTIVE_CODE
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when first tracked fires nothing at all."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.side_effect = lambda code, **kwargs: (
        active_sample(code) if code == ACTIVE_CODE else delivered_sample(code)
    )
    coordinator = UPSCoordinator(hass, client, entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    await coordinator._async_update_data()  # first refresh seeds the state

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: DELIVERED_CODE},
            ],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    coordinator = UPSCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()  # first refresh: suppressed

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
    client.async_get_parcel.side_effect = lambda code, **kwargs: active_sample(code)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == OTHER_CODE


async def test_delivery_time_changed_never_fires_for_this_carrier(hass):
    """planned_from/planned_to are held at None — no confirmed ETA field for
    an in-flight parcel yet — so this event can never fire for UPS today.
    Documents the current, real behaviour rather than testing a window this
    carrier doesn't publish."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = _fake_client()
    coordinator = UPSCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _in_transit()
    await coordinator._async_update_data()  # first refresh: suppressed

    client.async_get_parcel.return_value = active_sample()  # status changes
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []
