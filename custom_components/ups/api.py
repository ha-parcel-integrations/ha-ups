"""UPS public tracking API client (Track/GetStatus).

No account, no API key. Two calls per lookup:

1. A GET to ``BOOTSTRAP_URL`` collects an Akamai/session cookie jar, including
   the CSRF cookie (``COOKIE_XSRF``). It is aimed at the API host rather than
   the public track page — see ``const.py``; the page refuses some clients
   outright and the API host does not.
2. A POST to ``TRACKING_API_URL`` with that CSRF cookie copied into
   ``HEADER_XSRF`` (a plain double-submit — nothing is derived or signed) and
   :data:`FETCH_HEADERS` returns the tracking payload.

The four Akamai client-hint headers in :data:`GUARD_HEADERS` are the one
thing in this module that is genuinely load-bearing beyond the usual carrier
boilerplate: ``Track/GetStatus`` is behind a guard that does not 403 a
request it dislikes — it never answers at all. All four are required
*together*; dropping any one of them makes every lookup hang until the
client's own timeout, with nothing to log or parse. Do not "clean up" this
header set.

:data:`NAVIGATION_HEADERS` (bootstrap GET) and :data:`FETCH_HEADERS` (track
POST) are deliberately **not** the same dict beyond that shared guard core. A
real browser's top-level navigation to ``www.ups.com/track`` and its
follow-up ``fetch()``/XHR call to ``webapis.ups.com`` carry different
``sec-fetch-*``/``Accept``/``Origin`` shapes — a navigation never carries the
fetch-request shape a real browser does not send on it.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import Any

import aiohttp

from .const import (
    BOOTSTRAP_URL,
    COOKIE_XSRF,
    DEFAULT_LOCALE,
    HEADER_XSRF,
    TRACKING_API_URL,
)

_LOGGER = logging.getLogger(__name__)

# Explicit per-request timeout. The header guard's failure mode is silence,
# not an error status — without a timeout the coordinator would block until
# HA's own, far longer, ceiling. The slowest observed hang from a guard
# failure was ~14 s; this leaves headroom above that without letting one
# stuck parcel stall a whole poll.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Transport health only. This threshold was added on the reading that the
# guard ramps latency before it starts hanging; measurement withdrew it. What
# closes is a per-address request budget on unvalidated sessions, and it
# closes abruptly — 1.87 s and then a timeout, with nothing ahead of it. The
# line still catches genuinely slow transport, but it is never advance
# warning, and nothing should be wired to it as though it were.
SLOW_REQUEST_SECONDS = 8.0

_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# The four headers Akamai's client-hint guard scores *for a Chrome identity*
# (see the module docstring). What it actually checks is agreement with the
# User-Agent, not the presence of these names: a captured Safari session sends
# no client hints at all and is answered normally, while dropping one of these
# while still claiming Chrome makes the request hang. They belong together
# with _CHROME_USER_AGENT and must be changed together or not at all.
# Present on every request, but not sufficient alone to make a request *look*
# like the right kind of request — NAVIGATION_HEADERS and FETCH_HEADERS layer
# the rest of a real browser's shape on top of this.
# The sec-ch-ua version triplet is a live maintenance tripwire: if lookups
# start hanging while GetLookupData (a sibling, unguarded path) still
# answers, bump this triplet and the User-Agent together before suspecting
# anything else.
GUARD_HEADERS = {
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

# The bootstrap GET asks for a document and is answered with one (the SPA
# shell), so it keeps a navigation's shape: no Origin/Referer, and a
# navigate/document/none sec-fetch triplet rather than cors/empty/same-site.
# No browser makes this exact request, so there is no shape to copy — this is
# the one that was measured seeding both application cookies, and it is not a
# guess worth re-rolling.
NAVIGATION_HEADERS = {
    **GUARD_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "User-Agent": _CHROME_USER_AGENT,
}

# The track POST is a same-site XHR/fetch call from the track SPA.
FETCH_HEADERS = {
    **GUARD_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "User-Agent": _CHROME_USER_AGENT,
    "Origin": "https://www.ups.com",
    "Referer": "https://www.ups.com/",
}


def _connection_description(response: aiohttp.ClientResponse) -> str:
    """Name the handshake and the edge this response came back over.

    Two explanations survive for being served a bot-check instead of the
    track page, and this line separates them. The address, the headers, the
    cookie jar and Home Assistant's own SSL context all behave identically on
    a machine that *is* answered, so what is left is either the shape of this
    host's ClientHello — the OpenSSL build decides its key-share groups, and
    the guard scores those — or which edge it reached, since the answering
    node is chosen by whichever resolver this host happens to use. Only this
    host can report either.
    """
    connection = response.connection
    transport = connection.transport if connection is not None else None
    if transport is None:
        return f"an unknown connection ({ssl.OPENSSL_VERSION})"
    peer = transport.get_extra_info("peername")
    where = peer[0] if peer else "?"
    sock = transport.get_extra_info("ssl_object")
    if sock is None:
        return f"an unknown handshake with {where} ({ssl.OPENSSL_VERSION})"
    cipher, _, _ = sock.cipher() or ("?", "", "")
    return f"{sock.version()}/{cipher} with {where} ({ssl.OPENSSL_VERSION})"


def _accept_language(locale: str) -> str:
    """Build an ``Accept-Language`` value consistent with the ``loc`` we send.

    The guard scores how complete and coherent a browser profile looks, so a
    request that asks for ``loc=en_NL`` while its Accept-Language says
    ``en-US`` is exactly the kind of internal mismatch worth not having. A
    real browser in that locale leads with the country variant.
    """
    language, _, country = locale.partition("_")
    if not country:
        return f"{language},{language};q=0.9"
    return f"{language}-{country},{language};q=0.9"


class UPSApiError(Exception):
    """Raised when a UPS tracking request returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
    ) -> None:
        """Store the status code, the ``Retry-After`` header and the cause."""
        super().__init__(f"UPS API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after
        # Set only where the request genuinely never came back. The absence
        # of a status code does not mean the same thing: an unparseable body
        # or a missing trackDetails array also carries none, and those *were*
        # answered — emptying the budget on one throws away hours of
        # allowance because UPS returned something we could not read.
        self.timed_out = timed_out


class UPSApiClient:
    """Client for the account-less UPS ``Track/GetStatus`` endpoint.

    Keeps one cookie jar for the lifetime of the client (see
    ``__init__.py``'s dedicated per-entry session) and bootstraps it once,
    re-running the bootstrap at most once per refresh on a 401. A hung
    request (the header guard's failure mode) is treated as transient — never
    as "not found" — and surfaces as :class:`UPSApiError` with no status code
    so the coordinator's cache keeps the parcel visible.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with a session carrying its own cookie jar."""
        self._session = session
        self._bootstrapped = False
        # Track POSTs actually sent, for the caller's accounting. A lookup is
        # normally one, but a 401 re-bootstrap sends a second for the same
        # parcel, and on an allowance of three per address a request the
        # budget never hears about is the difference between resting and
        # hanging.
        self.requests_made = 0

    async def async_get_parcel(
        self, tracking_code: str, *, locale: str = DEFAULT_LOCALE
    ) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the ``trackDetails[0]`` dict for a known parcel, or ``None``
        when the endpoint reports ``errorCode: "504"`` (tracking number not
        found in database) — a normal, expected state, never an error. Any
        other failure (a hang, a non-200, an HTML body, a persistent 401)
        raises :class:`UPSApiError`; network errors propagate as
        ``aiohttp.ClientError``.
        """
        if not self._bootstrapped:
            await self._async_bootstrap(locale)

        self._require_xsrf()

        try:
            return await self._async_track(tracking_code, locale)
        except UPSApiError as err:
            if err.status_code != 401:
                raise
            # A stale/expired cookie jar — re-bootstrap once per refresh, then
            # let a second failure propagate rather than looping forever.
            _LOGGER.info(
                "UPS session expired (401) on %s; re-bootstrapping. Note this "
                "doubles the request count for this lookup.",
                tracking_code,
            )
            await self._async_bootstrap(locale)
            # The retry earns the same guard as the first attempt. A
            # re-bootstrap that came back challenged seeds no CSRF cookie, and
            # posting anyway spends a second request on a certain 401 — the
            # exact three-requests-for-a-wrong-diagnosis loop the guard exists
            # to stop.
            self._require_xsrf()
            return await self._async_track(tracking_code, locale)

    def _require_xsrf(self) -> None:
        """Refuse to send a request the missing CSRF cookie dooms to a 401.

        On this carrier a wasted request is a real cost — the whole allowance
        is a few per hour — so this fails locally, naming the cause, rather
        than spending one and reporting it as a session expiry.
        """
        if self._xsrf_header():
            return
        raise UPSApiError(
            f"bootstrap returned no {COOKIE_XSRF} cookie, so the CSRF "
            "header cannot be built — not sending a request that would "
            "only be refused",
            status_code=401,
        )

    async def _async_bootstrap(self, locale: str) -> None:
        """GET the API host once to seed the cookie jar (incl. CSRF)."""
        url = BOOTSTRAP_URL.format(locale=locale)
        started = time.monotonic()
        try:
            async with self._session.get(
                url,
                headers={
                    **NAVIGATION_HEADERS,
                    "Accept-Language": _accept_language(locale),
                },
                timeout=REQUEST_TIMEOUT,
            ) as response:
                # Before the body: reading it to the end releases the
                # connection back to the pool, and the handshake goes with it.
                handshake = _connection_description(response)
                body = await response.read()
                elapsed = time.monotonic() - started
                # Cookie *names* only: a bootstrap that returns 200 without
                # seeding the CSRF cookie is indistinguishable from a healthy
                # one in the log otherwise, and that failure was observed live.
                # Values are session credentials and debug logs get pasted
                # into public issues.
                # Both the jar and the raw Set-Cookie names, because they
                # answer different questions: a cookie the server sent but the
                # jar dropped is a client-side parsing difference, one the
                # server never sent is the edge deciding not to. Observed
                # 2026-09-07: production held only the Akamai cookies while
                # this repo's own machine, same address and headers, held
                # those plus X-XSRF-TOKEN-ST and X-CSRF-TOKEN.
                sent = [
                    header.split("=", 1)[0].strip()
                    for header in response.headers.getall("Set-Cookie", [])
                ]
                # len(body), never content_length: the latter is the
                # compressed length, and comparing one against the other's
                # decompressed size once made a healthy page look five times
                # bigger than it was.
                _LOGGER.debug(
                    "UPS bootstrap: %s bytes, Set-Cookie sent %s; jar now "
                    "holds %s",
                    len(body),
                    ", ".join(sorted(sent)) or "(nothing)",
                    ", ".join(sorted(c.key for c in self._session.cookie_jar))
                    or "(nothing)",
                )
                _LOGGER.debug(
                    "UPS bootstrap GET %s -> %s in %.2fs",
                    url,
                    response.status,
                    elapsed,
                )
                if elapsed >= SLOW_REQUEST_SECONDS:
                    _LOGGER.warning(
                        "UPS bootstrap took %.1fs (measured ~0.2s healthy). Slow "
                        "transport, not a warning of the coming close — that "
                        "arrives without one.",
                        elapsed,
                    )
                if not any(c.key == COOKIE_XSRF for c in self._session.cookie_jar):
                    # A 200 that seeds no CSRF cookie is not the track page.
                    # Naming which page it *is* separates an Akamai block
                    # from a redirect or a maintenance stub, and the answer
                    # decides whether the budget should be emptied. Public
                    # error-page markup, no credentials — but truncated
                    # anyway, because debug logs get pasted into issues.
                    snippet = " ".join(
                        body[:400].decode("utf-8", "replace").split()
                    )
                    _LOGGER.warning(
                        "UPS bootstrap returned %s bytes without %s over %s. "
                        "Page starts: %s",
                        len(body),
                        COOKIE_XSRF,
                        handshake,
                        snippet[:200],
                    )
                    csrf_seeded = False
                else:
                    csrf_seeded = True
                if response.status != 200:
                    raise UPSApiError(
                        f"bootstrap HTTP {response.status}",
                        status_code=response.status,
                    )
        except asyncio.TimeoutError as err:
            # The header guard failing is the expected cause here. Never
            # mistaken for "not found".
            _LOGGER.debug(
                "UPS bootstrap GET %s timed out after %.2fs",
                url,
                time.monotonic() - started,
            )
            raise UPSApiError("bootstrap timed out", timed_out=True) from err
        # Only a bootstrap that seeded the CSRF cookie counts as done. Marking
        # it regardless meant every later lookup skipped the GET, raised on
        # the missing cookie and cost the caller a token for a cycle in which
        # nothing left the machine at all — and gave up the one chance of
        # being served the real page next time.
        self._bootstrapped = csrf_seeded

    def _xsrf_header(self) -> dict[str, str]:
        """Return the double-submit CSRF header, copied from the cookie jar."""
        for cookie in self._session.cookie_jar:
            if cookie.key == COOKIE_XSRF:
                return {HEADER_XSRF: cookie.value}
        return {}

    async def _async_track(
        self, tracking_code: str, locale: str
    ) -> dict[str, Any] | None:
        """POST the actual tracking request. See :meth:`async_get_parcel`."""
        url = TRACKING_API_URL.format(locale=locale)
        self.requests_made += 1
        xsrf_header = self._xsrf_header()
        headers = {
            **FETCH_HEADERS,
            "Accept-Language": _accept_language(locale),
            **xsrf_header,
        }
        body = {
            "Locale": locale,
            "TrackingNumber": [tracking_code],
            "isBarcodeScanned": False,
            "Requester": "quic",
            "ClientUrl": "https://www.ups.com/track",
            "returnToValue": "",
            "AssociatedBcdnNumber": None,
        }
        _LOGGER.debug(
            "UPS track POST %s for %s starting (csrf header present: %s)",
            url,
            tracking_code,
            bool(xsrf_header),
        )
        started = time.monotonic()

        try:
            async with self._session.post(
                url, json=body, headers=headers, timeout=REQUEST_TIMEOUT
            ) as response:
                elapsed = time.monotonic() - started
                _LOGGER.debug(
                    "UPS track POST for %s -> %s in %.2fs",
                    tracking_code,
                    response.status,
                    elapsed,
                )
                if elapsed >= SLOW_REQUEST_SECONDS:
                    _LOGGER.warning(
                        "UPS lookup for %s took %.1fs (measured ~2-3s healthy). Slow "
                        "transport, not a warning of the coming close — that "
                        "arrives without one.",
                        tracking_code,
                        elapsed,
                    )
                if response.status == 401:
                    raise UPSApiError("HTTP 401 (CSRF/session expired)", status_code=401)
                if response.status != 200:
                    raise UPSApiError(
                        f"HTTP {response.status}", status_code=response.status
                    )
                try:
                    # content_type=None: a hang-guard failure sometimes yields
                    # an HTML shell instead of JSON, which must raise here
                    # rather than crash on aiohttp's own content-type check.
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    raise UPSApiError(f"unparseable body ({err})") from err
        except asyncio.TimeoutError as err:
            _LOGGER.debug(
                "UPS track POST for %s timed out after %.2fs",
                tracking_code,
                time.monotonic() - started,
            )
            raise UPSApiError(
                "request timed out — treat as transient, not not-found "
                "(the Akamai header guard hangs rather than erroring)",
                timed_out=True,
            ) from err

        if not isinstance(payload, dict):
            raise UPSApiError("unexpected body (not a JSON object)")

        track_details = payload.get("trackDetails")
        if not isinstance(track_details, list) or not track_details:
            raise UPSApiError("response carried no trackDetails")

        detail = track_details[0]
        if not isinstance(detail, dict):
            raise UPSApiError("trackDetails[0] was not an object")

        if detail.get("errorCode") == "504":
            return None
        if detail.get("errorCode"):
            raise UPSApiError(
                str(detail.get("errorText") or detail["errorCode"])
            )

        return detail
