# Architecture

This document describes the internal structure of the UPS integration, how the
components relate to each other, and the key design decisions made. It is
intended as a reference for AI agents and contributors working on the codebase.

UPS is a **code-based, account-less** carrier: the user enters tracking numbers
and there is no parcel feed to discover them from. The dominant constraint on
every design decision below is that the endpoint answers a residential address
roughly **three times before it stops answering at all**. The measurements
behind that live in `carrier-research/ups/api/tracking.md`; this document
describes what the code does about it.

## Project layout

```
custom_components/ups/
├── __init__.py        # Entry point: setup, teardown, per-entry session + cookie jar
├── api.py             # HTTP client: cookie bootstrap, CSRF double-submit, track POST
├── budget.py          # The per-address request budget, as a persisted token bucket
├── calendar.py        # Deliveries calendar entity
├── config_flow.py     # UI config flow: single-instance hub, parcels + settings options
├── const.py           # Constants: URLs, ParcelStatus, budget/tier values, option keys
├── coordinator.py     # DataUpdateCoordinator: one request per cycle, cache, events
├── device.py          # Shared device-info helper
├── device_trigger.py  # Exposes bus events as device automation triggers
├── diagnostics.py     # Redacted diagnostics dump
├── parcels.py         # Status maps, normalize_parcel, history, sort, filters — pure
├── sensor.py          # Summary + per-parcel + awaiting-pickup + delivered + last_update
├── manifest.json      # HA integration manifest
├── icons.json         # Entity icon translations
├── strings.json       # UI strings (source of truth, duplicated into translations/)
└── translations/
    ├── en.json
    └── nl.json
```

