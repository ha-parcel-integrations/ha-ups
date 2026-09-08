"""Tests for UPS setup and unload."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ups.api import UPSApiError
from custom_components.ups.const import (
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)

from .payloads import ACTIVE_CODE
from .payloads import active_sample as _sample

OTHER_CODE = "EXAMPLE222222"


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ups.api.UPSApiClient.async_get_parcel",
        new=AsyncMock(return_value=_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # The active parcel produced a per-parcel sensor and the summary sensor.
    incoming = hass.states.get("sensor.ups_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    # Services registered on setup...
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    # ...and removed on unload (single-instance integration).
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_setup_succeeds_even_when_the_carrier_is_unreachable(hass):
    """Setup issues no HTTP at all, so it cannot fail on the carrier.

    It used to: the first refresh ran inline before platforms were forwarded,
    every tracked code burned its full timeout in sequence, and the resulting
    ConfigEntryNotReady put HA on its own far more aggressive retry schedule —
    each retry firing more requests into a cooldown only rest clears. Parcels
    are seeded from cache or as placeholders instead, and the first real
    request waits for the budget.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    fetch = AsyncMock(side_effect=UPSApiError("UPS unreachable"))
    with patch("custom_components.ups.api.UPSApiClient.async_get_parcel", new=fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    fetch.assert_not_called()


async def test_a_failed_platform_forward_closes_the_session(hass):
    """This entry owns its session, so every failing setup path has to close
    it — otherwise each of HA's retries leaks one, and with it the cookie jar
    a bootstrap had already paid for."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        side_effect=RuntimeError("boom"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.runtime_data.session.closed


async def test_setup_without_parcels_still_loads(hass):
    """Nothing tracked means nothing to seed — and still no HTTP."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, options={CONF_PARCELS: []}
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.ups.api.UPSApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
        )

        # The next poll returns a different tracking code: the summary sensor
        # spawns a new per-parcel sensor and removes the stale one.
        mock.return_value = _sample(OTHER_CODE)
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{OTHER_CODE}"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
            )
            is None
        )


async def test_options_update_applies_live_without_reload(hass):
    """Adding a parcel via options refreshes the coordinator immediately."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.ups.api.UPSApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        mock.side_effect = lambda code, **kwargs: _sample(code)
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
        await hass.async_block_till_done()

    incoming = hass.states.get("sensor.ups_incoming_parcels")
    assert incoming.state == "2"
