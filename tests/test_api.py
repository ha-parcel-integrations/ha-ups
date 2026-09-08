"""Tests for the UPS API client."""
import asyncio
from http.cookies import Morsel
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ups.api import (
    GUARD_HEADERS,
    UPSApiClient,
    UPSApiError,
)

CODE = "1Z999AA10123456784"


def _cookie(name: str, value: str) -> Morsel:
    morsel = Morsel()
    morsel.set(name, value, value)
    return morsel


class _FakeCookieJar:
    """A minimal stand-in for aiohttp's CookieJar the client iterates."""

    def __init__(self, cookies: list[Morsel] | None = None) -> None:
        self._cookies = cookies or []

    def __iter__(self):
        return iter(self._cookies)


def _ctx(response: AsyncMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _response(
    status: int,
    body: object = None,
    *,
    raise_on_json=None,
    set_cookie: list[str] | None = None,
) -> AsyncMock:
    response = AsyncMock()
    response.status = status
    response.read = AsyncMock(return_value=b"")
    response.content_length = 19721
    response.headers = MagicMock()
    response.headers.getall = MagicMock(return_value=set_cookie or [])
    # None exercises the "handshake unknown" branch; a test that cares about
    # the negotiated cipher builds its own.
    response.connection = None
    if raise_on_json is not None:
        response.json = AsyncMock(side_effect=raise_on_json)
    else:
        response.json = AsyncMock(return_value=body)
    return response


def _session(
    *,
    get_response: AsyncMock | None = None,
    post_responses: list[AsyncMock] | None = None,
    cookies: list[Morsel] | None = None,
) -> MagicMock:
    session = MagicMock()
    session.cookie_jar = _FakeCookieJar(cookies)
    session.get = MagicMock(return_value=_ctx(get_response or _response(200)))
    post_iter = iter(post_responses or [])
    session.post = MagicMock(side_effect=lambda *a, **k: _ctx(next(post_iter)))
    return session


def _envelope(detail: dict) -> dict:
    return {"statusCode": 200, "trackDetails": [detail]}


# ---------------------------------------------------------------------------
# happy path / bootstrap
# ---------------------------------------------------------------------------


async def test_get_parcel_bootstraps_then_tracks():
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[_response(200, _envelope({"requestedTrackingNumber": CODE}))],
    )
    client = UPSApiClient(session)

    parcel = await client.async_get_parcel(CODE)

    assert parcel["requestedTrackingNumber"] == CODE
    session.get.assert_called_once()  # bootstrap
    session.post.assert_called_once()
    # the CSRF cookie value is copied verbatim into the request header
    assert session.post.call_args.kwargs["headers"]["X-XSRF-TOKEN"] == "csrf-value"


async def test_bootstrap_only_runs_once_across_calls():
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[
            _response(200, _envelope({"requestedTrackingNumber": CODE})),
            _response(200, _envelope({"requestedTrackingNumber": CODE})),
        ],
    )
    client = UPSApiClient(session)

    await client.async_get_parcel(CODE)
    await client.async_get_parcel(CODE)

    session.get.assert_called_once()


async def test_all_four_guard_headers_are_sent():
    """Dropping any one of these makes every lookup hang silently."""
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[_response(200, _envelope({"requestedTrackingNumber": CODE}))],
    )
    client = UPSApiClient(session)

    await client.async_get_parcel(CODE)

    for header in ("Accept-Encoding", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"):
        assert header in GUARD_HEADERS
        assert session.post.call_args.kwargs["headers"][header] == GUARD_HEADERS[header]
        assert session.get.call_args.kwargs["headers"][header] == GUARD_HEADERS[header]


async def test_no_authorization_header_is_ever_sent():
    """This is a keyless endpoint — a stray Authorization header would be a bug."""
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[_response(200, _envelope({"requestedTrackingNumber": CODE}))],
    )
    client = UPSApiClient(session)

    await client.async_get_parcel(CODE)

    assert "Authorization" not in session.post.call_args.kwargs["headers"]
    assert "Authorization" not in session.get.call_args.kwargs["headers"]


# ---------------------------------------------------------------------------
# not-found / error envelopes
# ---------------------------------------------------------------------------


async def test_error_code_504_is_not_found():
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[
            _response(
                200,
                _envelope(
                    {
                        "requestedTrackingNumber": "NOTATRACKINGNUMBER",
                        "errorCode": "504",
                        "errorText": "Tracking number not found in database",
                    }
                ),
            )
        ],
    )
    client = UPSApiClient(session)

    assert await client.async_get_parcel("NOTATRACKINGNUMBER") is None


async def test_other_error_code_raises():
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[
            _response(200, _envelope({"errorCode": "500", "errorText": "Server error"}))
        ],
    )
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)


async def test_missing_track_details_raises():
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[_response(200, {"statusCode": 200, "trackDetails": []})],
    )
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)


async def test_non_object_body_raises():
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[_response(200, ["not", "a", "dict"])],
    )
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)


async def test_html_body_raises_not_hangs():
    """A 200 whose body is HTML (the generic SPA shell) must raise cleanly."""
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[
            _response(200, raise_on_json=ValueError("Expected JSON, got HTML"))
        ],
    )
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)


# ---------------------------------------------------------------------------
# 401 / re-bootstrap and timeout handling
# ---------------------------------------------------------------------------


async def test_401_rebootstraps_once_then_succeeds():
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[
            _response(401),
            _response(200, _envelope({"requestedTrackingNumber": CODE})),
        ],
    )
    client = UPSApiClient(session)

    parcel = await client.async_get_parcel(CODE)

    assert parcel["requestedTrackingNumber"] == CODE
    assert session.get.call_count == 2  # initial bootstrap + re-bootstrap
    assert session.post.call_count == 2


