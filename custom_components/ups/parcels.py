"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific: :data:`_STATUS_MAP` (with
:data:`_ACTIVITY_CODE_MAP` beside it) and :func:`normalize_parcel`. Everything
else — the timestamp parsing, the history builder, the sort contract, the
delivered filter, the one-shot warning for unmapped statuses — is suite-wide
machinery and should be left alone.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_LOCALE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-ups/issues/new"
    "?template=unrecognised_status.yml"
)

# Map on nameKey, never on packageStatus/name — those are localised display
# strings that change with the requested locale. The keys below are taken from
# UPS's own Dutch tracking translation bundle; their wording establishes the
# milestone meaning without relying on actCode or scan-text similarity.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "cms.stapp.orderReceived": ParcelStatus.REGISTERED,
    "cms.stapp.readyForShpmt": ParcelStatus.REGISTERED,
    "cms.stapp.returnLabelCreated": ParcelStatus.REGISTERED,
    "cms.stapp.weHaveYourPkg": ParcelStatus.IN_TRANSIT,
    "cms.stapp.inTransit": ParcelStatus.IN_TRANSIT,
    "cms.stapp.shipped": ParcelStatus.IN_TRANSIT,
    "cms.stapp.collected": ParcelStatus.IN_TRANSIT,
    "cms.stapp.pickedUpByUPS": ParcelStatus.IN_TRANSIT,
    "cms.stapp.clearedCustoms": ParcelStatus.IN_TRANSIT,
    "cms.stapp.clearedImprtCustoms": ParcelStatus.IN_TRANSIT,
    "cms.stapp.tenderedToUPSDeliveryAgent": ParcelStatus.IN_TRANSIT,
    # Administrative events, not exceptions: the parcel is still moving
    # normally through the network. Mapping either to `problem` would fire
    # exception notifications for a routine — and in the second case
    # successful — request the user very likely made themselves.
    "cms.stapp.addressChangeRequested": ParcelStatus.IN_TRANSIT,
    "cms.stapp.addressChangeRequestCompleted": ParcelStatus.IN_TRANSIT,
    # Dropping a parcel *off* at an Access Point is the shipper's side of the
    # journey, so UPS now has it: in_transit. The recipient-collection side
    # has its own keys (deliveredToUAP, delToUPSLoc, pkgReadyForPickupAP),
    # which is what makes this the likely reading. Lower confidence than the
    # rest of this block — but in_transit is the safe direction to be wrong
    # in, since at_pickup_point would tell the user to collect a parcel that
    # has not arrived, and would drop polling to the cold tier.
    "cms.stapp.dropoffAccessPoint": ParcelStatus.IN_TRANSIT,
    "cms.stapp.outForDelivery": ParcelStatus.OUT_FOR_DELIVERY,
    "cms.stapp.pkgIsReadyForPickup": ParcelStatus.AT_PICKUP_POINT,
    "cms.stapp.deliveredToUAP": ParcelStatus.AT_PICKUP_POINT,
    "cms.stapp.pkgReadyForPickupAP": ParcelStatus.AT_PICKUP_POINT,
    "cms.stapp.pkgIsAvailForPickup": ParcelStatus.AT_PICKUP_POINT,
    "cms.stapp.awaitingCustomerpickup": ParcelStatus.AT_PICKUP_POINT,
    "cms.stapp.delToUPSLoc": ParcelStatus.AT_PICKUP_POINT,
    "cms.stapp.delivered": ParcelStatus.DELIVERED,
    "cms.stapp.pkgIsDel": ParcelStatus.DELIVERED,
    "cms.stapp.shipmentIsDelivered": ParcelStatus.DELIVERED,
    "cms.stapp.customerPickUp": ParcelStatus.DELIVERED,
    "cms.stapp.rfidConfirmedPickUp": ParcelStatus.DELIVERED,
    "cms.stapp.uspsTrackingDeliveredMsg1": ParcelStatus.DELIVERED,
    "cms.stapp.uspsDeliveredMsg": ParcelStatus.DELIVERED,
    "cms.stapp.returned": ParcelStatus.RETURNING,
    "cms.stapp.retnToSender": ParcelStatus.RETURNING,
    "cms.stapp.deliveryRefused": ParcelStatus.RETURNING,
    "cms.stapp.packageHalted": ParcelStatus.PROBLEM,
    "cms.stapp.investigationOpened": ParcelStatus.PROBLEM,
    "cms.stapp.investigationClosed": ParcelStatus.PROBLEM,
    "cms.stapp.delAttpted": ParcelStatus.PROBLEM,
    "cms.stapp.fnlDelAttpted": ParcelStatus.PROBLEM,
    "cms.stapp.secondryDeliveryAttempted": ParcelStatus.PROBLEM,
    "cms.stapp.postOfficeAttemptedDelivery": ParcelStatus.PROBLEM,
    "cms.stapp.finalDeliveryAttempt": ParcelStatus.PROBLEM,
    "cms.stapp.pkpUpAttempted": ParcelStatus.PROBLEM,
    "cms.stapp.secondPkpUpAttempted": ParcelStatus.PROBLEM,
    "cms.stapp.finalPkpUpAttempted": ParcelStatus.PROBLEM,
    "cms.stapp.pkpUpCanceled": ParcelStatus.PROBLEM,
    "cms.stapp.shipmentVoided": ParcelStatus.PROBLEM,
    "cms.stapp.incompleteDocumentationReceived": ParcelStatus.PROBLEM,
    "cms.stapp.shipmentClearencePending": ParcelStatus.PROBLEM,
}

