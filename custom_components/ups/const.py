"""Constants for the UPS parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "ups"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


# No BUTTON platform: the template's manual-refresh button is deliberately
# absent here. See this repo's CLAUDE.md — this carrier answers a residential
# address about three times before it stops answering at all, so a
# user-triggerable full-fleet poll would spend the whole day's allowance on one
# press.
PLATFORMS = [Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Trimmed to what has actually been observed on the wire. Six real parcels
# across the full range of states — in transit, in customs, under
# investigation, delivered, returned to sender — and not one of them carried
# any of these. The strongest case is an international delivery that passed
# customs, incurred import duties, was held three times and had its address
# corrected, and still carried none:
#
# - "weight" is NOT declared: additionalInformation.weight was "" (empty) on
#   every one of them. normalize_parcel still converts it when present — the
#   field is real and documented, just never seen populated yet.
# - "dimensions" is NOT declared: no dimension field exists in the payload.
# - "delivery_window" is NOT declared: planned_from/planned_to are held at
#   None until an in-flight parcel shows which ETA field (sdd/sdst/sdt/
#   scheduledDeliveryDateDetail) actually populates. Three of the six were
#   in flight and none carried one, so this is no longer "not seen yet".
# - "pickup_point" is NOT declared: upsAccessPoint has only ever been null.
#
# Revisit each only on a payload that actually carries the field — never guess
# ahead of what's evidenced.
CAPABILITIES = frozenset({"url", "history"})

# If this carrier ever grows a second backend with a genuinely different
# payload shape (a country-specific API, not just a config option), replace
# the single CAPABILITIES above with a CAPABILITIES_BY_VARIANT dict instead:
#
#   CAPABILITIES_BY_VARIANT = {
#       "Germany": frozenset({"pickup_point", "url", "history"}),
#       "Other": frozenset({"weight", "dimensions", "delivery_window",
#                            "pickup_point", "url", "history"}),
#   }
#
# Key order is display order on the docs site's comparison table; label each
# key exactly as the carrier's own country/backend selector does. The docs
# site's generator accepts either shape — don't declare both. Do not add this
# preemptively: a single-backend carrier (the common case) keeps the flat
# CAPABILITIES above.

# Two calls per lookup. No API key: the cookie bootstrap plus a
# double-submit CSRF header is the entire auth model.
#
# 1) BOOTSTRAP_URL: plain GET, collects the Akamai/session cookies and the
#    X-XSRF-TOKEN-ST cookie the POST's X-XSRF-TOKEN header copies verbatim.
#    It points at the API host, not at the public track page, and that is
#    load-bearing: www.ups.com answers some clients with Akamai's sensor
#    challenge instead of the page — a 200 in a quarter of a second carrying
#    only bot-manager cookies, from which no CSRF header can be built and the
#    lookup can never even be attempted. Observed on a Home Assistant install
#    four times across five hours while a second machine on the same address,
#    same headers, was answered normally throughout. The API host does not do
#    this: it answered that same install with honest 401s in the same period.
#    A GET to the endpoint below (it only accepts POST) falls through to the
#    SPA shell and seeds both application cookies, so the challenged page is
#    not needed at all.
# 2) TRACKING_API_URL: POST with the four Akamai guard headers
#    (api.py's GUARD_HEADERS) plus the CSRF header. Response is a 200
#    envelope even for a not-found number (branch on
#    trackDetails[0].errorCode, never on HTTP status alone) — a missing/blank
#    CSRF token is a clean 401; a header-guard failure is a silent hang, not
#    an HTTP error.
BOOTSTRAP_URL = "https://webapis.ups.com/track/api/Track/GetStatus?loc={locale}"
TRACKING_API_URL = "https://webapis.ups.com/track/api/Track/GetStatus?loc={locale}"
TRACKING_URL = "https://www.ups.com/track?loc={locale}&tracknum={tracking_code}"

# The XSRF cookie the bootstrap sets, and the header its value is copied into
# verbatim (Angular's stock double-submit interceptor — nothing is derived or
# signed).
COOKIE_XSRF = "X-XSRF-TOKEN-ST"
HEADER_XSRF = "X-XSRF-TOKEN"

# loc follows the user's HA locale where a UPS locale is known to exist
# (en_NL confirmed); anything else falls back to en_US rather than guessing at
# an unconfirmed combination. It affects localised text only — the nameKey
# vocabulary parcels.py maps on is locale-independent.
DEFAULT_LOCALE = "en_US"
KNOWN_LOCALES = frozenset({"en_NL"})

# Tracked parcels live in the config entry options as a list of
# ``{tracking_code}`` dicts — this carrier has no account or parcel feed, so the
# user enters the codes themselves. Kept as dicts so future per-parcel fields
# slot in without an options migration.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option (see scaffold/CLAUDE.md's "Dynamic polling"
# section for the full algorithm and the reasoning behind it).
#
# **UPS has no quiet window.** Every sibling stops polling 00:00-06:00 local
# and catches up on two daily anchors, because there is no point loading a
# carrier's servers overnight. That reasoning does not survive here: the load
# is already capped at roughly one request every 75 minutes by the address
# budget, so a quiet window does not reduce anything — it throws away a
# quarter of the day's allowance. Worse, the budget tops out at three, so a
# six-hour pause fills the bucket in the first ninety minutes and discards
# every token after that. And UPS scans overnight, so the parcels users most
# want fresh at breakfast are exactly the ones moving while polling is off.

# Tier thresholds (minutes). These no longer set the cadence — the request
# budget does — and survive as the answer to "is anything worth asking about
# at all" (_hottest_tier_minutes returning None suspends polling) and as a
# diagnostics field. Hot = at least one tracked,
# not-yet-delivered parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of
# its planned_from (or has no planned_from at all); mid = anything else still
# in flight (registered, in_transit, at_pickup_point, unknown, problem,
# returning).
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Cold tier: every tracked parcel is sitting at a pickup point. Nothing moves
# until a human walks in and collects it. Diagnostics only, like the two
# above — the budget sets the cadence here, so this saves no request; what
# keeps a collected-but-uncollected parcel from crowding out one still moving
# is _QUEUE_PRIORITY, which ranks at_pickup_point last. Only reachable since
# the nameKey map gained its at_pickup_point keys.
COLD_INTERVAL_MINUTES = 240

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# On top of the stable stagger, a fresh random offset on every computed
# interval. The stagger alone spreads installs apart but leaves a single
# install's request timestamps a perfect arithmetic sequence, which is the one
# thing a real browser never produces. All jitter here is additive — it only
# ever delays a request, never brings one forward, so no computed cadence can
# come out faster than the budget allows.
JITTER_FRACTION = 0.15

# Shortest gap between two poll cycles, regardless of what asked for the
# refresh. The budget already makes an over-eager caller harmless — it simply
# finds no token — but every manual path into the coordinator
# (``track_parcel``, an options change, ``homeassistant.update_entity`` on any
# UPS entity) funnels through ``async_request_refresh()``, and HA's own 10 s
# debouncer would otherwise let those spin the cycle far faster than anything
# can come of it.
#
# It is also what spreads a bulk add: three codes pasted into the options form
# in one go are three cycles, not one, so they arrive minutes apart instead of
# back to back. Five minutes is long enough that the address never sees a
# burst, short enough that a user watching the page sees all three fill in.
MIN_CYCLE_GAP_SECONDS = 300

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.cache"

# The measured grace: an unvalidated session is answered three times from a
# residential address and then hangs until the address has rested. Measured
# 2026-09-05/06 across a rested address, four VPN exits and two Tor exits;
# rest does not raise it and a fresh cookie jar does not reset it.
REQUEST_BUDGET_CAPACITY = 3

# Seconds of rest per request earned back. Measured 2026-09-06 by asking once
# every 30 minutes for ten hours: eight answered, and the gaps between them
# were 60, 60, 60, 60, 60, 121, 60 minutes. The beat is an hour — but one beat
# in seven was simply skipped, and a request that arrives before its token
# does not merely fail, it hangs and costs a two-hour stand-down on top.
#
# So this sits deliberately *past* the beat rather than on it. Asking at 60
# nets six requests per nine hours once a skipped beat is paid for; asking at
# 75 nets eight per ten. Waiting is both the safer and the more productive
# choice, which is why the extra quarter of an hour is not caution but
# arithmetic.
REQUEST_BUDGET_REFILL_SECONDS = 4500

# Number of tracked codes above which the rotation gets slow enough to be
# worth warning about: with one request per refill interval shared between
# them, this many parcels means over half a day between updates for each.
TRACKED_CODE_SOFT_LIMIT = 10

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default even when — as here — the timeline arrives in
# the same response and costs no extra request: it is a large attribute, and on
# carriers that need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
