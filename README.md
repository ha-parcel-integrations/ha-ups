# UPS Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-ups.svg)](https://github.com/ha-parcel-integrations/ha-ups/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

> **Pre-1.0.** The status vocabulary is evidence-backed only for a *delivered*
> shipment end-to-end — `at_pickup_point`, `returning` and `problem` have no
> confirmed status key yet, and the ETA window, weight and pickup-point
> fields are held at their empty defaults until an in-flight parcel is
> captured. See [Troubleshooting](#troubleshooting).

A custom Home Assistant integration that tracks your [UPS](https://www.ups.com/track) parcels. No account is needed — you enter the tracking code yourself, just like on the UPS website.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of UPS parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar (currently stays empty — see the pre-1.0 note above; UPS has no confirmed ETA field yet)
- `ups.track_parcel` / `ups.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered)
- Opt-in per-parcel status history
- A diagnostic last-update sensor
- No manual refresh button, deliberately: UPS allows only a few lookups per hour per connection, so a refresh button would spend the whole allowance in one press (see [Polling](#polling))

## Requirements

- Home Assistant 2024.12 or newer
- A UPS parcel and its tracking code (from the shipping
  confirmation email or the missed-delivery card) — no account needed

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-ups` as an **Integration**.
3. Install **UPS** and restart Home Assistant.

### Manual

Copy `custom_components/ups` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → UPS**. There is nothing to fill in: the hub is created immediately (UPS tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`ups.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking code is on your shipping confirmation email or the missed-delivery card.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

### Polling

Polling isn't one of these settings, and there is nothing to configure.

**Expect updates hours apart, not minutes.** UPS allows one home connection
only a handful of tracking lookups per hour, and there is no way to raise
that — no account, no API key, no setting. Ask too often and it simply stops
answering for a couple of hours. So the integration spends what it is given,
carefully:

- **one parcel is refreshed per turn**, roughly once an hour, and your
  parcels take turns. Two parcels means each is refreshed about every two
  hours; ten means about every twelve. The integration warns in the log when
  the list gets long enough for that to be worth knowing;
- **a parcel you just added goes to the front of the queue**, and gets its
  sensor immediately — showing `unknown` until its first update arrives, so
  you can see it was registered even before it has been looked up. Add three
  codes at once and all three appear straight away; they are then looked up a
  few minutes apart rather than together;
- **a delivered parcel is never looked up again.** It stays visible for as
  long as your retention setting says, and costs nothing;
- **nothing can force a burst.** A service call, an options change or
  `homeassistant.update_entity` is served from the last known data when there
  is no allowance left;
- **restarting Home Assistant costs no requests at all.** Parcels are
  published from disk, so restarting repeatedly while you configure things
  doesn't pile anything up;
- parcels waiting at a pickup point are refreshed last — nothing changes
  there until you collect them;
- **polling runs through the night.** Most parcel integrations pause until
  morning; this one doesn't, because UPS scans overnight and the allowance is
  small enough that skipping six hours would throw a quarter of it away.
  Polling does stop entirely once nothing is left to track;
- a run of failures stands down for hours rather than retrying on schedule,
  because that is how long the endpoint takes to answer again.

Slow updates are therefore the carrier's limit, not a fault. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how the pacing works, and
[CLAUDE.md](CLAUDE.md) for the constraints behind it.

## Removal

Standard HA removal applies: **Settings → Devices & Services → UPS → ⋮ → Delete**. Nothing is stored on UPS's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.ups_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.ups_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.ups_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.ups_ready_for_pickup` | Parcels waiting for you at a UPS Access Point, full list under the `parcels` attribute |
| `sensor.ups_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.ups_last_successful_update` | Diagnostic: when UPS was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning | Confirmed for UPS? |
|---|---|---|
| `registered` | Announced / received by UPS | ✅ |
| `in_transit` | In the sorting network | ✅ |
| `out_for_delivery` | With the courier today | ✅ |
| `at_pickup_point` | Waiting for you at a pickup location | ⚠️ mapped from UPS's own translation bundle, not yet seen on the wire |
| `delivered` | Delivered | ✅ |
| `returning` | Going back to the sender | ✅ |
| `problem` | UPS reports an exception | ✅ |
| `unknown` | Not yet scanned, or a status we have not mapped yet | — |

The carrier's own human-readable text is always available as `raw_status`. An
unrecognised status is reported as `unknown` and logs a one-time warning with
a link to open an issue — that's how the ⚠️ rows above get confirmed. A ⚠️ row
means the status key is mapped from UPS's own tracking translation bundle, so
the meaning is right, but no real parcel has yet been observed reporting it.

## Events

The integration fires these on the event bus (also available as device triggers on the UPS device):

| Event | When |
|---|---|
| `ups_parcel_registered` | A new parcel appears in the active list |
| `ups_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `ups_parcel_delivered` | A parcel is delivered |
| `ups_parcel_delivery_time_changed` | The expected delivery window changes — never fires today, since UPS has no confirmed ETA field yet |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `ups.track_parcel` | `tracking_code` | Start tracking a parcel |
| `ups.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.ups: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — UPS has not scanned it yet (the endpoint reports the code as not found until the first scan), or the code is wrong. It will pick up automatically once scanned.
- **A status logs "Unrecognised UPS status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-ups/issues/new) with the logged line so the mapping can be extended. `at_pickup_point`, `returning` and `problem` are the expected first candidates.
- **A poll logs "UPS fetch failed... request timed out"** — UPS's tracking endpoint answers a well-formed request normally, but silently stops responding instead of returning an error when it doesn't like the request. A single stuck parcel is retried on the next poll and does not affect the others. If *every* parcel starts timing out at once and stays that way across several polls, it's more likely the endpoint temporarily throttling this connection than a one-off — wait it out. The integration stands down for two hours automatically, doubling up to six, and will not let anything force extra requests during that window — measured, that is how long UPS takes to start answering again.
- **No delivery window / calendar entries** — UPS has no confirmed ETA field yet; `planned_from`/`planned_to` are held at `None` pre-1.0. See the note at the top of this README.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration uses the same public tracking endpoint as the UPS consumer website. It is not affiliated with, endorsed by, or supported by UPS.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