# Most scans carry no milestoneName at all — 34 of 37 on a real parcel — so
# history entries mapped on nameKey alone came out empty. `actCode` is the only
# other status-bearing field on a scan, and UPS pairs each code with a fixed
# `activityScan` sentence; that pairing is the source for this map.
#
# Do **not** extend it from the public UPS status-code table (500+ two-letter
# codes, UPS Developer Kit, mirrored by RocketShipIt). That is a different
# vocabulary sharing this one's shape: it reads DP as an invoice-piece mismatch
# and AR as a refused delivery, where the scans themselves say "Departed from
# Facility" and "Arrived at Facility". Only its customs codes line up, which on
# an international parcel is exactly the coincidence that makes it look usable.
#
# One code can carry several alternative sentences, and a code is mapped on
# what emitting it at all implies rather than on which sentence came back. A
# code UPS reaches for only around an exception is `problem` even where the
# sentence reports the resolution: the parcel hit friction, and hiding the
# resolved half leaves a history where a lone unresolved entry reads as coming
# out of nowhere. What stays ``None`` is a code whose sentences say nothing
# about the parcel's state at all — a renumbering, a request someone has made,
# an act described as intended rather than done. ``None`` means known and
# deliberately unmapped, and stays **silent**: sending a user to open an issue
# about a code we chose not to map wastes their time. A code absent from the
# map entirely is one we have never seen, and warns.
#
# Mapping a scan is safe in a way that mapping the parcel's own status is not:
# a history entry's status is display-only — nothing derives the parcel's
# status, ``delivered`` or ``delivered_at`` from it.
_ACTIVITY_CODE_MAP: dict[str, ParcelStatus | None] = {
    "MP": ParcelStatus.REGISTERED,
    # Movement, customs progress and administrative address changes alike: the
    # parcel is in the network and nothing is being asked of anyone. The
    # address-change codes are deliberately not `problem`, matching the
    # nameKey map — an exception notification for a change the user very
    # likely requested themselves is worse than silence.
    "PU": ParcelStatus.IN_TRANSIT,
    "OR": ParcelStatus.IN_TRANSIT,
    "AR": ParcelStatus.IN_TRANSIT,
    "SS": ParcelStatus.IN_TRANSIT,
    "DP": ParcelStatus.IN_TRANSIT,
    "WH": ParcelStatus.IN_TRANSIT,
    "DA": ParcelStatus.IN_TRANSIT,
    "HL": ParcelStatus.IN_TRANSIT,
    "HM": ParcelStatus.IN_TRANSIT,
    "YP": ParcelStatus.IN_TRANSIT,
    "DS": ParcelStatus.IN_TRANSIT,
    "EP": ParcelStatus.IN_TRANSIT,
    "IP": ParcelStatus.IN_TRANSIT,
    "XD": ParcelStatus.IN_TRANSIT,
    "77": ParcelStatus.IN_TRANSIT,
    "1C": ParcelStatus.IN_TRANSIT,
    "1D": ParcelStatus.IN_TRANSIT,
    "VK": ParcelStatus.IN_TRANSIT,
    "H5": ParcelStatus.IN_TRANSIT,
    "O1": ParcelStatus.IN_TRANSIT,
    "VM": ParcelStatus.IN_TRANSIT,
    "OT": ParcelStatus.OUT_FOR_DELIVERY,
    "FS": ParcelStatus.DELIVERED,
    "KB": ParcelStatus.DELIVERED,
    # A refusal names its own return leg in the same sentence.
    "AM": ParcelStatus.RETURNING,
    "AQ": ParcelStatus.RETURNING,
    "KR": ParcelStatus.RETURNING,
    "UA": ParcelStatus.RETURNING,
    # Something is genuinely stuck, or someone must act before it moves.
    "48": ParcelStatus.PROBLEM,
    "49": ParcelStatus.PROBLEM,
    "9F": ParcelStatus.PROBLEM,
    "C5": ParcelStatus.PROBLEM,
    "7F": ParcelStatus.PROBLEM,
    "BH": ParcelStatus.PROBLEM,
    "SL": ParcelStatus.PROBLEM,
    "BE": ParcelStatus.PROBLEM,
    "FF": ParcelStatus.PROBLEM,
    "UP": ParcelStatus.PROBLEM,
    # A code UPS only ever emits around an exception stays `problem` even when
    # its sentence reports the resolution: something went wrong, and a history
    # that hides the resolved ones makes the remaining entry look isolated.
    # "A hold on your package has been resolved" is the sole text behind Z1 and
    # the only one ever observed for B3, and it is XV's first variant — reading
    # the same sentence two ways depending on the code would be indefensible.
    "MF": ParcelStatus.PROBLEM,
    "H1": ParcelStatus.PROBLEM,
    "R4": ParcelStatus.PROBLEM,
    "XV": ParcelStatus.PROBLEM,
    "XN": ParcelStatus.PROBLEM,
    "B3": ParcelStatus.PROBLEM,
    "DB": ParcelStatus.PROBLEM,
    "Z1": ParcelStatus.PROBLEM,
    # Known, deliberately unmapped: these describe an intention, a request or
    # a renumbering rather than a state the parcel is in.
    "CG": None,
    "S7": None,
    "ZO": None,
    "EU": None,
}


# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised UPS status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def _warn_unmapped_activity(code: str) -> None:
    """Log an unmapped scan code once, naming the field it came from.

    Separate from the status warning so a report says which vocabulary the
    code belongs to — the two are not interchangeable.
    """
    key = f"actCode={code}"
    if key in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(key)
    _LOGGER.warning(
        "Unrecognised UPS scan code — help us map it. Open an issue "
        "and paste this line: %s\n  actCode=%s → history entry reported "
        "without a status",
        NEW_ISSUE_URL,
        code,
    )


def map_activity_status(code: str | None) -> ParcelStatus | None:
    """Map a scan's ``actCode`` to a canonical status, or ``None``.

    Only reached for a scan carrying no ``milestoneName``; the milestone is
    always the better source when it is there.
    """
    if not code:
        return None
    if code in _ACTIVITY_CODE_MAP:
        return _ACTIVITY_CODE_MAP[code]
    _warn_unmapped_activity(code)
    return None


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


# Distinct (requested, echoed) tracking-number pairs already warned about, so
# a barcode UPS echoes differently from what the user typed doesn't flood the
# log on every poll.
_tracking_number_mismatches_logged: set[tuple[str, str]] = set()


def _warn_tracking_number_mismatch(requested: str, echoed: str) -> None:
    """One-shot warning when UPS echoes a different tracking number back."""
    pair = (requested, echoed)
    if pair in _tracking_number_mismatches_logged:
        return
    _tracking_number_mismatches_logged.add(pair)
    _LOGGER.warning(
        "UPS echoed a different tracking number (%s) than requested (%s) — "
        "keeping the value you configured",
        echoed,
        requested,
    )


def _current_milestone_key(raw: dict) -> str | None:
    """Return the raw status key for ``raw_status``/``status``.

    Source order: ``currentMilestone.nameKey``, falling back to the last
    ``milestones[]`` entry with ``isCurrent: true``, then to the newest
    activity's ``milestoneName.nameKey`` (``shipmentProgressActivities[]`` is
    newest-first, so that is index 0).
    """
    current = raw.get("currentMilestone")
    if isinstance(current, dict):
        key = current.get("nameKey")
        if key:
            return key

    for milestone in raw.get("milestones") or []:
        if isinstance(milestone, dict) and milestone.get("isCurrent"):
            key = milestone.get("nameKey")
            if key:
                return key

    activities = raw.get("shipmentProgressActivities") or []
    if activities and isinstance(activities[0], dict):
        milestone_name = activities[0].get("milestoneName")
        if isinstance(milestone_name, dict):
            return milestone_name.get("nameKey")

    return None