There is deliberately **no `button.py`**, and `PLATFORMS` carries no
`Platform.BUTTON`. See [No refresh button](#no-refresh-button).

## Data flow

```
                webapis.ups.com/track/api/Track/GetStatus
                                  │
                                  ▼
                       UPSApiClient (api.py)
        ┌──────────────────────────────────────────────────┐
        │ _async_bootstrap()   GET  → cookie jar + CSRF    │
        │ _async_track()       POST → trackDetails[0]      │
        └──────────────────────────────────────────────────┘
                                  ▲
                    one request, for one parcel
                                  │
                       UPSCoordinator (coordinator.py)
        ┌──────────────────────────────────────────────────┐
        │ RequestBudget  ──── may we ask at all?           │
        │ _standing_down ──── did a failure forbid it?     │
        │ _fetch_queue   ──── which parcel gets the turn?  │
        │ _raw_cache     ──── everyone else's last payload │
        └──────────────────────────────────────────────────┘
                                  │
                    normalize_parcel() per tracked code
                                  │
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
        coordinator.data                    coordinator.delivered
        (active parcels)                    (retention-filtered)
                 │                                 │
                 ├─────────────┬───────────────────┤
                 ▼             ▼                   ▼
        sensor entities   calendar entity     bus events
                                              (device triggers)
```

Every tracked code is represented in the published lists on **every** cycle —
from a fresh payload, its cached one, or a `{"requestedTrackingNumber": code}`
placeholder that normalises to `unknown`. A parcel never disappears because its
turn has not come.

## Component responsibilities

### `__init__.py`
- Creates one `aiohttp.ClientSession` **per config entry** with its own
  `CookieJar`, reusing HA's pooled connector (`connector_owner=False`). The
  cookie jar is the auth state, so it cannot be shared with HA's session.
- Calls `coordinator.async_load_cache()` then `coordinator.seed_from_cache()`.
  **Setup issues no HTTP at all** — see [Setup never
  fetches](#setup-never-fetches).
- Closes the session on every failing setup path (cache load, platform forward).
- Registers an update listener that calls `async_request_refresh()` — no
  reload, so the config-entry-listener deprecation is not tripped. This is also
  the resume path after polling has fully suspended.
- `async_remove_entry` deletes the persisted store.

### `api.py`
- Two calls per lookup: a bootstrap `GET` that seeds the cookie jar, then the
  track `POST` carrying the CSRF cookie's value in the `X-XSRF-TOKEN` header
  (Angular's stock double-submit; nothing is derived or signed).
- `GUARD_HEADERS` — four Akamai client-hint headers that are load-bearing
  **together**. `NAVIGATION_HEADERS` (bootstrap) and `FETCH_HEADERS` (track)
  layer a page-navigation and an XHR shape on top of that shared core.
- Returns `trackDetails[0]` for a known parcel, `None` for `errorCode: "504"`
  (not found — a normal state), and raises `UPSApiError` for everything else.
  `UPSApiError.timed_out` distinguishes silence from an answered failure.
- Re-bootstraps at most once per lookup on a 401; both the first attempt and
  the retry pass through `_require_xsrf()`, which refuses to spend a request
  the missing CSRF cookie has already doomed.
- `requests_made` counts track POSTs actually sent, so the coordinator can
  charge the budget for a 401's second POST.
- `_accept_language()` derives `Accept-Language` from the resolved `loc`.
  Asking for `loc=en_NL` while claiming `en-US` is exactly the internal
  inconsistency the guard's profile-completeness scoring exists to catch.

### `budget.py`
- `RequestBudget` — a token bucket, capacity `REQUEST_BUDGET_CAPACITY` (3),
  one token per `REQUEST_BUDGET_REFILL_SECONDS` (4500). Wall-clock, so it
  survives a restart; persisted with the cache.
- `try_spend()` re-anchors the refill clock **at the request**, not at the
  moment the token was earned, so a banked token cannot put two requests less
  than a beat apart.
- `exhaust()` zeroes it when a request hung despite the accounting saying there
  was budget — the estimate was wrong and the only safe reading is empty.
- Capacity and refill always come from `const.py`, never from disk.

### `coordinator.py`
- `_async_update_data()` spends **at most one request per cycle**, then
  republishes every tracked parcel. Gated by `_budget_holds()` and
  `_standing_down()`, in that order.
- `_fetch_queue()` ranks the tracked codes — see [Queue
  order](#queue-order).
- `_publish()` normalises, splits active/delivered, applies the retention
  filter, and fires the bus events. Shared by a real poll and by
  `seed_from_cache()`, so a cache-only start produces the same entity state.
- `_hottest_tier_minutes()` no longer sets the cadence (the budget does). It
  survives to answer one question: is anything worth asking about at all?
  `None` suspends scheduling entirely.
- `async_load_cache()` / `_persist_cache()` hold the payload cache, the
  delivered and attempted code sets, the per-code fetch timestamps, the
  consecutive-failure count, the stand-down deadline and the budget.

### `parcels.py`
Pure functions, no I/O and no HA objects beyond the config entry's options —
so the carrier-specific half is unit-testable without Home Assistant.

- `_STATUS_MAP` — `milestoneName.nameKey` → `ParcelStatus`, for the parcel's
  own status. Never map on `packageStatus`/`name`; those are localised.
- `_ACTIVITY_CODE_MAP` — `actCode` → `ParcelStatus | None`, for history
  entries only. A **second vocabulary**; see [Two status
  vocabularies](#two-status-vocabularies).
- `normalize_parcel()` — the canonical parcel dict.
- `published_raw()` / `PUBLISHED_RAW_FIELDS` — the curated `raw` subset; see
  [`raw` is curated](#raw-is-a-curated-subset).
- `build_history()`, `sort_parcels_by_ts()`, `apply_delivered_filter()` —
  suite-wide machinery, identical across carriers.

### `sensor.py`
- `UPSIncomingParcelsSensor` — active count; also owns the lifecycle of the
  per-parcel sensors (creates new barcodes, removes stale ones **via the
  entity registry**, never by self-removal).
- `UPSParcelSensor` — one per active parcel, keyed by barcode.
- `UPSAwaitingPickupSensor` — parcels at `ParcelStatus.AT_PICKUP_POINT`.
  Count and parcels only: no `upsAccessPoint` object has ever been observed
  populated, so there is no location to expose.
- `UPSNextDeliverySensor` — earliest `planned_from`. Permanently `None` on this
  carrier until an ETA field is confirmed.
- `UPSDeliveredParcelsSensor`, `UPSLastUpdateSensor` (diagnostic, TIMESTAMP).

### `diagnostics.py`
`TO_REDACT` covers the canonical PII fields plus every address, recipient,
signature, proof-of-delivery and account/token field UPS returns inside `raw`.
`recent_fetches` is the one field `async_redact_data` cannot reach, so the
coordinator scrubs the tracking code out of each recorded error message before
storing it.

## Key design decisions

### The request budget replaces the polling interval
The suite's status-driven tiering does not set the cadence here. With roughly
one request every 75 minutes to spend, how soon a parcel *could* change decides
nothing; how soon we may *ask* decides everything.

| Question | Answered by |
|---|---|
| *When* does the next request go out? | `RequestBudget` |
| *Which* parcel gets it? | `_fetch_queue()` |
| Is there anything worth asking at all? | `_hottest_tier_minutes()`, `None` = suspend |

The next cycle is `max(seconds_until_token, MIN_CYCLE_GAP_SECONDS)` plus a
stable per-install stagger plus jitter — all **additive**, so a computed
cadence can never come out faster than the budget allows.

`REQUEST_BUDGET_REFILL_SECONDS` is 4500 (75 min) although the measured beat is
60. Asking hourly nets *fewer* requests per day, because a request arriving
before its token hangs and costs a two-hour stand-down.

### Queue order
`_fetch_queue()` sorts on a four-part key:

1. never-attempted codes first (`_attempted_codes`, persisted — keyed on
   *attempted*, not on "has no data yet");
2. then how many refill intervals the code is behind, capped at
   `_MAX_OVERDUE_INTERVALS`;
3. then `_QUEUE_PRIORITY`: `out_for_delivery` → `problem` → `returning` →
   `in_transit` → `registered` → `unknown` → `at_pickup_point`;
4. then the longest unrefreshed (`_last_fetch_by_code`, persisted).

Delivered codes are dropped from the queue entirely.

The overdue band at (2) is what stops a parcel starving. Ranking on status with
staleness only as a tie-break *within* a status means a group never yields to a
colder one, so a single parcel in a hotter group wins every cycle for as long
as it stays there — and with one request per cycle the queue head is the whole
decision. Capping the band means that past a few missed turns everything is
equally overdue and the ranking orders the stale parcels rather than overruling
them.

`_last_fetch_by_code` is stamped **before** the request goes out, so a code
that keeps failing cannot hold the front.

### Locale
`coordinator._resolve_locale()` maps HA's language/country onto `loc`, but only
for combinations in `KNOWN_LOCALES` (`en_NL`, confirmed on the wire); anything
else falls back to `DEFAULT_LOCALE`. A wrong-but-plausible locale would only
change display text UPS returns — the `nameKey` vocabulary the status map runs
on is locale-independent — but guessing one that turns out invalid is not worth
the risk on a carrier this expensive to ask twice.

### Timing is jittered, additively
Every computed interval carries `JITTER_FRACTION` on top of the stable
per-install stagger. Spacing buys nothing against the budget itself — that is a
count, not a rate — but a perfect arithmetic sequence of request timestamps is
not something a browser produces, and this guard scores how browser-like a
client looks. Both offsets are **additive only**, so no computed cadence can
come out faster than the budget allows.

### No hard cap on tracked codes
`TRACKED_CODE_SOFT_LIMIT` (10) triggers a **one-shot** warning naming the
refresh interval each parcel then gets (`len(queue) × refill`, over half a day
at ten) — one-shot because it describes a setting, not an event. There is
deliberately no hard limit: the rotation degrades gracefully rather than
refusing parcels, and a user who adds twenty codes gets twenty sensors
immediately at zero request cost. Delivered codes leave the queue, so they
never push anyone over it.

### Setup never fetches
`seed_from_cache()` publishes every tracked parcel — from cache, or as a
placeholder — and there is no first-refresh fallback. Every manual route into
the coordinator (`track_parcel`, an options change,
`homeassistant.update_entity`) reaches `async_request_refresh()` and simply
finds no token.

### A failed cycle never raises `UpdateFailed`
Raising discards everything the cycle built and takes the entities unavailable,
which is the opposite of what the placeholders are for. Every tracked code is
represented in `raws` **before** the failure handling runs. The stand-down
rides on `update_interval`, a warning carries its length, and
`last_success_time` stops advancing so staleness stays visible.

### Two gates, not one
The budget says whether the address has anything left; `_standing_down()` says
whether a failure has forbidden asking regardless. They differ because a
stand-down (two hours) routinely outlasts the next token (75 minutes), and in
that gap the budget alone would say yes.

`_standdown_until_utc` is an absolute **deadline**, stamped once and counted
down to — never a duration recomputed from `now`, which any intervening refresh
would silently restart at full length.

`BACKOFF_BASE_SECONDS` is 1 h, doubling to a 6 h cap, and is keyed on **any**
fully-failed cycle rather than on a 429: this carrier does not answer 429, it
stops answering.

### Only silence empties the budget
`budget.exhaust()` fires on `UPSApiError.timed_out`, never on an answered
failure. A 401, a 429 or an unparseable body were all *answered*, so they say
nothing about what the address has left.

### A delivered code is never fetched again
`_delivered_codes` is terminal and persisted. For a code-based carrier,
fetching is driven by the options list rather than by `coordinator.data`, so
without this a delivered parcel keeps costing a request every cycle. The code
stays in the options list and its cached payload keeps feeding the delivered
sensor until the retention filter drops it.

`delivered` comes from `isDelivered`, which marks the end of the *journey*, not
a delivery: a parcel returned to the shipper carries it with no
`cms.stapp.delivered` scan anywhere. `_delivered_at()` therefore falls back to
the newest scan, or such a parcel would sort last forever and the days-based
retention filter — which keeps what it cannot parse — would never drop it.

### No refresh button
The template ships a manual-refresh button in every other carrier repo. Here it
is the easiest possible way for a user to lose the endpoint: an unthrottled,
user-triggerable poll, with HA's debouncer spacing presses 10 s apart and the
whole day's allowance three requests deep.

### No quiet window, no daily anchors
Every sibling pauses 00:00–06:00 local. Here the load is already capped by the
budget, so pausing spares no one and discards allowance — with a capacity of
three, a six-hour pause fills the bucket in ninety minutes and throws the rest
away. UPS also scans overnight. `_in_quiet_window` and `_next_anchor` do not
exist.

### Two status vocabularies
`raw_status` carries a `milestoneName.nameKey` where a scan has one and its
`actCode` otherwise. Most scans carry no milestone at all — 34 of 37 on a real
parcel — so keying history on the nameKey alone left the great majority of
entries empty. Consumers tell them apart by shape: nameKeys are `cms.stapp.*`,
scan codes are two characters.

This is safe only because **a history entry's status is display-only**.
Nothing derives the parcel's own status, `delivered` or `delivered_at` from it.

### `raw` is a curated subset
A divergence from the suite contract, which publishes the full payload. UPS
answers with ~126 top-level fields and 38 KB per parcel, most of it the track
page's own furniture; published whole, one `ups_parcel_status_changed` event
went past the recorder's 32 KB event-data limit and was dropped from the
database. `PUBLISHED_RAW_FIELDS` is a size decision, not a privacy one —
`diagnostics.py`'s `TO_REDACT` still does the privacy work.

The trim happens at **publish time only**: the coordinator caches the entry UPS
sent, so widening the list later costs no request and `history` still rebuilds
from cache after a restart.

## Persistence

`helpers.storage.Store` at `ups.cache.<entry_id>`, debounced by
`STORE_SAVE_DELAY_SECONDS`:

```python
{
    "last_fetch_utc": float | None,
    "raw_cache": {tracking_code: trackDetails_entry},
    "delivered_codes": [tracking_code],
    "attempted_codes": [tracking_code],
    "consecutive_failures": int,
    "standdown_until_utc": float | None,
    "last_fetch_by_code": {tracking_code: float},
    "budget": {"tokens": float, "updated_utc": float},
}
```

All wall-clock, so it survives a restart. A backwards clock jump (a Pi with no
RTC syncing after boot) re-anchors rather than banking the skew as credit.

## Bus events

Fired by `_fire_change_events()`, silent on the very first refresh. Every
payload is the normalised parcel plus `device_id`.

| Event | When |
|---|---|
| `ups_parcel_registered` | a new barcode that is not already delivered |
| `ups_parcel_status_changed` | any status change except the hop to delivered |
| `ups_parcel_delivered` | the hop **to** delivered (never both) |
| `ups_parcel_delivery_time_changed` | a `planned_*` becomes non-null and differs |

`device_trigger.py` exposes all four as device automation triggers, filtered on
the hub's `device_id`.

## Sensor unique ID patterns

| Sensor class | Unique ID pattern |
|---|---|
| `UPSIncomingParcelsSensor` | `{entry_id}_incoming_parcels` |
| `UPSParcelSensor` | `{entry_id}_{barcode}` |
| `UPSNextDeliverySensor` | `{entry_id}_next_delivery` |
| `UPSAwaitingPickupSensor` | `{entry_id}_awaiting_pickup` |
| `UPSDeliveredParcelsSensor` | `{entry_id}_delivered_parcels` |
| `UPSLastUpdateSensor` | `{entry_id}_last_update` |
| `UPSDeliveriesCalendar` | `{entry_id}_deliveries` |

The setup-time stale-entity sweep treats any `sensor` entity whose unique_id
starts with `{entry_id}_` and is **not** in `non_parcel_unique_ids` as a
per-parcel sensor. Every new non-parcel sensor must be added to that set.

## Adding a status mapping

1. Confirm the key on a real payload, and record it in
   `carrier-research/ups/api/tracking.md`.
2. Add it to `_STATUS_MAP` (a `nameKey`) or `_ACTIVITY_CODE_MAP` (an
   `actCode`) — the two are not interchangeable.
3. Add a test asserting the canonical status, and say in the docstring what
   evidence backs it.
4. If it makes a new `ParcelStatus` reachable, check whether `CAPABILITIES` or
   a sensor needs to follow.
