"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ups.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.ups.parcels import (
    apply_delivered_filter,
    build_history,
    format_dimensions,
    map_activity_status,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import (
    DELIVERED_CODE,
    _activity,
    active_sample,
    delivered_sample,
    not_found_envelope,
    weighed_sample,
)

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("cms.stapp.orderReceived", ParcelStatus.REGISTERED),
        ("cms.stapp.weHaveYourPkg", ParcelStatus.IN_TRANSIT),
        ("cms.stapp.inTransit", ParcelStatus.IN_TRANSIT),
        ("cms.stapp.outForDelivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("cms.stapp.delivered", ParcelStatus.DELIVERED),
        ("cms.stapp.pkgReadyForPickupAP", ParcelStatus.AT_PICKUP_POINT),
        ("cms.stapp.returned", ParcelStatus.RETURNING),
        ("cms.stapp.incompleteDocumentationReceived", ParcelStatus.PROBLEM),
        # Observed on the wire 2026-09-04, from a user's own log.
        ("cms.stapp.retnToSender", ParcelStatus.RETURNING),
        ("cms.stapp.investigationOpened", ParcelStatus.PROBLEM),
        ("cms.stapp.addressChangeRequested", ParcelStatus.IN_TRANSIT),
        ("cms.stapp.addressChangeRequestCompleted", ParcelStatus.IN_TRANSIT),
        ("cms.stapp.dropoffAccessPoint", ParcelStatus.IN_TRANSIT),
    ],
)
def test_map_parcel_status_known(code, expected):
    assert map_parcel_status(code) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    """at_pickup_point/returning/problem have no evidenced nameKey yet — any
    raw key we haven't mapped, including a plausible-looking future one,
    must fall through to unknown, never guessed."""
    assert map_parcel_status("cms.stapp.exceptionOccurred") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("cms.stapp.somethingNew") is None
    assert map_event_status("cms.stapp.delivered") == ParcelStatus.DELIVERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("cms.stapp.abducted") == ParcelStatus.UNKNOWN
    assert map_parcel_status("cms.stapp.abducted") == ParcelStatus.UNKNOWN
    assert caplog.text.count("cms.stapp.abducted") == 1
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_converts_epoch_milliseconds():
    """Suite-wide machinery, unused by normalize_parcel for this carrier (UPS
    stamps GMT date/time pairs, not epoch ms) but kept intact — see the module
    docstring."""
    assert to_iso_timestamp(1784203767167) == "2026-07-16T12:09:27.167000+00:00"
    assert to_iso_timestamp("2026-04-29T13:12:42Z") == "2026-04-29T13:12:42Z"
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(10**20) is None  # out of range -> None, never raises


def test_format_dimensions_needs_all_three_axes():
    """Suite-wide machinery; unused by normalize_parcel — UPS exposes no
    dimension field at all."""
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


# ---------------------------------------------------------------------------
# build_history — GMT-triplet timestamps, newest-first input, null milestones
# ---------------------------------------------------------------------------


def test_build_history_orders_oldest_to_newest():
    """shipmentProgressActivities arrives newest-first; history must not."""
    history = build_history(delivered_sample()["shipmentProgressActivities"])
    assert history[0]["raw_status"] == "cms.stapp.orderReceived"
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_falls_back_to_the_scan_code():
    """A DP (Departed from Facility) scan carries no milestoneName at all —
    on a real parcel that is the great majority of scans, so the entry takes
    its actCode rather than coming out empty."""
    history = build_history(delivered_sample()["shipmentProgressActivities"])
    dp_entries = [entry for entry in history if entry["raw_status"] == "DP"]
    assert len(dp_entries) == 1
    assert dp_entries[0]["status"] == ParcelStatus.IN_TRANSIT


def test_build_history_prefers_the_milestone_over_the_scan_code():
    """The milestone is the better source wherever there is one."""
    history = build_history(
        [
            {
                "actCode": "WH",
                "gmtDate": "20260627",
                "gmtTime": "11:51:34",
                "milestoneName": {"nameKey": "cms.stapp.delivered"},
            }
        ]
    )
    assert history[0]["raw_status"] == "cms.stapp.delivered"
    assert history[0]["status"] == ParcelStatus.DELIVERED


def test_build_history_keeps_a_scan_with_no_status_at_all():
    """Neither field is a reason to drop the entry — the timestamp still is
    the parcel moving."""
    history = build_history(
        [{"gmtDate": "20260627", "gmtTime": "11:51:34", "actCode": ""}]
    )
    assert len(history) == 1
    assert history[0]["raw_status"] is None
    assert history[0]["status"] is None


def test_a_code_that_says_nothing_about_the_parcel_stays_silent(caplog):
    """CG only reports a renumbering. Known, not unknown — nagging about a code
    we chose not to map sends the user to open an issue we would close."""
    history = build_history(
        [{"gmtDate": "20260627", "gmtTime": "11:51:34", "actCode": "CG"}]
    )
    assert history[0]["raw_status"] == "CG"
    assert history[0]["status"] is None
    assert "CG" not in caplog.text


def test_a_resolved_exception_is_still_an_exception():
    """UPS emits these codes only around friction. Z1's sole sentence is "A
    hold on your package has been resolved" and it is XV's first variant, so
    reading that sentence one way here and another there is not defensible."""
    for code in ("Z1", "B3", "XV", "DB"):
        assert map_activity_status(code) == ParcelStatus.PROBLEM
    for code in ("MF", "H1", "R4", "XN"):
        assert map_activity_status(code) == ParcelStatus.PROBLEM


def test_a_closed_investigation_is_still_a_problem():
    """Seen live: "The investigation has been closed. We were unable to contact
    the receiver." Closing one does not undo the parcel having needed one, and
    the scan code behind it (UP) already reads as a problem."""
    assert map_event_status("cms.stapp.investigationClosed") == ParcelStatus.PROBLEM
    assert map_activity_status("UP") == ParcelStatus.PROBLEM


def test_the_map_covers_the_statuses_a_scan_can_reach():
    """A scan naming its own return leg must not read as in_transit, and a
    delivery scan must not read as a problem."""
    assert map_activity_status("AM") == ParcelStatus.RETURNING
    assert map_activity_status("KB") == ParcelStatus.DELIVERED
    assert map_activity_status("49") == ParcelStatus.PROBLEM
    assert map_activity_status("OT") == ParcelStatus.OUT_FOR_DELIVERY
    assert map_activity_status("MP") == ParcelStatus.REGISTERED


def test_an_unknown_scan_code_warns_once_and_names_the_field(caplog):
    """A scan code and a nameKey are different vocabularies — a report has to
    say which one it found."""
    assert map_activity_status("ZZ") is None
    assert map_activity_status("ZZ") is None
    assert caplog.text.count("actCode=ZZ") == 1
    assert "issues/new" in caplog.text


def test_a_scan_code_and_a_status_key_warn_separately(caplog):
    """The one-shot set is shared; the two namespaces must not shadow one
    another in it."""
    map_activity_status("QQ")
    map_event_status("QQ")
    assert "actCode=QQ" in caplog.text
    assert caplog.text.count("QQ") == 2


def test_build_history_caps_to_max_events():
    events = [
        {
            "actCode": "AR",
            "gmtDate": f"202604{day:02d}",
            "gmtTime": "10:00:00",
            "milestoneName": {"nameKey": "cms.stapp.inTransit"},
        }
        for day in range(1, 26)
    ]
    assert len(build_history(events, max_events=20)) == 20


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"actCode": "AR"}]) == []  # no GMT date/time
    assert build_history(["not-a-dict"]) == []