def _gmt_timestamp(entry: dict) -> str | None:
    """Build an ISO 8601 UTC timestamp from an activity's GMT triplet.

    Uses ``gmtDate``/``gmtTime`` (both UTC), never the localised ``date``/
    ``time`` strings — those are locale-formatted (``DD/MM/YYYY``) and
    ambiguous to parse generically.
    """
    gmt_date = entry.get("gmtDate")
    gmt_time = entry.get("gmtTime")
    if not gmt_date or not gmt_time:
        return None
    try:
        parsed = datetime.strptime(f"{gmt_date} {gmt_time}", "%Y%m%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _delivered_at(raw: dict, delivered: bool) -> str | None:
    """Timestamp of the scan that ended the journey, or ``None``.

    ``None`` whenever ``isDelivered`` is false — a delivered-looking scan on
    an otherwise-active parcel (a partial/multi-package shipment) must not
    leak a delivered_at onto a parcel this integration still reports active.

    ``isDelivered`` marks more endings than ``cms.stapp.delivered`` does: a
    parcel handed back to the shipper carries ``packageStatus: "Returned to
    Sender"`` with ``isDelivered: true`` and no delivered milestone anywhere in
    its timeline (observed live 2026-09-08). Keying only on that milestone left
    such a parcel with a null ``delivered_at``, which the days-based retention
    filter keeps rather than drops — so it sat in the delivered sensor forever,
    unsorted. The newest scan is when the ending happened, whatever UPS called
    it.
    """
    if not delivered:
        return None
    newest: str | None = None
    for activity in raw.get("shipmentProgressActivities") or []:
        if not isinstance(activity, dict):
            continue
        timestamp = _gmt_timestamp(activity)
        if not timestamp:
            continue
        milestone_name = activity.get("milestoneName")
        if (
            isinstance(milestone_name, dict)
            and milestone_name.get("nameKey") == "cms.stapp.delivered"
        ):
            return timestamp
        if newest is None or timestamp > newest:
            newest = timestamp
    return newest


def _weight_kg(raw: dict) -> float | None:
    """Convert ``additionalInformation.weight`` to kilograms, or ``None``.

    ``weight`` is ``""`` when absent — the only value observed so far.
    When present, ``weightUnit`` is expected to be ``KGS`` or ``LBS``
    (unverified — no populated sample exists yet); an unrecognised unit
    publishes ``None`` rather than guessing which one it is.
    """
    info = raw.get("additionalInformation")
    if not isinstance(info, dict):
        return None
    weight_raw = info.get("weight")
    if not weight_raw:
        return None
    try:
        value = float(weight_raw)
    except (TypeError, ValueError):
        return None
    unit = str(info.get("weightUnit") or "").upper()
    if unit == "KGS":
        return value
    if unit == "LBS":
        return value * 0.45359237
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from ``shipmentProgressActivities``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``.

    ``raw_status`` carries the activity's ``milestoneName.nameKey`` where there
    is one, and its ``actCode`` otherwise. Two vocabularies in one field is a
    deliberate trade: most scans carry no milestone at all, so keying on the
    nameKey alone left the great majority of entries with both fields empty.
    A consumer telling them apart can: nameKeys are ``cms.stapp.*``, scan codes
    are two characters. An entry with neither is still kept, never dropped.

    The API returns activities newest-first; sorting by the parsed GMT
    timestamp below produces oldest→newest regardless of input order, capped
    to the most recent ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = _gmt_timestamp(event)
        if not timestamp:
            continue
        milestone_name = event.get("milestoneName")
        raw_status = (
            milestone_name.get("nameKey") if isinstance(milestone_name, dict) else None
        )
        if raw_status:
            status = map_event_status(raw_status)
        else:
            act_code = event.get("actCode") or None
            status = map_activity_status(act_code)
            raw_status = act_code
        entry = {
            "timestamp": timestamp,
            "status": status,
            "raw_status": raw_status,
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None, *, locale: str = DEFAULT_LOCALE) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(locale=locale, tracking_code=tracking_code)

# UPS answers with ~126 top-level fields, most of them the track page's own
# furniture: MyChoice upsell links, CMS keys, show/disable flags, notification
# widgets. Publishing them whole pushed a single status-change event past the
# recorder's 32 KB event-data limit, so it refused to store it. This is the
# subset that could plausibly drive an automation. The two heaviest fields are
# deliberately out: shipmentProgressActivities is already published as
# ``history``, and the image blobs would put us straight back over the limit.
PUBLISHED_RAW_FIELDS = frozenset(
    {
        "requestedTrackingNumber",
        "trackingNumber",
        "trackingNumberType",
        "senderShipperNumber",
        "returnNumbers",
        "additionalPackagesCount",
        "packageStatus",
        "packageStatusCode",
        "packageStatusType",
        "packageStatusTime",
        "currentMilestone",
        "milestones",
        "simplifiedText",
        "sddMsg",
        "whatsNextText",
        "trackHistoryDescription",
        "nextExpectedEvent",
        "sdd",
        "sdt",
        "sdst",
        "scheduledDeliveryDateDetail",
        "deliveredDateDetail",
        "deliveryAttemptMsgDate",
        "isDelivered",
        "receivedBy",
        "leftAt",
        "leaveAt",
        "proofOfDeliveryUrl",
        "shipToAddress",
        "shipFromAddress",
        "deliveryAddress",
        "consigneeAddress",
        "upsAccessPoint",
        "isCommercialAddress",
        "receiverStopType",
        "shipmentGMTInfo",
        "additionalInformation",
        "attentionNeeded",
        "asrInformation",
        "specialInstructions",
        "flightInformation",
        "voyageInformation",
        "hasBrokerageEvent",
        "alertCount",
        "isInFlight",
        "isDeliveredToUAP",
        "isPickedUpByCustomer",
        "isDelayedPackge",
        "isFraudIntercept",
        "isSmartPackage",
        "isBasicOrSurepost",
        "isUpgradedSurepost",
        "isManifest",
        "isEDW",
        "isUpsPremierPackage",
        "isUPSDeliveryPartner",
        "errorCode",
        "errorText",
    }
)


def published_raw(raw: dict) -> dict:
    """Return the publishable subset of a ``trackDetails[]`` entry."""
    return {key: value for key, value in raw.items() if key in PUBLISHED_RAW_FIELDS}


def normalize_parcel(
    raw: dict, *, include_history: bool = False, locale: str = DEFAULT_LOCALE
) -> dict:
    """Return a carrier-agnostic parcel dict with the carrier payload under ``raw``.

    The source is one ``trackDetails[]`` entry from ``Track/GetStatus``, or
    the coordinator's pending placeholder (``{"requestedTrackingNumber":
    code}``) for a tracked code that has not resolved yet. Unlike the rest of
    the suite, ``raw`` is not the whole entry but its
    ``PUBLISHED_RAW_FIELDS`` subset; the coordinator caches the full entry, so
    nothing here narrows what a later release can publish or what
    ``history`` is built from. ``diagnostics.py``'s ``TO_REDACT`` is still
    what keeps address/signature/token fields out of a shared dump — this
    subset is about size, not secrecy.

    ``planned_from``/``planned_to``, ``pickup_point`` and ``dimensions`` stay
    at their empty defaults — no confirmed field exists yet for any of them
    (an in-flight ETA, an access-point object, or any dimension field at
    all). ``pickup`` is the exception: it follows the mapped status, which
    does know when a parcel is waiting to be collected. ``sender``/``receiver`` stay ``None``: the only
    candidate fields are a recipient's address and an internal UPS account
    number, neither a name fit to publish as either.
    """
    raw_status = _current_milestone_key(raw)
    status = map_parcel_status(raw_status)
    delivered = bool(raw.get("isDelivered"))

    requested_code = raw.get("requestedTrackingNumber")
    echoed_code = raw.get("trackingNumber")
    if requested_code and echoed_code and requested_code != echoed_code:
        _warn_tracking_number_mismatch(requested_code, echoed_code)
    tracking_code = requested_code or echoed_code

    activities = raw.get("shipmentProgressActivities") or []

    return {
        "carrier": "UPS",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": _delivered_at(raw, delivered),
        "planned_from": None,
        "planned_to": None,
        # Derived from the status, not from a field: no upsAccessPoint object
        # has ever been seen populated, so the location stays None — but a
        # parcel whose milestone says it is waiting to be collected must not
        # publish pickup False alongside it.
        "pickup": status == ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": None,
        "url": tracking_url(tracking_code, locale=locale),
        "weight": _weight_kg(raw),
        "dimensions": None,
        "history": build_history(activities) if include_history else None,
        "raw": published_raw(raw),
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
