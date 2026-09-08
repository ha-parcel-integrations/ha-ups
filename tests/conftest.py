"""pytest configuration for the UPS test suite."""
import sys

import pytest
from pytest_homeassistant_custom_component.plugins import hass  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make ``custom_components.ups`` loadable from config-flow / setup tests."""
    yield


@pytest.fixture(autouse=True)
def unlimited_budget():
    """Take the request budget out of the way for most tests.

    In production a cycle spends one request and the next is an hour away, so
    a test that drives ``_async_update_data`` a few times in a row would
    otherwise run dry and assert nothing. The tests that are *about* the
    budget opt back in via ``real_budget``.
    """
    from custom_components.ups import coordinator

    original = (
        coordinator.REQUEST_BUDGET_CAPACITY,
        coordinator.REQUEST_BUDGET_REFILL_SECONDS,
    )
    coordinator.REQUEST_BUDGET_CAPACITY = 10_000
    coordinator.REQUEST_BUDGET_REFILL_SECONDS = 1
    yield
    (
        coordinator.REQUEST_BUDGET_CAPACITY,
        coordinator.REQUEST_BUDGET_REFILL_SECONDS,
    ) = original


@pytest.fixture
def real_budget(unlimited_budget):
    """Restore the real budget for the tests that exercise it."""
    from custom_components.ups import const, coordinator

    coordinator.REQUEST_BUDGET_CAPACITY = const.REQUEST_BUDGET_CAPACITY
    coordinator.REQUEST_BUDGET_REFILL_SECONDS = const.REQUEST_BUDGET_REFILL_SECONDS
    yield const.REQUEST_BUDGET_CAPACITY


@pytest.fixture(autouse=True)
def reset_one_shot_warnings():
    """Clear the "already warned this session" set between tests.

    ``parcels._unmapped_statuses_logged`` is module-level, so without this a
    test's one-shot-warning assertion would depend on which test already
    triggered that status code first.
    """
    from custom_components.ups import parcels

    parcels._unmapped_statuses_logged.clear()
    yield


if sys.platform == "win32":
    # pytest-homeassistant-custom-component blocks socket *creation*
    # (``disable_socket(allow_unix_socket=True)``) in its per-test setup hook.
    # That is fine on Linux, where asyncio's self-pipe is an AF_UNIX
    # socketpair — but Windows event loops build theirs from AF_INET sockets,
    # so every async test dies with ``SocketBlockedError`` while the event
    # loop fixture is being created. Neutralise the creation block on Windows
    # and keep the network guard as the plugin's connect-time allowlist
    # (``socket_allow_hosts(["127.0.0.1"])``, applied right before the
    # ``disable_socket`` call we swallow here).
    import pytest_socket

    pytest_socket.disable_socket = lambda allow_unix_socket=False: None

    # HA's aiohttp helper hardcodes aiohttp's AsyncResolver, whose aiodns
    # backend refuses the Proactor loop the suite runs on under Windows.
    # Swap in the threaded resolver for the tests — no test resolves DNS.
    import homeassistant.helpers.aiohttp_client as _ha_aiohttp_client
    from aiohttp.resolver import ThreadedResolver

    _ha_aiohttp_client.AsyncResolver = ThreadedResolver
