# Working in this repository

Home Assistant custom integration for **UPS** parcel tracking. Distributed via
HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
generated from `ha-carrier-template`. **Silver** quality tier. No DTO layer.

Three places hold the knowledge, and they do not overlap:

| What | Where |
|---|---|
| How this integration is built, and why it is built that way | [`ARCHITECTURE.md`](ARCHITECTURE.md) — read it before changing the coordinator, the budget or the status maps |
| Endpoint mechanics, measurements, status vocabularies | `carrier-research/ups/api/tracking.md` (private repo) — **never** duplicated into this repo |
| Suite-wide conventions | [`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md) |

This file is the short list of things an agent must not get wrong.

## Shared conventions — fetch when relevant

Don't fetch `CONVENTIONS.md` every session — fetch it **before** you act in one
of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the docs site's comparison table, so a stale value is a wrong claim on the website |
| ship anything while below 1.0.0 | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **Setup runs before `async_forward_entry_setups`, never in a platform** —
  from a forwarded platform HA can't catch `ConfigEntryNotReady`. Runtime-only;
  tests don't catch a regression.
- **The stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — add every new non-parcel sensor's unique_id.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove`; self-removal races and leaves ghosts.
- **Say "pickup point"**, not "ServicePoint"/"parcel shop"/"locker".

## The one fact everything here follows from

**UPS grants a request budget per IP address, and it is small.** An unvalidated
session is answered **three times** from a residential address and then every
further request *hangs* — no 429, no 403, no latency ramp, just silence until
the address has rested. The grace refills roughly one request per hour, and
draining it costs a further ~2 h penalty.

Three things were tested and ruled out — **do not re-propose them**: a fresh
cookie jar grants no new budget; a different User-Agent/platform/Chrome version
grants no new budget; and commercial VPN exits are refused outright (zero
answered across four Proton exits), so a VPN is strictly worse than the home
line. Only a sensor-validated browser session escapes the budget, and obtaining
one means running Akamai's sensor JS, which this integration does not do.

Everything in [`ARCHITECTURE.md`](ARCHITECTURE.md#key-design-decisions) — the
budget, the one-request-per-cycle queue, the HTTP-free setup, the missing
refresh button, the absent quiet window — exists because of this. **A future
template-drift pass will flag all of it as regressed. It is not.** Do not
"restore" any of it without new evidence in `carrier-research/ups/`.

## Do not refactor away

**Client identity is fixed, not randomised.** `_CHROME_USER_AGENT`, `sec-ch-ua`
and `sec-ch-ua-platform` stay as they are. Only that one combination is
evidenced to work, and the guard scores *consistency* — varying it per request
breaks the very thing that satisfies the check. Per-install variation might
solve the fleet-fingerprint problem but is unmeasured; it needs a probe from a
rested IP, not a guess.

**`sec-ch-ua` is a live maintenance tripwire.** It pins a Chrome major version.
If lookups start hanging while a sibling, unguarded endpoint still answers,
bump the version triplet and `_CHROME_USER_AGENT` **together** in `api.py`
before suspecting anything else.

**The four `GUARD_HEADERS` are load-bearing together.** Dropping any one makes
every lookup hang until the timeout, with nothing to log or parse. Do not
"clean up" this header set, and do not collapse `NAVIGATION_HEADERS` and
`FETCH_HEADERS` into one dict — a browser's page navigation and its own XHR
call do not look the same.

**The bootstrap GET goes to the API host, never to `www.ups.com/track`.** The
public page answers some clients with Akamai's sensor challenge instead of the
page: a `200` in a quarter of a second carrying only bot-manager cookies. No
CSRF cookie means no header, which means the lookup cannot even be attempted —
the integration is dead on that host with nothing wrong in its own code. The
cause is host-level (the OpenSSL build's ClientHello is the leading candidate)
and not fixable from here; the API host does not apply the same challenge. Do
not "tidy" this back to the public page.

**Do not enable HTTP/2.** `webapis.ups.com` fails the stream on HTTP/2.
`aiohttp` speaks 1.1 by default, so this needs no handling — but do not reach
for it to fix a future hang.

**Do not extend `_ACTIVITY_CODE_MAP` from the public UPS status-code table.**
The 500+ entry Developer-Kit table (mirrored by RocketShipIt) is a *different
vocabulary* sharing this one's two-letter shape: it reads `DP` as an invoice
piece-count mismatch and `AR` as a refused delivery, where the scans themselves
say "Departed from Facility" and "Arrived at Facility". Only its customs codes
line up, which on an international parcel is exactly the coincidence that makes
it look usable. Checked against a real payload and rejected.

**Do not derive parcel state from a history entry's status.** Mapping scan
codes at all is only acceptable because history is display-only. If
`coordinator.py` ever starts reading it, revisit the whole
`_ACTIVITY_CODE_MAP` decision first.

**A test about queue order must take the `real_budget` fixture.** The default
fixture shrinks a refill to one second, which makes every code maximally
overdue at once and hands the decision back to the status ranking — so a
starvation test silently passes for the wrong reason.

## Evidence tiers in the status maps — know which one you are reading

- **Observed on the wire.** 14 `nameKey` values and 42 of the 51 `actCode`
  values, the latter with the exact `activityScan` sentence they were mapped
  from. Recorded in `carrier-research/ups/api/tracking.md`.
- **UPS's own translation bundle.** Roughly thirty further `nameKey` entries —
  including *every* `at_pickup_point` key. The bundle establishes what a key
  means, not that this endpoint emits it. Its URL and extraction date are **not
  recorded anywhere**; that is an open gap.
- **`dropoffAccessPoint` is read as `in_transit` on medium confidence.** It
  could be a recipient-side Access Point drop instead. `in_transit` is the safe
  way to be wrong: `at_pickup_point` would tell a user to collect a parcel that
  has not arrived.

An unmapped `nameKey` falls through to `unknown` with a one-shot warning; an
unmapped `actCode` leaves the history entry statusless with its own one-shot
warning, prefixed `actCode=` so a report says which vocabulary it found. Four
codes (`CG`, `S7`, `ZO`, `EU`) map to `None` deliberately and stay **silent** —
sending a user to open an issue about a code we chose not to map wastes their
time.

**Mapping rule for ambiguous codes:** one code can carry several alternative
sentences, so map on what emitting it at all implies, not on which sentence
came back. A code UPS reaches for only around an exception is `problem` even
where the sentence reports the resolution — otherwise a history that hides the
resolved half leaves a lone unresolved entry reading as if it came from nowhere.

## Logging levels are chosen for a post-mortem, not a debug session

Nobody has debug logging on *before* the throttle bites. So: anything you would
need to explain a throttle after the fact must be visible without having
prepared for it.

- **`warning` — a slow request** (`SLOW_REQUEST_SECONDS`, 8 s) on a lookup that
  still succeeded. **It is not early warning of anything.** Measured, the close
  is abrupt: 1.87 s and then a timeout, no ramp. Keep it as a transport-health
  signal; never wire anything to it as though it were advance notice. **This
  carrier gives none.**
- **`info` — the three state changes a user would otherwise experience as a
  hang.** Recovery after N failed cycles; a 401 re-bootstrap (it silently
  doubles a lookup's request count); polling suspending itself entirely. That
  last one happened in practice and must never be silent again.
- **`debug` — the per-cycle decision line** (tracked / fetching / skipped /
  budget left). Every confusing behaviour so far — "3 codes, 1 parcel", the
  retry storm, the starving queue — was one of those numbers differing from
  what the user expected.

**Diagnostics carry the ramp.** `recent_fetches` keeps the last
`RECENT_FETCH_HISTORY` outcomes. Diagnostics get pasted into public issues, so
over-redact: `TO_REDACT` covers the payload, and the coordinator scrubs the
tracking code out of each recorded error string by hand, because that field is
the one `async_redact_data` cannot reach.

## Options and reloads

The options flow is exactly `Parcels` (one editable multi-code list) and
`Settings` (a flat form). Changes apply live via an update listener calling
`async_request_refresh()` — this is an account-less carrier, so it does **not**
use `async_schedule_reload`, and combining a listener with a reload-on-update
flow is deprecated (an error in HA 2026.12+).

**Tracking-code validation is deliberately permissive.** UPS tracks Mail
Innovations, InfoNotice and reference numbers through the same field as 1Z
numbers, and the site forwards whatever a user pastes. Accept any non-empty,
sane-length code; never enforce the 1Z checksum client-side. The endpoint's own
`errorCode: "504"` is the format-rejection signal.

## Running tests

```
../.suite-venv/bin/python -m pytest tests/ --cov=custom_components.ups
../.suite-venv/bin/python -m ruff check custom_components tests
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README, `ARCHITECTURE.md` and this file
in the same commit; API mechanics live in `carrier-research/ups/api/`, never
here.
