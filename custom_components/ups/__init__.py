"""UPS parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import UPSApiClient
from .const import PLATFORMS, STORAGE_KEY, STORAGE_VERSION
from .coordinator import UPSCoordinator
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class UPSData:
    """Runtime data attached to the UPS config entry."""

    client: UPSApiClient
    coordinator: UPSCoordinator
    session: aiohttp.ClientSession


type UPSConfigEntry = ConfigEntry[UPSData]


async def async_setup_entry(hass: HomeAssistant, entry: UPSConfigEntry) -> bool:
    """Set up UPS from a config entry."""
    # No API key, but a cookie-and-CSRF bootstrap that must survive across
    # refreshes — so, unlike a stateless keyed API, this carrier needs its
    # own session rather than HA's shared one. The connector is still shared
    # (connector_owner=False) to reuse HA's pooled connections; only the
    # cookie jar is isolated.
    session = aiohttp.ClientSession(
        connector=async_get_clientsession(hass).connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
    )
    client = UPSApiClient(session)
    coordinator = UPSCoordinator(hass, client, entry)

    try:
        # Setup issues no HTTP at all. The first refresh used to run inline
        # here, so with the address closed every tracked code burned its full
        # 20 s timeout in sequence — observed blocking entry setup for 88 s on
        # two codes, with HA's own "waiting for integrations" warning, while
        # issuing exactly the burst that keeps the cooldown alive. The
        # ConfigEntryNotReady that followed then put HA on its own far more
        # aggressive retry schedule, and every retry fired more requests into
        # a cooldown only rest clears. Parcels come from the cache, or as
        # placeholders showing unknown, and the first real request waits for
        # the request budget like every other one.
        await coordinator.async_load_cache()
        if coordinator.seed_from_cache():
            _LOGGER.debug(
                "UPS: published %s tracked parcel(s) from cache; the first "
                "request waits for the budget (last fetch %.0fs ago)",
                len(coordinator.data or []),
                coordinator.seconds_since_last_fetch or 0,
            )
    except Exception:
        await session.close()
        raise

    entry.runtime_data = UPSData(client=client, coordinator=coordinator, session=session)

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Every setup path that fails must close the session it opened, or
        # each of HA's retries leaks one — and this one carries the cookie
        # jar, so a leaked session is also a bootstrap that has to be redone.
        await session.close()
        raise

    # Apply option changes (added/removed parcels, history) live via a
    # coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. The update listener does NOT reload, so it does
    # not trip the config-entry-listener deprecation. This is also the resume
    # path after polling fully suspended (Section 2.1): adding a parcel back
    # triggers this refresh, which recomputes the tier and re-arms scheduling.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async_setup_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: UPSConfigEntry
) -> None:
    """Apply changed options by refreshing the coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_remove_entry(hass: HomeAssistant, entry: UPSConfigEntry) -> None:
    """Delete the persisted payload cache when the entry is removed."""
    await Store(
        hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
    ).async_remove()


async def async_unload_entry(hass: HomeAssistant, entry: UPSConfigEntry) -> bool:
    """Unload the UPS config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.session.close()
    # Single-instance integration (single_config_entry), so the services can
    # always go when the entry unloads.
    async_unload_services(hass)
    return True
