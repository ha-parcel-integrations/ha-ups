"""Coordinator for the UPS parcel tracker integration.

Fetching and event firing only — the parcel mapping lives in :mod:`.parcels`.
That split is what lets the account-based variant swap this file out without
duplicating the mapping code.
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import UPSApiClient, UPSApiError
from .budget import RequestBudget
from .const import (
    COLD_INTERVAL_MINUTES,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_LOCALE,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    JITTER_FRACTION,
    KNOWN_LOCALES,
    MID_INTERVAL_MINUTES,
    MIN_CYCLE_GAP_SECONDS,
    REQUEST_BUDGET_CAPACITY,
    REQUEST_BUDGET_REFILL_SECONDS,
    STAGGER_MINUTES,
    STORAGE_KEY,
    STORAGE_VERSION,
    TRACKED_CODE_SOFT_LIMIT,
    ParcelStatus,
)
from .parcels import apply_delivered_filter, normalize_parcel, sort_parcels_by_ts

_LOGGER = logging.getLogger(__name__)

# Backoff when the carrier gives us nothing better to go on:
# ``BACKOFF_BASE_SECONDS * 2**consecutive_failures``, capped at
# ``BACKOFF_CAP_SECONDS``.
#
# The base sits above the slowest normal tier on purpose. A backoff shorter
# than the mid cadence would have the coordinator polling *more* often while
# it is supposed to be standing down — and what it is standing down from is a
# cooldown that only rest clears: measured still closed at 78 minutes and
# answering again at 113. The first stand-down is therefore two hours, past
# the far end of that range rather than inside it, doubling to a six-hour
# ceiling — nothing like the minutes-scale ramp a well-behaved 429 earns.
BACKOFF_BASE_SECONDS = 3600
BACKOFF_CAP_SECONDS = 21600

# Debounce on the cache write. Long enough that a burst of refreshes writes
# once, short enough that a restart moments later still sees the last poll.
STORE_SAVE_DELAY_SECONDS = 15

# How many recent fetch outcomes to keep for diagnostics. Enough to cover a
# few cycles of a multi-parcel install, so a throttle report shows the ramp
# (rising durations, then timeouts) without the user having had debug logging
# on beforehand — which they never do, because the problem is only noticed
# afterwards.
RECENT_FETCH_HISTORY = 25


# Which parcel earns the next request when several are waiting. Ordered by
# how soon each state can plausibly change: a parcel on a van moves within
# the hour, one at a pickup point cannot change at all until a human collects
# it. Never-attempted codes outrank all of these — see _fetch_queue.
_QUEUE_PRIORITY = (
    ParcelStatus.OUT_FOR_DELIVERY,
    ParcelStatus.PROBLEM,
    ParcelStatus.RETURNING,
    ParcelStatus.IN_TRANSIT,
    ParcelStatus.REGISTERED,
    ParcelStatus.UNKNOWN,
    ParcelStatus.AT_PICKUP_POINT,
)

# Floor under the overdue band: how many refill intervals a code may fall
# behind before the status ranking stops being able to hold it back. Past the
# band every overdue code is equally overdue, so the ranking decides between
# them again — it orders the stale parcels rather than outranking them.
#
# It is only a floor because the band has to clear a full rotation, and how
# long that takes depends on how many codes are sharing the budget. A ceiling
# below the rotation length stops discriminating exactly where it is needed:
# some other code is then pinned at it on every cycle, the starved code is
# never *strictly* the most overdue, and it loses the status tie-break
# forever rather than merely waiting longer.
_MIN_OVERDUE_INTERVALS = 3


def _resolve_locale(hass: HomeAssistant) -> str:
    """Return the ``loc`` query value to send UPS, from the HA locale.

    Only combinations known to work (KNOWN_LOCALES) are ever sent — the
    ``nameKey`` vocabulary parcels.py maps on is locale-independent, so a
    wrong-but-plausible locale would only affect display text UPS returns,
    but guessing one that turns out invalid is not worth the risk. Everything
    else falls back to DEFAULT_LOCALE.
    """
    language = (hass.config.language or "en").split("-")[0].lower()
    country = (hass.config.country or "").upper()
    candidate = f"{language}_{country}" if country else ""
    return candidate if candidate in KNOWN_LOCALES else DEFAULT_LOCALE


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int | None:
    """Tier for the barcode-based model (Section 2.1).

    ``None`` means "stop polling entirely" — nothing is tracked, or every
    tracked parcel is already delivered (already filtered out of
    ``active_parcels`` by the caller). A parcel waiting at a pickup point
    earns the cold tier, but only if *every* active parcel is waiting: one
    still in transit means something can change at the normal cadence.
    """
    if not active_parcels:
        return None

    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return HOT_INTERVAL_MINUTES
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return HOT_INTERVAL_MINUTES
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return HOT_INTERVAL_MINUTES

    if all(
        parcel["status"] == ParcelStatus.AT_PICKUP_POINT
        for parcel in active_parcels
    ):
        return COLD_INTERVAL_MINUTES

    return MID_INTERVAL_MINUTES


def _next_update_interval(
    now: datetime, wait_seconds: float | None, entry_id: str
) -> timedelta | None:
    """Turn "when is the next request affordable" into an ``update_interval``.

    ``None`` fully suspends scheduling (``DataUpdateCoordinator`` honours this
    natively). Otherwise the cadence follows the request budget rather than
    the parcel's status: with roughly one request every 75 minutes to spend,
    how *soon* a parcel could change no longer decides anything — how soon we
    may ask does. Status decides which parcel gets the request, not when.

    There is no quiet window and no daily anchor, unlike every sibling repo.
    See ``const.py``: the budget already caps the load, so pausing overnight
    discards allowance instead of sparing anyone, and UPS scans at night.
    """
    if wait_seconds is None:
        return None

    delay = max(wait_seconds, MIN_CYCLE_GAP_SECONDS)
    # Stable per-install offset plus a fresh per-cycle jitter. Both additive
    # only, so a computed cadence never comes out faster than the budget
    # allows — the whole point is that it cannot.
    stagger = timedelta(minutes=_stagger_minutes(entry_id))
    jitter = timedelta(seconds=random.uniform(0, delay * JITTER_FRACTION))
    return timedelta(seconds=delay) + stagger + jitter


class UPSCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls each tracked parcel and publishes the canonical parcel lists.

    This carrier has no account or parcel feed, so the tracked parcels are the
    tracking codes the user entered (stored in the entry options). Each is
    fetched individually and merged into one list; ``coordinator.data`` is the
    active (not-yet-delivered) parcels, ``self.delivered`` the rest.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: UPSApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Passing config_entry makes self.config_entry available on the
            # base class, which every helper below relies on.
            config_entry=entry,
            name=DOMAIN,
            # Recomputed at the end of every refresh (Section 2.1's tiering) —
            # start with the hot cadence so the very first poll, right after
            # setup, happens promptly regardless of what it finds.
            update_interval=timedelta(minutes=HOT_INTERVAL_MINUTES),
        )
        self._client = client
        # Resolved once at startup — HA's locale/country don't change without
        # a restart, and the nameKey vocabulary we map on is locale-independent
        # anyway (see _resolve_locale).
        self._locale = _resolve_locale(hass)
        self.delivered: list[dict] = []
        # tracking_code -> last successful raw payload, so a transient fetch
        # failure or a not-found blip keeps the parcel visible instead of
        # dropping its sensor. Persisted, so a restart publishes real parcels
        # rather than a screen of placeholders it would have to spend the
        # whole allowance refilling.
        self._raw_cache: dict[str, dict] = {}
        # Tracking codes whose parcel has reached the terminal delivered
        # state. Skipped on every subsequent poll — see _async_update_data.
        # Rebuilt from each cycle's payloads and persisted alongside them, so
        # a restart does not spend a request re-confirming a parcel that has
        # already arrived.
        self._delivered_codes: set[str] = set()
        # Codes we have issued at least one request for. Persisted, so a
        # restart does not re-classify a code as "new" and hand it the front
        # of the queue all over again — see _fetch_queue.
        self._attempted_codes: set[str] = set()
        # tracking_code -> UTC epoch of its last request, so the queue can
        # serve the stalest parcel first and nothing starves behind a parcel
        # that keeps outranking it.
        self._last_fetch_by_code: dict[str, float] = {}
        # tracking_code -> last known status, for the queue's priority order.
        # Keyed on the code rather than the barcode: a parcel that has not
        # been scanned yet has no barcode to key on.
        self._status_by_code: dict[str, ParcelStatus] = {}
        # Consecutive fully-failed refresh cycles, for the exponential
        # backoff. Reset to 0 as soon as any parcel fetch succeeds.
        #
        # Deliberately not keyed on 429: this carrier's throttle does not
        # answer 429, it stops answering at all (a per-address request budget
        # on unvalidated sessions — every further request hangs until the
        # address has rested).
        # A backoff wired only to 429 would therefore never fire on the one
        # failure mode that actually happens here, and the coordinator would
        # keep retrying at the tier cadence, feeding the very cooldown it is
        # waiting out. The 429 branch is kept for the day UPS starts
        # answering honestly, but it is not the primary trigger.
        self._consecutive_failures = 0
        # When the current stand-down ends, as a UTC epoch deadline rather
        # than a duration. A duration is recomputed from *now* on every cycle,
        # so any refresh in between — adding a parcel, an options change —
        # silently pushed the stand-down a further full length into the
        # future. Observed live: two parcels added three minutes apart moved
        # the next poll 33 minutes later instead of leaving it where it was.
        self._standdown_until_utc: float | None = None
        # Whether the tracked-code warning has already been given. Without
        # this it repeats at WARNING on every cycle for as long as the codes
        # are tracked, which is a setting, not an event.
        self._warned_soft_limit = False
        # UTC epoch seconds of the last cycle that issued real HTTP —
        # diagnostics only now that the budget carries the pacing. Wall clock
        # rather than monotonic so it can be persisted and outlive a restart.
        self._last_fetch_utc: float | None = None
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        # What this address is still allowed to ask for. Restored from disk in
        # async_load_cache — a budget that starts full on every boot would be
        # no budget at all.
        self._budget = RequestBudget(
            capacity=REQUEST_BUDGET_CAPACITY,
            refill_seconds=REQUEST_BUDGET_REFILL_SECONDS,
        )
        # Last tier computed by _hottest_tier_minutes — surfaced in
        # diagnostics; ``None`` before the first refresh and whenever polling
        # is fully suspended (nothing tracked, or everything delivered).
        self._current_tier_minutes: int | None = None
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started — otherwise every
        # restart would flood users with "registered" notifications.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None
        # Rolling record of individual fetch outcomes for diagnostics. Holds
        # no tracking codes — a duration and an outcome is all a throttle
        # report needs, and diagnostics get pasted into public issues.
        self._recent_fetches: deque[dict[str, Any]] = deque(
            maxlen=RECENT_FETCH_HISTORY
        )

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    async def async_load_cache(self) -> None:
        """Restore the fetch floor and payload cache from disk.

        Called once during setup, before the first refresh, so a restart
        inside the floor serves parcels immediately and issues no requests.
        A missing, unreadable or malformed store is not an error — the
        integration simply starts cold.
        """
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        raw_cache = stored.get("raw_cache")
        if isinstance(raw_cache, dict):
            self._raw_cache = {
                code: raw
                for code, raw in raw_cache.items()
                if isinstance(code, str) and isinstance(raw, dict)
            }
        delivered_codes = stored.get("delivered_codes")
        if isinstance(delivered_codes, list):
            self._delivered_codes = {c for c in delivered_codes if isinstance(c, str)}
        attempted_codes = stored.get("attempted_codes")
        if isinstance(attempted_codes, list):
            self._attempted_codes = {c for c in attempted_codes if isinstance(c, str)}
        failures = stored.get("consecutive_failures")
        if isinstance(failures, int) and failures > 0:
            self._consecutive_failures = failures
        standdown_until = stored.get("standdown_until_utc")
        if not isinstance(standdown_until, (int, float)) and self._consecutive_failures:
            # Written by a version that stored only the failure count. The
            # deadline it implies is unknowable, so assume the stand-down
            # started now — the conservative reading, and what that version
            # did on every restart anyway.
            standdown_until = time.time() + min(
                BACKOFF_BASE_SECONDS * 2**self._consecutive_failures,
                BACKOFF_CAP_SECONDS,
            )
        if isinstance(standdown_until, (int, float)):
            # Carry the stand-down across the restart, otherwise a reboot
            # either cancels it or — when re-derived from the failure count —
            # restarts it at full length, making a restart during a
            # stand-down actively worse than sitting still.
            self._standdown_until_utc = float(standdown_until)
            remaining = self._standdown_until_utc - time.time()
            if remaining > 0:
                self.update_interval = timedelta(seconds=remaining)
        last_fetch = stored.get("last_fetch_utc")
        if isinstance(last_fetch, (int, float)):
            self._last_fetch_utc = float(last_fetch)
        per_code = stored.get("last_fetch_by_code")
        if isinstance(per_code, dict):
            self._last_fetch_by_code = {
                code: float(when)
                for code, when in per_code.items()
                if isinstance(code, str) and isinstance(when, (int, float))
            }
        self._budget = RequestBudget.from_dict(
            stored.get("budget"),
            REQUEST_BUDGET_CAPACITY,
            REQUEST_BUDGET_REFILL_SECONDS,
        )
        # What came off disk decides whether this start polls at all, so it is
        # the first thing worth knowing when a restart behaves unexpectedly.
        _LOGGER.debug(
            "UPS restored cache: %s payloads, %s attempted codes, %s delivered, "
            "%s consecutive failures, last fetch %s, %s request(s) of budget left",
            len(self._raw_cache),
            len(self._attempted_codes),
            len(self._delivered_codes),
            self._consecutive_failures,
            f"{self.seconds_since_last_fetch:.0f}s ago"
            if self.seconds_since_last_fetch is not None
            else "never",
            self._budget.available(),
        )

    def _persist_cache(self) -> None:
        """Queue a debounced write of the cache and the fetch floor."""
        self._store.async_delay_save(
            lambda: {
                "last_fetch_utc": self._last_fetch_utc,
                "raw_cache": self._raw_cache,
                "delivered_codes": sorted(self._delivered_codes),
                "attempted_codes": sorted(self._attempted_codes),
                "consecutive_failures": self._consecutive_failures,
                "standdown_until_utc": self._standdown_until_utc,
                "last_fetch_by_code": self._last_fetch_by_code,
                "budget": self._budget.as_dict(),
            },
            STORE_SAVE_DELAY_SECONDS,
        )

    def _schedule(self, now: datetime, wait_seconds: float | None) -> None:
        """Set ``update_interval``, letting an active stand-down outrank it.

        Every path that schedules goes through here. ``seed_from_cache`` used
        to compute its own interval from the budget alone, so a restart during
        a stand-down published from cache and then quietly overwrote the
        deadline the load had just restored — observed 2026-09-07, a two-hour
        stand-down cut to the 68 minutes the budget happened to want.
        """
        standdown_left = (
            self._standdown_until_utc - time.time()
            if self._standdown_until_utc
            else 0.0
        )
        if wait_seconds is not None and standdown_left > wait_seconds:
            # A hang costs more than the budget knows about: the address stays
            # shut for roughly two hours, while the next token is due in one
            # and a quarter. Left to the budget alone the coordinator would
            # poll straight back into the cooldown it is waiting out.
            #
            # Scheduled straight to the deadline, deliberately without jitter:
            # jitter only ever adds, so re-rolling it every cycle would nudge
            # recovery further out each time.
            self.update_interval = timedelta(seconds=standdown_left)
        else:
            self.update_interval = _next_update_interval(
                now, wait_seconds, self.config_entry.entry_id
            )

    def seed_from_cache(self) -> bool:
        """Publish every tracked parcel without any HTTP. Returns whether it did.

        Setup issues no requests at all. The first refresh runs inline in
        ``async_setup_entry``, so with the address closed every tracked code
        burns its full timeout in sequence — observed blocking entry setup for
        88 s on two codes while issuing exactly the burst that keeps the
        cooldown alive. Restarting a few times while configuring is normal,
        and it was the most reliable way to lose the endpoint.

        A code with no cached payload is seeded with a placeholder rather than
        fetched, so it gets its sensor immediately and shows ``unknown`` until
        the rotation reaches it. An entity that exists and admits it knows
        nothing yet is far better than a request storm, and far better than no
        entity at all.
        """
        codes = self._tracked()
        if not codes:
            return False
        raws = [
            self._raw_cache.get(code) or {"requestedTrackingNumber": code}
            for code in codes
        ]
        active = self._publish(raws)
        now = dt_util.now()
        self._current_tier_minutes = _hottest_tier_minutes(active, now)
        wait_seconds = (
            None if not self._fetch_queue(codes) else self._budget.seconds_until_token()
        )
        self._schedule(now, wait_seconds)
        # Publishes the data and arms the scheduler off the interval just
        # computed, so the first real request lands when the budget allows.
        self.async_set_updated_data(active)
        return True

    @property
    def recent_fetches(self) -> list[dict[str, Any]]:
        """Recent per-request outcomes, oldest first (diagnostics only)."""
        return list(self._recent_fetches)

    @property
    def consecutive_failures(self) -> int:
        """Fully-failed cycles behind the current backoff (diagnostics only)."""
        return self._consecutive_failures

    @property
    def seconds_since_last_fetch(self) -> float | None:
        """Age of the last cycle that issued HTTP (diagnostics only)."""
        if self._last_fetch_utc is None:
            return None
        return round(time.time() - self._last_fetch_utc, 1)

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    def _tracked(self) -> list[str]:
        """Return the configured tracking codes."""
        return [
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
        ]

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    def _budget_holds(self) -> bool:
        """Whether the address has nothing left to spend right now."""
        if self._budget.available() >= 1:
            return False
        _LOGGER.debug(
            "UPS budget spent: next request in %.0fs",
            self._budget.seconds_until_token(),
        )
        return True

    def _standing_down(self) -> bool:
        """Whether a failed cycle's stand-down still forbids asking.

        The budget alone is not that gate. A hang costs a stand-down of two
        hours while the next token is due in one and a quarter, so for the
        three quarters of an hour in between every manual route into the
        coordinator — an options change, ``track_parcel``,
        ``homeassistant.update_entity`` — used to find a token and spend it
        straight back into the cooldown being waited out. Worse, a request
        that then succeeded cleared the stand-down entirely.
        """
        if not self._standdown_until_utc:
            return False
        remaining = self._standdown_until_utc - time.time()
        if remaining <= 0:
            return False
        _LOGGER.debug(
            "UPS standing down: %.0fs left before the next request", remaining
        )
        return True

    def _fetch_queue(self, codes: list[str]) -> list[str]:
        """Tracked codes in the order they deserve the next request.

        A cycle spends one request, so this is not a filter but a ranking —
        everything still in flight stays in the queue and eventually reaches
        the front. Three rules, in order:

        * a delivered code is dropped entirely; that state is terminal;
        * a code we have never attempted comes first. The user added it and
          is watching for it, and it is the only case where waiting an hour
          reads as the integration being broken;
        * a code that has fallen a full rotation behind comes next, oldest
          first, whatever its status;
        * everything else sorts by what can still change soonest, and within
          that by how long it has gone unrefreshed.

        The overdue band is what stops a parcel starving. Ranking on status
        first and using staleness only as a tie-break *within* a status reads
        as fair and is not: a status group never yields to a colder one, so a
        single parcel in a hotter group wins every cycle for as long as it
        stays there. Observed live 2026-09-07 — one parcel reached
        ``in_transit`` and took all three of the address's requests in
        twenty-two minutes while five parcels on ``unknown``, none of which
        had ever returned a payload, waited behind it with no way of ever
        reaching the front. With one request per cycle the queue head is the
        whole decision, so this is starvation, not a slower turn.

        Codes that have never been attempted still come first: the user just
        added them and is watching. A code that keeps failing cannot hold the
        front either — ``_last_fetch_by_code`` is stamped before the request
        goes out, so an attempt that never comes back still counts as a turn
        taken.
        """
        queued = [code for code in codes if code not in self._delivered_codes]
        ranking = {status: rank for rank, status in enumerate(_QUEUE_PRIORITY)}
        unknown_rank = len(_QUEUE_PRIORITY)
        now = time.time()

        # One request per cycle shared between the queue means each code comes
        # round every ``len(queued)`` intervals, so the band is sized from the
        # queue rather than fixed. The extra interval is what makes a code that
        # has waited a whole rotation out rank strictly ahead of every code
        # that has already had its turn, instead of merely tying with them.
        max_overdue = max(_MIN_OVERDUE_INTERVALS, len(queued) + 1)

        def sort_key(code: str) -> tuple[int, int, int, float]:
            never_attempted = code not in self._attempted_codes
            status = self._status_by_code.get(code)
            last_fetch = self._last_fetch_by_code.get(code, 0.0)
            overdue = min(
                int((now - last_fetch) // REQUEST_BUDGET_REFILL_SECONDS),
                max_overdue,
            )
            return (
                0 if never_attempted else 1,
                -overdue,
                ranking.get(status, unknown_rank),
                last_fetch,
            )

        return sorted(queued, key=sort_key)

    async def _async_update_data(self) -> list[dict]:
        """Spend at most one request, then republish every tracked parcel.

        One request per cycle, not one per parcel. UPS answers a residential
        address roughly three times before it stops answering at all, so a
        sweep over N parcels spends the whole allowance in one go and leaves
        nothing for the rest of the day. Parcels take turns instead: the
        cadence comes from the budget, the order from :meth:`_fetch_queue`.
        """
        codes = self._tracked()

        # Drop cache entries for parcels the user no longer follows, so the
        # cache stays bounded.
        tracked_codes = set(codes)
        self._raw_cache = {
            code: raw for code, raw in self._raw_cache.items() if code in tracked_codes
        }
        self._delivered_codes &= tracked_codes
        self._attempted_codes &= tracked_codes
        self._status_by_code = {
            code: status
            for code, status in self._status_by_code.items()
            if code in tracked_codes
        }
        self._last_fetch_by_code = {
            code: when
            for code, when in self._last_fetch_by_code.items()
            if code in tracked_codes
        }

        # Delivered is terminal: nothing about that parcel can change again,
        # so re-fetching it is pure waste of an allowance measured in single
        # requests. The code stays in the options list (the user removes it
        # when they want to) and its cached payload keeps feeding the
        # delivered sensor until the retention filter drops it, but it costs
        # nothing. Nothing else in the suite does this: for account-based
        # carriers one call returns everything, so the saving only exists for
        # a code-based carrier.
        queue = self._fetch_queue(codes)
        skipped_delivered = len(codes) - len(queue)

        codes_to_fetch: list[str] = []
        if queue and not self._budget_holds() and not self._standing_down():
            codes_to_fetch = [queue[0]]

        # The single most useful line in a post-mortem: what the cycle chose
        # to do and what was left waiting. Every confusing behaviour so far
        # ("3 codes, 1 parcel", the retry storm) was one of these numbers
        # differing from what the user expected.
        _LOGGER.debug(
            "UPS cycle: %s tracked, %s delivered, %s waiting; fetching %s, "
            "%s request(s) of budget left",
            len(codes),
            skipped_delivered,
            len(queue),
            len(codes_to_fetch),
            self._budget.available(),
        )

        if queue and not codes_to_fetch:
            # Out of budget. Fall through rather than returning the previous
            # list: a parcel added while the budget is empty must still get
            # its sensor now, showing unknown, and returning what was already
            # published would leave it invisible for hours.
            _LOGGER.debug(
                "UPS: nothing affordable this cycle; republishing %s tracked "
                "parcel(s) from cache",
                len(codes),
            )

        if len(queue) <= TRACKED_CODE_SOFT_LIMIT:
            self._warned_soft_limit = False
        elif not self._warned_soft_limit:
            self._warned_soft_limit = True
            _LOGGER.warning(
                "UPS is tracking %s parcels that still need updates, and this "
                "carrier allows roughly one request per %.0f minutes. Each "
                "parcel will only refresh about every %.0f hours until some "
                "are delivered or removed.",
                len(queue),
                REQUEST_BUDGET_REFILL_SECONDS / 60,
                len(queue) * REQUEST_BUDGET_REFILL_SECONDS / 3600,
            )

        if codes_to_fetch:
            self._budget.try_spend()
            self._last_fetch_utc = time.time()
            # Marked before the request goes out, not after: an attempt counts
            # whether or not it comes back.
            self._attempted_codes |= set(codes_to_fetch)
            for code in codes_to_fetch:
                self._last_fetch_by_code[code] = self._last_fetch_utc

        # Kept as a list even though a cycle fetches one code: the collection
        # below still mirrors gather's return_exceptions=True contract, so a
        # failed fetch becomes a result rather than aborting the republish of
        # every other parcel.
        results: list[Any] = []
        requests_before = self._client.requests_made
        for code in codes_to_fetch:
            started = time.monotonic()
            try:
                results.append(
                    await self._client.async_get_parcel(code, locale=self._locale)
                )
                self._record_fetch(time.monotonic() - started, None)
            except Exception as err:  # noqa: BLE001 - collected like gather(return_exceptions=True)
                self._record_fetch(time.monotonic() - started, err, code)
                results.append(err)

        # A 401 re-bootstraps and posts again for the same parcel, so one
        # lookup can leave twice. The token above covers one; charge the rest,
        # or the budget believes the address has more left than it does.
        unbilled = self._client.requests_made - requests_before - len(codes_to_fetch)
        for _ in range(max(0, unbilled)):
            self._budget.try_spend()

        raws: list[dict] = []
        # Codes already represented in ``raws``, tracked by the code we *asked
        # for* rather than the one the payload echoes back. Deriving it from
        # the payload would publish a parcel twice the moment UPS answered
        # with a different number than the one requested.
        covered: set[str] = set()
        errors = 0
        retry_afters: list[float] = []
        saw_429 = False
        for code, result in zip(codes_to_fetch, results):
            if isinstance(result, BaseException):
                if not isinstance(
                    result, (UPSApiError, aiohttp.ClientError)
                ):
                    raise result
                errors += 1
                if (
                    isinstance(result, UPSApiError)
                    and result.status_code == 429
                ):
                    saw_429 = True
                    if result.retry_after is not None:
                        retry_afters.append(result.retry_after)
                _LOGGER.warning("UPS fetch failed for %s: %s", code, result)
                cached = self._raw_cache.get(code)
                if cached is not None:
                    raws.append(cached)
                    covered.add(code)
                continue

            if result is None:
                # errorCode 504 (not found), or not scanned yet. Keep prior
                # data if we have it, otherwise show a pending placeholder so
                # the user still sees the parcel they asked us to track.
                raws.append(
                    self._raw_cache.get(code) or {"requestedTrackingNumber": code}
                )
                covered.add(code)
                continue

            # UPS always echoes requestedTrackingNumber, but guard the edge
            # case anyway so the sensor never loses its key.
            result.setdefault("requestedTrackingNumber", code)
            self._raw_cache[code] = result
            raws.append(result)
            covered.add(code)

        # Every tracked code must be represented, whatever happened above:
        # from its fresh payload, its cached one, or a placeholder that shows
        # `unknown`. A code whose *own* fetch just failed with nothing cached
        # gets a placeholder too, rather than disappearing.
        #
        # This has to happen before the failure handling below. It used to run
        # after it, so a first fetch that failed with an empty cache raised
        # and published nothing at all — three codes added, three sensors
        # expected, and the user saw nothing.
        for code in codes:
            if code not in covered:
                raws.append(
                    self._raw_cache.get(code) or {"requestedTrackingNumber": code}
                )

        # A 429, or a cycle whose fetch failed, both mean the same thing: stop
        # asking for a while. The second case is the one that actually occurs
        # here — the symptom is a timeout, not a status code — and without it
        # the coordinator would keep retrying and hold its own cooldown open.
        all_failed = bool(codes_to_fetch) and errors == len(codes_to_fetch)
        backoff_seconds: float | None = None
        if saw_429 or all_failed:
            self._consecutive_failures += 1
            # Only silence empties the budget. Anything that came back —
            # a 401 from a stale CSRF token, a 429, a body we could not parse
            # — was *answered*, so it says nothing about what the address has
            # left; zeroing on one throws away hours of allowance over a
            # session bug. Keyed on the timeout itself rather than on a
            # missing status code, which five answered failures also have.
            if any(
                isinstance(r, UPSApiError) and r.timed_out
                for r in results
                if isinstance(r, BaseException)
            ):
                self._budget.exhaust()
            backoff_seconds = (
                max(retry_afters)
                if retry_afters
                else min(
                    BACKOFF_BASE_SECONDS * 2**self._consecutive_failures,
                    BACKOFF_CAP_SECONDS,
                )
            )
            # Never shorter than a refill: a backoff below it would schedule
            # retries the budget then silently declines, leaving the
            # coordinator idling at an interval that can neither escalate nor
            # recover.
            backoff_seconds = max(backoff_seconds, REQUEST_BUDGET_REFILL_SECONDS)
            self._standdown_until_utc = time.time() + backoff_seconds
            # ``retry_after`` on UpdateFailed only covers the next single
            # cycle (the coordinator clears it again straight after), so the
            # interval itself has to carry a backoff that lasts longer than
            # one skipped poll.
            self.update_interval = timedelta(seconds=backoff_seconds)
            self._current_tier_minutes = None
            self._persist_cache()
            # No ``UpdateFailed``: raising discards everything built above and
            # takes the entities unavailable, which is the opposite of what a
            # placeholder is for. The stand-down rides on ``update_interval``,
            # the warning says how long it is, and ``last_success_time`` stops
            # advancing so staleness stays visible.
            _LOGGER.warning(
                "UPS: this cycle's %s fetch(es) failed (failure #%s); serving "
                "%s tracked parcel(s) from cache and standing down for %.0fs",
                len(codes_to_fetch),
                self._consecutive_failures,
                len(codes),
                backoff_seconds,
            )
        elif codes_to_fetch:
            # Only a cycle that actually reached UPS may clear the backoff. A
            # cycle that made no request proves nothing about reachability,
            # and treating it as recovery would let an out-of-budget cycle
            # cancel the stand-down the previous failure just earned.
            if self._consecutive_failures:
                _LOGGER.info(
                    "UPS reachable again after %s failed cycle(s); resuming the "
                    "normal cadence",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
            self._standdown_until_utc = None

        normalized_active = self._publish(raws)

        # Stamp only when this cycle actually reached UPS. A poll where every
        # code was skipped as already-delivered talked to nobody, so it keeps
        # the previous stamp rather than presenting itself as fresh.
        if not codes or (codes_to_fetch and errors < len(codes_to_fetch)):
            self.last_success_time = datetime.now(timezone.utc)

        self._persist_cache()

        if backoff_seconds is None:
            now = dt_util.now()
            # The tier no longer sets the cadence — the budget does — but it
            # still answers the one question the budget cannot: whether there
            # is anything worth asking about at all.
            self._current_tier_minutes = _hottest_tier_minutes(
                normalized_active, now
            )
            wait_seconds = (
                None
                if not self._fetch_queue(self._tracked())
                else self._budget.seconds_until_token()
            )
            self._schedule(now, wait_seconds)

        # Suspending scheduling entirely is correct when everything really is
        # delivered, but it is also the end state of every bug that loses
        # track of an active parcel — and once suspended nothing but an
        # options change wakes it. Too consequential to happen silently.
        if self.update_interval is None:
            _LOGGER.info(
                "UPS polling suspended: no active parcel among %s tracked "
                "code(s). It resumes when a parcel is added or removed.",
                len(codes),
            )
        else:
            _LOGGER.debug(
                "UPS cycle done: %s active, %s delivered, %s fetch error(s); "
                "tier %s, next poll in %.0fs",
                len(normalized_active),
                len(self.delivered),
                errors,
                self._current_tier_minutes,
                self.update_interval.total_seconds(),
            )
        return normalized_active

    def _record_fetch(
        self, seconds: float, error: BaseException | None, code: str | None = None
    ) -> None:
        """Note one request's duration and outcome for diagnostics.

        The message is scrubbed of the code that produced it. Most of these
        strings are our own and name no parcel, but one is UPS's ``errorText``
        passed through verbatim, and diagnostics get pasted into public issues
        — the one field here that ``TO_REDACT`` cannot reach must not be the
        one that leaks a tracking number.
        """
        message = str(error) if error is not None else None
        if message and code:
            message = message.replace(code, "<code>")
        self._recent_fetches.append(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "seconds": round(seconds, 2),
                "ok": error is None,
                "error": message,
            }
        )

    def _publish(self, raws: list[dict]) -> list[dict]:
        """Normalise raw payloads into the active/delivered lists and events.

        Shared by a real poll and by :meth:`seed_from_cache`, so a cache-only
        start produces exactly the same entity state a poll would.
        """
        codes = set(self._tracked())
        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, include_history=include_history, locale=self._locale)
            for raw in raws
        ]
        active = [parcel for parcel in normalized if not parcel["delivered"]]
        delivered = [parcel for parcel in normalized if parcel["delivered"]]

        # Remember which codes are done, so the next cycle skips them. Keyed
        # on the tracking code the request was made with, not the barcode:
        # a not-yet-scanned parcel has no barcode to key on.
        self._delivered_codes = {
            raw.get("requestedTrackingNumber")
            for raw, parcel in zip(raws, normalized)
            if parcel["delivered"] and raw.get("requestedTrackingNumber")
        } & codes
        self._status_by_code = {
            raw["requestedTrackingNumber"]: parcel["status"]
            for raw, parcel in zip(raws, normalized)
            if raw.get("requestedTrackingNumber") in codes
        }

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set.
        incoming = normalized_active + self.delivered
        self._fire_change_events(incoming)
        self._known_state = {
            parcel["barcode"]: parcel["status"]
            for parcel in incoming
            if parcel.get("barcode")
        }
        self._known_delivery_times = {
            parcel["barcode"]: (parcel.get("planned_from"), parcel.get("planned_to"))
            for parcel in incoming
            if parcel.get("barcode")
        }
        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new versus already present before HA started.

        The event contract, identical across the suite:

        * every payload is the full normalised parcel plus ``device_id``;
        * the hop **to** ``delivered`` fires only ``_parcel_delivered``, never
          also ``_parcel_status_changed``;
        * a barcode first seen already-delivered fires nothing;
        * ``registered`` only fires for a new, not-yet-delivered barcode;
        * an ETA going ``value → null`` is intentionally silent — the carrier
          just lost the window, which is not worth waking someone up for.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )
