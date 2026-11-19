"""The parcel contract's checks, run over every normalised sample."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.ups.const import ParcelStatus
from custom_components.ups.parcels import (
    is_pickup,
    normalize_parcel,
    planned_for_status,
    planned_window,
)

from .payloads import _activity, active_sample, delivered_sample

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

_AMS = ZoneInfo("Europe/Amsterdam")


def _with_eta(sample: dict) -> dict:
    sample.update(
        {
            "sdd": "20990429",
            "sdst": "13:00:00",
            "sdt": "15:00:00",
            "shipmentGMTInfo": {"shipToGMTOffset": "+02:00"},
        }
    )
    return sample


def _at_pickup_point() -> dict:
    sample = _with_eta(active_sample())
    sample["currentMilestone"] = {"nameKey": "cms.stapp.pkgIsReadyForPickup"}
    return sample


def _collected() -> dict:
    sample = delivered_sample()
    sample["currentMilestone"] = {"nameKey": "cms.stapp.customerPickUp"}
    sample["shipmentProgressActivities"].append(
        _activity("UA", "cms.stapp.pkgIsReadyForPickup", "20260428", "17:00:00")
    )
    return sample


def _returned() -> dict:
    sample = delivered_sample()
    sample["currentMilestone"] = {"nameKey": "cms.stapp.retnToSender"}
    return sample


def _returning() -> dict:
    sample = _with_eta(active_sample())
    sample["currentMilestone"] = {"nameKey": "cms.stapp.retnToSender"}
    return sample


def _moment_only() -> dict:
    sample = _with_eta(active_sample())
    sample["sdt"] = "13:00:00"
    return sample


SAMPLES = {
    "active": lambda: _with_eta(active_sample()),
    "at_pickup_point": _at_pickup_point,
    "collected": _collected,
    "delivered": delivered_sample,
    "returned": _returned,
    "returning": _returning,
    "moment_only": _moment_only,
    "bare": lambda: {"requestedTrackingNumber": "1Z999AA10123456000"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param](), home_time_zone=_AMS)


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (
        parcel["status"] is ParcelStatus.DELIVERED
        or (parcel["status"] is ParcelStatus.RETURNING and parcel["delivered"])
    )


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert datetime.fromisoformat(parcel["planned_to"]) > datetime.fromisoformat(
            parcel["planned_from"]
        )


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = normalize_parcel(_collected())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True


def test_a_parcel_returned_to_the_shipper_is_delivered_but_returning():
    parcel = normalize_parcel(_returned())
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is True
    assert parcel["planned_from"] is None


def test_a_returning_parcel_is_not_delivered():
    sample = _returning()
    sample["isDelivered"] = False
    parcel = normalize_parcel(sample)
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_a_window_keeps_both_ends():
    parcel = normalize_parcel(_with_eta(active_sample()))
    assert parcel["planned_from"] == "2099-04-29T13:00:00+02:00"
    assert parcel["planned_to"] == "2099-04-29T15:00:00+02:00"


def test_a_moment_ends_at_the_end_of_that_day():
    parcel = normalize_parcel(_moment_only())
    assert parcel["planned_from"] == "2099-04-29T13:00:00+02:00"
    assert parcel["planned_to"] == "2099-04-29T23:59:59+02:00"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T15:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            None,
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            date(2026, 4, 29),
            date(2026, 4, 29),
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            "2026-05-01",
            ("2026-04-29T00:00:00+02:00", "2026-05-01T23:59:59+02:00"),
        ),
        (
            "2026-04-29T15:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            None,
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            datetime(2026, 4, 29, 13, 0),
            datetime(2026, 4, 29, 15, 0),
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (None, None, (None, None)),
        ("garbage", None, (None, None)),
        (12, 13, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=_AMS) == expected


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_planned_for_status_empties_the_window(status):
    assert planned_for_status(status, "a", "b") == (None, None)


def test_planned_for_status_keeps_the_window_otherwise():
    for status in (
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.UNKNOWN,
    ):
        assert planned_for_status(status, "a", "b") == ("a", "b")


def test_is_pickup_sources():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(False, ParcelStatus.IN_TRANSIT, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(None, ParcelStatus.DELIVERED) is False


@pytest.mark.parametrize(
    ("delivered", "uap", "picked", "status"),
    [
        (False, False, False, ParcelStatus.AT_PICKUP_POINT),
        (False, True, False, ParcelStatus.AT_PICKUP_POINT),
        (False, False, True, ParcelStatus.DELIVERED),
        (False, True, True, ParcelStatus.DELIVERED),
        (True, False, False, ParcelStatus.DELIVERED),
        (True, True, False, ParcelStatus.AT_PICKUP_POINT),
        (True, False, True, ParcelStatus.DELIVERED),
        (True, True, True, ParcelStatus.DELIVERED),
    ],
)
def test_pickup_point_status_across_every_flag_combination(
    delivered, uap, picked, status
):
    sample = _at_pickup_point()
    sample["isDelivered"] = delivered
    sample["isDeliveredToUAP"] = uap
    sample["isPickedUpByCustomer"] = picked
    parcel = normalize_parcel(sample, home_time_zone=_AMS)
    assert parcel["status"] is status
    assert parcel["delivered"] is (status is ParcelStatus.DELIVERED)
    assert parcel["pickup"] is (uap or picked or status is ParcelStatus.AT_PICKUP_POINT)


def test_a_picked_up_parcel_is_delivered_even_when_in_transit_otherwise():
    sample = _with_eta(active_sample())
    sample["isPickedUpByCustomer"] = True
    assert (
        normalize_parcel(sample, home_time_zone=_AMS)["status"]
        is ParcelStatus.DELIVERED
    )


def test_delivered_to_the_access_point_alone_is_not_delivered():
    sample = _with_eta(active_sample())
    sample["isDeliveredToUAP"] = True
    assert (
        normalize_parcel(sample, home_time_zone=_AMS)["status"]
        is not ParcelStatus.DELIVERED
    )


@pytest.mark.parametrize("flag", ["isDeliveredToUAP", "isPickedUpByCustomer"])
def test_the_access_point_flags_make_a_parcel_pickup(flag):
    sample = _with_eta(active_sample())
    sample[flag] = True
    assert normalize_parcel(sample, home_time_zone=_AMS)["pickup"] is True


def test_an_ordinary_parcel_is_not_pickup():
    assert normalize_parcel(_with_eta(active_sample()))["pickup"] is False
