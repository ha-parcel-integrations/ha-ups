"""Diagnostics support for the UPS parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import UPSConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
# ``raw`` carries the full API response verbatim (parcels.py's
# normalize_parcel), so every address/recipient/token field UPS returns
# passes through it. Do NOT redact nameKey, actCode, packageStatusType/Code,
# the GMT timestamp triplet or serviceName — they carry no personal data and
# are exactly what a status-mapping bug report needs.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    # the tracking number, however UPS's own payload names it
    "trackingNumber",
    "requestedTrackingNumber",
    # address / recipient / proof-of-delivery fields
    "shipToAddress",
    "deliveryAddress",
    "receivedBy",
    "leftAt",
    "signatureImage",
    "deliveryPhoto",
    "proofOfDeliveryUrl",
    # UPS account / session identifiers — never a status field
    "senderShipperNumber",
    "myChoiceToken",
    "enrollNum",
    "internalKey",
    # a depot name is harmless, but a final-scan location has never been
    # proven not to carry a street address
    "location",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UPSConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the UPS config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
            # The two numbers a throttle report is unreadable without: how
            # long the coordinator has been backing off, and whether the
            # fetch floor is currently suppressing refreshes.
            "consecutive_failed_cycles": coordinator.consecutive_failures,
            "seconds_since_last_fetch": coordinator.seconds_since_last_fetch,
        },
        # Per-request durations and outcomes, oldest first. Rising durations
        # followed by timeouts is the throttle's signature, and this is the
        # only place it can be read back after the fact — nobody has debug
        # logging on before the problem happens. Carries no tracking codes.
        "recent_fetches": coordinator.recent_fetches,
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