def test_build_history_uses_gmt_not_local_time():
    """Uses gmtDate/gmtTime, never the locale-formatted date/time."""
    history = build_history(
        [
            {
                "actCode": "FS",
                "gmtDate": "20260627",
                "gmtTime": "11:51:34",
                "date": "27/06/2026",  # local, DD/MM/YYYY — must be ignored
                "time": "13:51",
                "milestoneName": {"nameKey": "cms.stapp.delivered"},
            }
        ]
    )
    assert history[0]["timestamp"] == "2026-06-27T11:51:34+00:00"


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample."""
    delivered = normalize_parcel(delivered_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert delivered["planned_from"] is not None or delivered["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert delivered["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "UPS"
    assert parcel["barcode"] == DELIVERED_CODE
    # Never publish sender/receiver for this carrier: shipToAddress is PII,
    # senderShipperNumber is an account number, not a name.
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "cms.stapp.delivered"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42+00:00"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["url"] == (
        f"https://www.ups.com/track?loc=en_US&tracknum={DELIVERED_CODE}"
    )
    # never seen populated on the wire yet
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 6
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_active_parcel_not_delivered():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    # No confirmed ETA field yet — always None until one is.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_isDelivered_drives_delivered_not_the_status_map():
    """delivered comes from isDelivered, a real boolean — never derived from
    the status map."""
    raw = delivered_sample()
    raw["isDelivered"] = False
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.DELIVERED  # still maps as delivered
    assert parcel["delivered"] is False  # but the flag is not derived from it
    assert parcel["delivered_at"] is None


def test_a_returned_parcel_is_stamped_from_its_last_scan():
    """Observed live 2026-09-08: a parcel handed back to the shipper carries
    isDelivered with no delivered milestone anywhere in its timeline. Left
    unstamped it sorted last in the delivered sensor and the days-based
    retention filter — which keeps what it cannot parse — never dropped it."""
    raw = delivered_sample()
    raw["packageStatus"] = "Returned to Sender"
    raw["currentMilestone"] = {"nameKey": "cms.stapp.retnToSender"}
    raw["milestones"] = [{"nameKey": "cms.stapp.retnToSender", "isCurrent": True}]
    for activity in raw["shipmentProgressActivities"]:
        if (activity.get("milestoneName") or {}).get("nameKey") == "cms.stapp.delivered":
            activity["milestoneName"] = None
            activity["actCode"] = "UA"

    parcel = normalize_parcel(raw)

    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42+00:00"


def test_a_delivered_scan_still_wins_over_a_later_one():
    """The newest scan is only the fallback. A parcel whose delivery is
    followed by a further scan must still be stamped at the delivery."""
    raw = delivered_sample()
    raw["shipmentProgressActivities"].insert(
        0, _activity("WH", None, "20260430", "09:00:00")
    )
    assert normalize_parcel(raw)["delivered_at"] == "2026-04-29T13:12:42+00:00"


def test_an_active_parcel_is_never_stamped_from_its_last_scan():
    """The fallback rides on isDelivered, so a parcel still in flight cannot
    pick up a delivered_at from whatever its newest scan happens to be."""
    raw = delivered_sample()
    raw["isDelivered"] = False
    for activity in raw["shipmentProgressActivities"]:
        activity["milestoneName"] = None
    assert normalize_parcel(raw)["delivered_at"] is None


def test_normalize_falls_back_from_current_milestone_to_milestones_list():
    raw = delivered_sample()
    raw["currentMilestone"] = None
    parcel = normalize_parcel(raw)
    assert parcel["raw_status"] == "cms.stapp.delivered"


def test_normalize_falls_back_to_newest_activity_milestone():
    raw = delivered_sample()
    raw["currentMilestone"] = None
    raw["milestones"] = []
    parcel = normalize_parcel(raw)
    # newest activity (index 0) is the FS/delivered scan
    assert parcel["raw_status"] == "cms.stapp.delivered"


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-resolved code still yields a full parcel dict."""
    parcel = normalize_parcel({"requestedTrackingNumber": "1Z000000000000000"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None


def test_normalize_keeps_the_useful_raw_fields():
    """The subset is about size — a field worth an automation stays."""
    raw = delivered_sample()
    raw["shipToAddress"] = {"city": "Amsterdam"}
    raw["receivedBy"] = "J. Doe"
    parcel = normalize_parcel(raw)
    assert parcel["raw"]["shipToAddress"] == {"city": "Amsterdam"}
    assert parcel["raw"]["receivedBy"] == "J. Doe"
    assert parcel["raw"]["isDelivered"] is raw["isDelivered"]


def test_normalize_drops_the_track_page_furniture():
    """A full payload published whole put one event past the recorder's
    32 KB limit; the scans are already published as ``history``."""
    raw = delivered_sample()
    raw["sendUpdatesOptions"] = {"myChoicePreferencesLink": "https://ups.com/x"}
    raw["promo"] = {"title": "Save on shipping"}
    raw["signatureImage"] = "iVBORw0KGgo..."
    raw["shipmentProgressActivities"] = [{"actCode": "AR"}]
    parcel = normalize_parcel(raw, include_history=True)
    for dropped in (
        "sendUpdatesOptions",
        "promo",
        "signatureImage",
        "shipmentProgressActivities",
    ):
        assert dropped not in parcel["raw"]
    assert parcel["history"] is not None


def test_normalize_does_not_narrow_the_cached_payload():
    """The coordinator caches what came back, so trimming must not mutate it."""
    raw = delivered_sample()
    raw["promo"] = {"title": "Save on shipping"}
    normalize_parcel(raw)
    assert "promo" in raw


def test_normalize_weight_empty_string_is_none():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["weight"] is None


def test_normalize_weight_converts_kgs():
    parcel = normalize_parcel(weighed_sample())
    assert parcel["weight"] == 1.25


def test_normalize_weight_converts_lbs():
    raw = weighed_sample()
    raw["additionalInformation"]["weight"] = "10"
    raw["additionalInformation"]["weightUnit"] = "LBS"
    parcel = normalize_parcel(raw)
    assert parcel["weight"] == pytest.approx(4.5359237)


def test_normalize_weight_unrecognised_unit_is_none():
    raw = weighed_sample()
    raw["additionalInformation"]["weightUnit"] = "STONE"
    parcel = normalize_parcel(raw)
    assert parcel["weight"] is None


def test_normalize_barcode_prefers_requested_over_echoed():
    raw = delivered_sample()
    raw["requestedTrackingNumber"] = "1z999aa10123456999"
    raw["trackingNumber"] = "1Z999AA10123456999"
    parcel = normalize_parcel(raw)
    assert parcel["barcode"] == "1z999aa10123456999"


def test_normalize_warns_once_on_tracking_number_mismatch(caplog):
    raw = delivered_sample()
    raw["requestedTrackingNumber"] = "1Z000000000000001"
    raw["trackingNumber"] = "1Z000000000000002"
    normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("echoed a different tracking number") == 1


def test_normalize_uses_requested_locale_in_url():
    parcel = normalize_parcel(delivered_sample(), locale="en_NL")
    assert "loc=en_NL" in parcel["url"]


def test_normalize_not_found_placeholder_from_error_envelope_never_reaches_here():
    """api.py already turns errorCode 504 into None before normalize_parcel is
    called — this just documents the coordinator's own placeholder shape is
    what reaches normalize_parcel instead."""
    envelope = not_found_envelope("1Z000000000000000")
    # normalize_parcel would treat this as an active-but-unmapped parcel if
    # ever handed the raw envelope directly — assert it does NOT crash, since
    # a future refactor might change who filters errorCode.
    parcel = normalize_parcel(envelope)
    assert parcel["barcode"] == "1Z000000000000000"


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


def test_a_parcel_at_a_pickup_point_publishes_pickup_true():
    """``pickup`` and ``status`` may not contradict each other.

    There is still no access-point object in any payload, so
    ``pickup_point`` stays None — but the milestone does say the parcel is
    waiting to be collected, and an automation keys on the boolean.
    """
    parcel = normalize_parcel(
        {
            "requestedTrackingNumber": "1Z999AA10123456784",
            "currentMilestone": {"nameKey": "cms.stapp.pkgReadyForPickupAP"},
        }
    )

    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_a_parcel_in_transit_publishes_pickup_false():
    parcel = normalize_parcel(
        {
            "requestedTrackingNumber": "1Z999AA10123456784",
            "currentMilestone": {"nameKey": "cms.stapp.inTransit"},
        }
    )

    assert parcel["pickup"] is False
