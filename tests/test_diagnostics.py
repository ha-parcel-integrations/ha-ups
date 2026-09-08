"""Tests for UPS diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.ups.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "1Z999AA10123456784"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.consecutive_failures = 0
    entry.runtime_data.coordinator.seconds_since_last_fetch = 12.3
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "1Z999AA10123456784",
            "sender": None,
            "receiver": None,
            "status": "out_for_delivery",
            "raw": {
                "trackingNumber": "1Z999AA10123456784",
                "shipToAddress": {"city": "Rotterdam", "street": "Coolsingel 1"},
                "deliveryAddress": {"city": "Rotterdam", "zipCode": "3011AB"},
                "receivedBy": "J. Doe",
                "signatureImage": "base64...",
                "senderShipperNumber": "ABC123",
                "packageStatusType": "I",
                "packageStatusCode": "OT",
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {"incoming_active": 1, "delivered": 0}
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
        "consecutive_failed_cycles": 0,
        "seconds_since_last_fetch": 12.3,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["trackingNumber"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["shipToAddress"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["deliveryAddress"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["receivedBy"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["signatureImage"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["senderShipperNumber"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"
    assert result["incoming"][0]["raw"]["packageStatusType"] == "I"
    assert result["incoming"][0]["raw"]["packageStatusCode"] == "OT"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (the account-less full stop) must be visible,
    not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.consecutive_failures = 0
    entry.runtime_data.coordinator.seconds_since_last_fetch = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
        "consecutive_failed_cycles": 0,
        "seconds_since_last_fetch": None,
    }


async def test_redaction_preserves_the_key_set(hass):
    """Values get replaced, but no key is ever dropped by redaction."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    raw = {
        "trackingNumber": "1Z999AA10123456784",
        "shipToAddress": {"city": "Rotterdam"},
        "packageStatusType": "D",
    }
    parcel = {"barcode": "1Z999AA10123456784", "raw": raw}
    entry.runtime_data.coordinator.data = [parcel]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert set(result["incoming"][0]) == set(parcel)
    assert set(result["incoming"][0]["raw"]) == set(raw)


def test_to_redact_covers_every_address_and_token_field():
    for field in (
        "shipToAddress",
        "deliveryAddress",
        "receivedBy",
        "leftAt",
        "signatureImage",
        "deliveryPhoto",
        "proofOfDeliveryUrl",
        "senderShipperNumber",
        "myChoiceToken",
        "enrollNum",
        "internalKey",
        "location",
    ):
        assert field in TO_REDACT


async def test_diagnostics_carry_the_fetch_ramp_without_tracking_codes(hass):
    """Rising durations then timeouts is the throttle's signature.

    It is the one thing a report needs and the one thing nobody has debug
    logging on for, so it has to survive into diagnostics — and it has to do
    so without carrying a tracking code into a public issue.
    """
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.consecutive_failures = 2
    entry.runtime_data.coordinator.seconds_since_last_fetch = 30.0
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.recent_fetches = [
        {"at": "2026-09-05T10:00:00+00:00", "seconds": 1.4, "ok": True, "error": None},
        {"at": "2026-09-05T10:15:00+00:00", "seconds": 9.8, "ok": True, "error": None},
        {
            "at": "2026-09-05T10:30:00+00:00",
            "seconds": 20.0,
            "ok": False,
            "error": "UPS API request failed: request timed out",
        },
    ]

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert [f["seconds"] for f in result["recent_fetches"]] == [1.4, 9.8, 20.0]
    assert "1Z999" not in str(result["recent_fetches"])
