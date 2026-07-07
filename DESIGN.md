# Aeolus: Design and Architecture

The design rationale behind Aeolus. If the README tells you *how* to run it,
this tells you *why* it is built the way it is. Data-source facts were verified
against live sources, not memory.

## Goal

A self-hosted replacement for a commercial weather app: current conditions,
48h hourly, 10-day daily, minute-scale precip nowcast, animated radar, and
severe-weather alerts. Phone-first installable PWA, a JSON API for other home
apps, and optional Slack notifications. Free data sources only.

Core principle: **the cache is the product.** Consumers only ever read the
local store. Upstreams are unreliable commodities; when one dies, Aeolus serves
last-known-good with a staleness badge, never an error.

## Decisions at a glance

| Concern | Decision | Why |
|---|---|---|
| Forecast primary | Open-Meteo (keyless) | 16-day hourly+daily, 15-min HRRR nowcast for North America, 10k calls/day free, CC-BY caching allowed |
| Forecast secondary | Pirate Weather (key, optional) | Independent AWS infra, HRRR/NBM, Dark Sky schema, true minutely, 10k calls/month |
| Alerts | NWS api.weather.gov, by county zone | Authoritative; the county query catches both county and zone products |
| Alerts redundancy | IEM `sbw.geojson` watchdog | Independent infrastructure; the legacy NWS CAP feeds are dead and the ATOM feed is the same infra, not redundancy |
| Radar tiles | IEM NEXRAD N0Q timestamped tiles | Public domain, 5-min cadence, immutable frame URLs; nowCOAST WMS (MRMS) as fallback |
| Nowcast | Open-Meteo `minutely_15` (HRRR-native) | 15-min buckets answer "rain in the next hour?"; optional Pirate Weather minutely overlay |
| Base map | Self-hosted Protomaps pmtiles extract | Single file, MapLibre GL; zero third-party runtime dependency for the PWA |
| Daily horizon | 10 days in UI, days 8-10 badged low-confidence | Daily forecast skill collapses around day 8; days 11-16 are trend noise, omitted |
| Stack | Flask + SQLite + gunicorn on python:3.12-slim | Small, boring, self-contained; no external services to run |
| Slack | Direct webhook from the alert poller, two tiers | Warnings immediate (tornado @channel, bypass quiet hours); watches/advisories digest during quiet hours |