async def test_401_twice_raises():
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[_response(401), _response(401)],
    )
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)


async def test_a_rebootstrap_that_loses_the_cookie_sends_nothing():
    """The retry earns the same guard as the first attempt.

    A re-bootstrap can come back challenged — that is the whole point of the
    challenge being client-dependent — and it then seeds no CSRF cookie.
    Posting anyway spends a second request on a certain 401, which is exactly
    the three-requests-for-a-wrong-diagnosis loop the first guard removed.
    """
    jar = _FakeCookieJar([_cookie("X-XSRF-TOKEN-ST", "csrf-value")])
    session = MagicMock()
    session.cookie_jar = jar

    def _bootstrap(*args, **kwargs):
        # The first bootstrap works; the re-bootstrap is served the challenge
        # page instead — 200, and no application cookie.
        if session.get.call_count > 1:
            jar._cookies = []
        return _ctx(_response(200))

    session.get = MagicMock(side_effect=_bootstrap)
    session.post = MagicMock(return_value=_ctx(_response(401)))
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError) as excinfo:
        await client.async_get_parcel(CODE)

    assert "X-XSRF-TOKEN-ST" in str(excinfo.value)
    assert session.post.call_count == 1  # the 401, and nothing after it


async def test_bootstrap_timeout_is_transient_not_not_found():
    """The header guard's failure mode is a hang, never a 404-style answer."""
    session = MagicMock()
    session.cookie_jar = _FakeCookieJar()
    session.get = MagicMock(side_effect=asyncio.TimeoutError)
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)


async def test_track_timeout_is_transient_not_not_found():
    session = MagicMock()
    session.cookie_jar = _FakeCookieJar([_cookie("X-XSRF-TOKEN-ST", "csrf-value")])
    session.get = MagicMock(return_value=_ctx(_response(200)))
    session.post = MagicMock(side_effect=asyncio.TimeoutError)
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)


async def test_bootstrap_non_200_raises():
    session = MagicMock()
    session.cookie_jar = _FakeCookieJar()
    session.get = MagicMock(return_value=_ctx(_response(500)))
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)


async def test_track_non_200_non_401_raises():
    session = _session(
        cookies=[_cookie("X-XSRF-TOKEN-ST", "csrf-value")],
        post_responses=[_response(500)],
    )
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)


async def test_a_bootstrap_without_the_csrf_cookie_refuses_to_send():
    """Observed live: a 200 bootstrap that seeded no CSRF cookie.

    The POST then went out header-less, earned a certain 401, triggered a
    re-bootstrap, 401'd again and was reported as a session expiry. Three
    requests spent on a diagnosis that was wrong, out of an allowance of a few
    an hour. Name the real cause and send nothing.
    """
    session = _session(
        cookies=[],
        post_responses=[_response(200, _envelope({"requestedTrackingNumber": CODE}))],
    )
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError) as excinfo:
        await client.async_get_parcel(CODE)

    assert "X-XSRF-TOKEN-ST" in str(excinfo.value)
    session.post.assert_not_called()


async def test_a_bootstrap_without_the_csrf_cookie_is_retried_next_time():
    """A challenge page is not a completed bootstrap.

    Marking it done meant every later lookup skipped the GET entirely and
    raised on the missing cookie — costing the caller a token for a cycle in
    which nothing left the machine, and never asking again for the real page.
    """
    session = _session(cookies=[], post_responses=[])
    client = UPSApiClient(session)

    for _ in range(2):
        with pytest.raises(UPSApiError):
            await client.async_get_parcel(CODE)

    assert session.get.call_count == 2
    session.post.assert_not_called()


async def test_a_refusal_names_the_handshake_it_came_back_over(caplog):
    """The last hypothesis left is this host's ClientHello, so log it."""
    response = _response(200)
    ssl_object = MagicMock()
    ssl_object.version = MagicMock(return_value="TLSv1.3")
    ssl_object.cipher = MagicMock(return_value=("TLS_AES_256_GCM_SHA384", "", 256))
    response.connection = MagicMock()
    response.connection.transport.get_extra_info = MagicMock(
        side_effect=lambda name: ssl_object if name == "ssl_object" else ("23.1.2.3", 443)
    )
    session = _session(cookies=[], get_response=response)
    client = UPSApiClient(session)

    with pytest.raises(UPSApiError):
        await client.async_get_parcel(CODE)

    assert "TLSv1.3/TLS_AES_256_GCM_SHA384 with 23.1.2.3" in caplog.text


def test_the_bootstrap_does_not_use_the_public_track_page():
    """The page refuses some clients outright; the API host does not.

    www.ups.com answered a Home Assistant install with Akamai's sensor
    challenge four times across five hours — a 200 carrying only bot-manager
    cookies, no CSRF cookie, so no lookup could even be attempted — while a
    second machine on the same address and headers was answered normally.
    The same install got honest 401s from the API host in that period, so
    only the page is refusing. Pointing this back at www.ups.com reinstates
    the outage.
    """
    from custom_components.ups.const import BOOTSTRAP_URL, TRACKING_API_URL

    assert BOOTSTRAP_URL.startswith("https://webapis.ups.com/")
    assert "www.ups.com" not in BOOTSTRAP_URL
    # Same host as the POST, so the cookie is seeded by what will consume it.
    assert BOOTSTRAP_URL.split("?")[0] == TRACKING_API_URL.split("?")[0]