**Rejected sources:** Tomorrow.io (ToS prohibits the cache-and-serve
architecture; 5-day daily), OpenWeatherMap (needs billing setup, 8-day daily),
Met.no (fine cross-check but 6-hourly past 60h, Nordics-only nowcast),
Visual Crossing (solid, but key required + unverifiable model provenance),
RainViewer (in wind-down; nowcast/satellite gone, tile URLs 410'd).

## Architecture

One process: a Flask app served by gunicorn, a SQLite database, and background
poller threads. The pollers run as daemon threads inside exactly one gunicorn
worker, elected by a non-blocking `fcntl.flock` on a lock file next to the DB,
so scaling workers never doubles the polling.

```
 Open-Meteo ----+     +---------------- aeolus (one container) ----------------+
 Pirate Weather-+---->|  pollers: forecast(1h) nowcast(5m) alerts(60/30s)      |
 NWS alerts ----+     |           radar-frames(90s, warms your bbox tiles)     |
 IEM radar -----+     |  store:   SQLite (forecasts, observations, alerts,     |
 IEM sbw watchdog     |           radar frames, locations)                     |
                      |  serve:   /v1/* JSON API + PWA + radar tile cache       |
                      |           /metrics (Prometheus) + /healthz             |
                      +----------------------+----------------+----------------+
                                             |                |
                                        your reverse    Slack webhook
                                        proxy / LAN      (optional)
```

### Store (SQLite, WAL, on a local volume — never a network share)

- `forecasts(source, fetched_at, kind[hourly|daily|minutely15], location_id, ts, payload_json)` — last N runs kept per source for spread display and postmortems
- `observations(source[station|model], station_id, ts, temp, rh, pressure, wind_speed, wind_dir, gust, rain_rate, rain_counter, uv, lux)` — for an optional local station; rain stored as a raw counter, deltas computed server-side with reset handling
- `alerts(event_key, message_id, vtec, event, severity, ..., ugc, geometry, affects_point, last_notified_state)`
- `radar_frames(layer_name, valid_utc, fetched_at, source)` — self-accumulated history (IEM only advertises the current frame)
- `locations(id, name, lat, lon, is_default)` — the configured default plus any saved places

### API surface

Every payload carries `source`, `fetched_at`, and `stale: bool` so UIs can badge
honestly. Serve last-known-good always; 503 only before the first successful
poll. Full endpoint and schema tables are in the README.

## Data source details

**Open-Meteo** (primary): free, no key, ~10,000 calls/day (household polling
uses a few percent). Hourly and daily to 16 days; `minutely_15` is real HRRR
sub-hourly for North America. CC-BY 4.0, caching explicitly allowed. Reliability
is community-run: documented regional reachability incidents (whole regions or
IP ranges losing access for periods). The architecture treats it as expendable.

**Pirate Weather** (secondary/cross-check, optional): free 10k calls/month with
a key, HRRR+NBM+GFS, Dark Sky schema, a minutely block. The point is that its
infrastructure (AWS) is independent of Open-Meteo. Empty key = poller off.

**NWS api.weather.gov** (alerts source of truth): free, no key, asks for an
identifying User-Agent with a real contact. Documented polling floor: no more
than every 30s. Known 503 habit and multi-day degradations; no SLA — which is
exactly why the IEM watchdog exists.

## Radar

- Poll the IEM catalog (`mesonet.agron.iastate.edu`) every `RADAR_INTERVAL`s.
  On a new frame, warm the timestamped layer for a small tile block around your
  location at `RADAR_WARM_ZOOMS`, and append it to the local frame history (the
  catalog only advertises the current frame, so history is self-accumulated on
  the 5-minute grid). Backfill fills gaps on startup so the loop is complete.
- Serve tiles through the app's disk proxy-cache under
  `Cache-Control: public, max-age=31536000, immutable` (timestamped URL =
  immutable content). The PWA service worker then makes animation scrubbing free
  on cell data.
- Fallback: when IEM goes frame-silent, switch to whole-bbox nowCOAST WMS
  (MRMS) renders. Both render MRMS-derived reflectivity, so the swap is
  cosmetic. Sourcing drops back to IEM the moment frames flow again.
- Base map: a self-hosted Protomaps pmtiles extract of your region (range-read
  out of the free daily planet build; no full-planet download). Optional — the
  radar still works without it, the map just shows a notice.

## Alerts pipeline

- Poll `/alerts/active?zone=<county>&status=actual` every `ALERTS_INTERVAL`s;
  tighten to the 30s floor while any convective/tornado watch is active.
  Exponential backoff on errors plus a staleness alarm.
- Dedupe by event, not by message: `properties.id` identifies a *message* (every
  update gets a new id). The event key is the VTEC `office.phen.sig.ETN.year`,
  with a fallback chain references → expiredReferences → bare id (a live VTEC
  continuation can arrive as `messageType=Alert` with empty references;
  non-VTEC products fragment without the fallback).
- Annotate, don't filter: `affects_point` is point-in-polygon on the alert
  geometry, else UGC containment — so the UI distinguishes "in your county" from
  "over your location" without hiding near-misses.
- Watchdog: check IEM `sbw.geojson` (true second infrastructure); an active
  TO.W/SV.W/FF.W polygon over your location that the primary cache lacks, or a
  primary poller silent past its alarm, fires the watchdog path.
- Slack tiers: Tier 1 (TO.W, SV.W, FF.W, anything Extreme) posts immediately,
  bypasses quiet hours, `@channel` on Tornado Warning. Tier 2 (watches/
  advisories) respects quiet hours and batches into a morning digest. Re-notify
  within an event only on severity escalation or an `ends` extension >30 min;
  all-clear on tier-1 cancel/expiry.
- **Honest posture:** this is convenience-grade. WEA and NOAA weather radio
  remain the life-safety backstop, and the UI footer says so.

## Optional local station

The schema and the `/v1/current` station branch are already in place for an
optional local weather station (e.g. a LoRaWAN sensor decoded into the
`observations` table with `source=station`). When a station observation is
fresher than 15 minutes it wins the "now" tile; the model fills what it lacks.
No station is required — without one, the model serves everything.

## Roadmap

1. **Core** (shipped): pollers, SQLite store, `/v1` API, PWA (Now/Hourly/
   Daily/Alerts tabs, with the precip nowcast inside the Now view), NWS alerts
   + Slack tiers + IEM watchdog, Prometheus `/metrics`.
2. **Radar** (shipped backend): IEM frame poller + tile proxy-cache, pmtiles
   basemap, nowCOAST fallback, alert-polygon overlay.
3. **Station** (optional): local station ingest, per-variable delta chips.
