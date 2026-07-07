# Aeolus

Self-hosted weather service. Current conditions, 48h hourly, 10-day daily,
15-minute precip nowcast, animated NEXRAD radar, and NWS severe-weather alerts
with tiered Slack notifications. Installable phone-first PWA, a JSON API for
other apps, and Prometheus metrics. One container, free data sources, no
account required.

Core principle: **the cache is the product.** Consumers read only the local
SQLite store. When an upstream dies, Aeolus serves last-known-good with a
`stale` badge, never an error.

> Honest posture: alerting here is convenience-grade. WEA and NOAA weather radio
> remain the life-safety backstop.

Data sources: Open-Meteo (forecast + nowcast), Pirate Weather (optional
secondary), NWS api.weather.gov (alerts), IEM (alert watchdog + NEXRAD radar),
NOAA nowCOAST (radar fallback). Radar and NWS alerts are US-only; forecast and
nowcast are global.

## Quick start

Requires Docker with the Compose plugin (`docker compose`, v2).

```sh
git clone https://github.com/Heretikio/aeolus.git
cd aeolus
cp .env.example .env      # then edit .env — at minimum set your location
docker compose up -d --build
```

Open <http://localhost:8080>. Forecast data lands within a few seconds of
startup; radar frames fill in over the first minute.

- Change the published port with `PORT=` in `.env`.
- Logs: `docker compose logs -f aeolus`
- Update: `git pull && docker compose up -d --build`
- Stop: `docker compose down` (data persists in the `aeolus-data` volume)

Data (SQLite DB, radar cache, optional basemap) lives in the named Docker volume
`aeolus-data`, owned by the image's non-root uid `10001`. A named volume inherits
that ownership automatically, which is why the compose file uses one rather than
a host bind mount.

## Set your location

Edit these in `.env`. The shipped defaults are an example (Norman, OK).

```ini
LAT=35.22
LON=-97.44
LOCATION_NAME=Home
NWS_ZONE=OKC027            # your county UGC (alerts are polled by county)
NWS_UGC=OKC027,OKZ029      # codes counted as "your area" for the affects-you badge
TZ=America/Chicago         # IANA timezone
RADAR_BBOX=-103.5,30.5,-91.5,39.5   # west,south,east,north — should contain LAT/LON
```

- **LAT/LON**: from <https://www.latlong.net> or Google Maps "What's here?".
- **NWS_ZONE / NWS_UGC** (US only): enter your address at
  <https://forecast.weather.gov>; the page shows your County code (e.g.
  `OKC027`) and Forecast Zone code (e.g. `OKZ029`). `NWS_ZONE` is the county;
  `NWS_UGC` is usually county + forecast zone. Full lookup table: the NWS
  [County-Zone Correlation file](https://www.weather.gov/gis/ZoneCounty) maps
  every county (FIPS) to its UGC and forecast zone.
- **TZ**: an IANA timezone name like `America/Chicago`. Pick yours from the
  [tz database list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).
- **RADAR_BBOX**: the map extent and radar region. Widen or shift it around your
  point.
- **Outside the US**: forecast and nowcast work globally. NWS alerts and NEXRAD
  radar are US-only, so set `ALERTS_ENABLED=0` — the default `NWS_ZONE` is a real
  US county, and leaving it set would surface *that* county's alerts. Radar can
  be left on (it just won't find frames for a non-US area) or set
  `RADAR_ENABLED=0`.

Restart after editing: `docker compose up -d`.

## Configuration

Everything is environment-driven with defaults; see [.env.example](.env.example)
for the full annotated list. The variables you are most likely to touch:

| Variable | Default | Purpose |
|---|---|---|
| `LAT` / `LON` | `35.22` / `-97.44` | Forecast/radar point |
| `LOCATION_NAME` | `Home` | Name shown in the location picker |
| `NWS_ZONE` | `OKC027` | County UGC polled for alerts (US) |
| `NWS_UGC` | `OKC027,OKZ029` | UGC codes counted as "your area" |
| `ALERTS_ENABLED` | `1` | `0` disables NWS alerts + watchdog (set `0` outside the US) |
| `TZ` | `America/Chicago` | Local timezone for timestamps and quiet hours |
| `USER_AGENT` | `aeolus/1.0 (you@example.com)` | Identifying UA for NWS; set a real contact |
| `PORT` | `8080` | Host port to publish (container always listens on 8080) |
| `PIRATE_WEATHER_KEY` | empty | Optional secondary forecast; empty = poller off |
| `SLACK_WEBHOOK_URL` | empty | Alert notifications; empty = DRY-RUN log only |
| `QUIET_HOURS_START` / `_END` | `22` / `7` | Tier-2 digest window (local hours) |
| `RADAR_ENABLED` | `1` | `0` disables the radar frame poller |
| `RADAR_BBOX` | `-103.5,30.5,-91.5,39.5` | Radar/map extent `W,S,E,N` |

Poll cadences, backoff, burn-window thresholds, and radar cache limits are all
tunable — see `.env.example`. Alert cadences are clamped to the NWS-documented
30-second floor.

## Optional integrations

**Pirate Weather** (secondary forecast, independent infrastructure): get a free
key at <https://pirateweather.net>, set `PIRATE_WEATHER_KEY`. When the primary
goes stale and Pirate is fresh, Aeolus serves Pirate automatically.

**Slack alerts**: create an [Incoming Webhook](https://api.slack.com/messaging/webhooks)
and set `SLACK_WEBHOOK_URL`. Empty = alerts are logged as `DRY-RUN` so you can
watch the logs before wiring Slack.

- Tier 1 (Tornado / Severe Thunderstorm / Flash Flood Warning, anything
  Extreme): immediate, bypasses quiet hours, `@channel` only for Tornado
  Warning. All-clear on cancel/expiry.
- Tier 2 (watches/advisories): immediate by day; during quiet hours they queue
  into a morning digest.
- Watchdog: an independent IEM check catches a warning polygon over your
  location that the primary NWS feed lacks, or a stalled primary poller.

**Radar basemap** (optional): the radar map works without it (it shows a
"basemap not installed" notice). To render a real street/terrain map, generate a
self-hosted Protomaps extract into the data volume:

```sh
RADAR_BBOX=-103.5,30.5,-91.5,39.5 ./scripts/make-basemap.sh
```

Run it on the Docker host (needs Docker + network). Set `RADAR_BBOX` to match
your server's. It range-reads only your region out of the daily planet build
(a few hundred MB, minutes). Re-run quarterly. The `/basemap.pmtiles` endpoint
serves it immediately, no restart.

## API

Every payload carries `source`, `fetched_at`, and `stale`. Endpoints serve
last-known-good always; 503 happens only before the first successful poll.
Forecast endpoints accept `loc=<location id>` (missing/invalid falls back to the
default location; an unknown integer is 404).

| Endpoint | Params | Returns |
|---|---|---|
| `GET /v1/current` | `loc` | Current conditions |
| `GET /v1/hourly` | `h` (default 48, max 384), `loc` | Hourly forecast from the current hour |
| `GET /v1/daily` | `d` (default 10, max 10), `loc` | Daily forecast from today |
| `GET /v1/nowcast` | `loc` | 15-minute precip steps |
| `GET /v1/alerts` | | Active alerts with `affects_point` annotation |
| `GET /v1/burn` | | Burn-window verdict: `go` / `caution` / `no_burn` |
| `GET /v1/burn/outlook` | | Per-day burn verdicts |
| `GET /v1/locations` | | Saved locations (the configured default is seeded) |
| `GET /v1/radar/frames.json` | | Radar animation frame list, oldest first |
| `GET /v1/radar/tiles/{layer}/{z}/{x}/{y}.png` | | Immutable radar tile proxy-cache |
| `GET /v1/radar/frames/{layer}.png` | | nowCOAST fallback frame image |
| `GET /v1/radar/alerts.geojson` | | Active alert polygons for the map |
| `GET /basemap.pmtiles` | | Self-hosted basemap (HTTP Range honored) |
| `GET /healthz` | | Per-source freshness and poller-leader liveness |
| `GET /metrics` | | Prometheus metrics |

### Canonical schema

Forecast payloads are **source-agnostic**: stored history keeps each upstream's
native fields, and `adapters.py` converts to one canonical schema at serve time.
Open-Meteo (WMO codes, US units) and Pirate Weather (Dark Sky schema) both come
out identical, so consumers never see source-native names. Missing values are
omitted, never invented.

Per-timestep fields (`current`, `hourly`, `nowcast` rows):

| Field | Unit / range |
|---|---|
| `temp_f`, `feels_like_f`, `dew_point_f` | degrees Fahrenheit |
| `humidity_pct`, `cloud_cover_pct` | 0-100 |
| `wind_mph`, `gusts_mph` | miles per hour |
| `wind_dir_deg` | meteorological degrees (direction wind is from) |
| `pressure_mb` | millibars (= hPa), sea level |
| `uv_index` | unitless UV index |
| `precip_prob_pct` | 0-100 |
| `precip_in`, `rain_in`, `snow_in` | inches for the timestep |
| `is_day` | `1` day / `0` night; absent when unknown |
| `condition` | human-readable string (e.g. "Partly cloudy") |
| `condition_code` | normalized enum (below) |

Daily rows add `temp_max_f`, `temp_min_f`, `feels_like_max_f`,
`feels_like_min_f`, `sunrise`, `sunset` (local ISO strings), `uv_index_max`,
`precip_prob_max_pct`, `precip_sum_in`, `wind_max_mph`, `gusts_max_mph`,
`wind_dir_deg`, `condition`, `condition_code`.

`condition_code`: `clear`, `partly_cloudy`, `cloudy`, `fog`, `drizzle`, `rain`,
`freezing_rain`, `sleet`, `snow`, `hail`, `thunderstorm`, `windy`, `tornado`,
`unknown`.

### Burn window

`/v1/burn` answers "can I light a brush pile?" from current conditions plus the
active-alert cache, thresholds env-tunable via the `BURN_*` vars:

- `no_burn` when ANY of: sustained wind ≥ 15 mph, gusts ≥ 25 mph, humidity ≤
  25%, an active Red Flag / Fire Weather Warning, or the combo wind ≥ 10 mph
  AND humidity ≤ 35%.
- `caution` when ANY of (and not `no_burn`): wind 10-15 mph, gusts 20-25 mph,
  humidity 25-40%, an active Fire Weather Watch. Missing wind/humidity can never
  yield `go`; it degrades to `caution`.
- `go` otherwise.

Each trigger produces a human reason (`"gusts 27 mph"`). The next
`BURN_LOOKAHEAD_HOURS` are scanned for a verdict flip (`changes_at`). The
multi-day `/v1/burn/outlook` gives one verdict per day covering a burn's full
~2-day life. Guidance only — county burn bans are not modeled. This feature is
oriented at rural/homestead users; ignore it if you don't burn.

## PWA

Phone-first installable PWA served by the same app: `/` is the shell,
`/manifest.json` the manifest, `/sw.js` the service worker (root scope). Fully
self-contained under `static/`: vanilla JS, hand-rolled inline SVG charts,
system fonts, no CDN. Dark/light themes follow `prefers-color-scheme`.

- Views (bottom tab bar): **Now**, **Hourly** (48h chart + table), **Daily**
  (10-day range bars), **Radar** (MapLibre map + frame scrubber), **Alerts**.
- Every view shows "Updated N min ago"; it turns amber when data is stale.
- Offline: the service worker caches the shell cache-first and `/v1/*`
  network-first with cache fallback, so the app opens on a dead connection
  showing the last data.
- The location picker reads `/v1/locations` and refetches with `loc=`.

## Deploy notes

- **Reverse proxy**: put any HTTPS reverse proxy (Caddy, nginx, Traefik,
  Cloudflare Tunnel, …) in front and point it at the container's port. The PWA
  and service worker assume they own the root path.
- **Metrics**: Prometheus can scrape `/metrics`. Alert rules can ride
  `aeolus_source_stale` and `aeolus_poller_leader` (pair `== 1` checks with
  `absent()` guards). `/healthz` reports per-source freshness and leader
  liveness for the compose healthcheck.
- **Harden the public surface**: `/metrics` and `/healthz` are unauthenticated
  and expose internal operational telemetry (per-source freshness, failure
  counts, radar cache size). Restrict them to your LAN/monitoring host at the
  reverse proxy. The radar tile endpoint (`/v1/radar/tiles/*`) fetches missing
  tiles from IEM on demand; rate-limit it at the proxy so an anonymous client
  cannot drive upstream traffic or cache churn. The tile/frame layer names and
  z/x/y are strictly validated, so this is throughput hardening, not an
  injection or SSRF concern.
- **Scaling**: the pollers run in exactly one gunicorn worker (leader-elected by
  a file lock), so raising worker/thread counts is safe.

## Run without Docker

For development. Python 3.12+.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q                 # offline suite
node scripts/verify_radar_ui.js                      # radar frontend harness
AEOLUS_LIVE=1 .venv/bin/python -m pytest tests/test_live_smoke.py -v  # one real fetch per source
.venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 2 --threads 8 wsgi:app
```

Config comes from the environment (or a `.env` you `export`). See
[.env.example](.env.example).

## How it works

Flask + SQLite (WAL) + gunicorn on `python:3.12-slim`. Background pollers run as
daemon threads inside exactly one gunicorn worker, elected via a non-blocking
`fcntl.flock`. Design rationale, data-source decisions, and alerting semantics
are in [DESIGN.md](DESIGN.md).

## Data sources & attribution

- Forecast/nowcast: [Open-Meteo](https://open-meteo.com) (CC-BY 4.0)
- Secondary forecast: [Pirate Weather](https://pirateweather.net)
- Alerts: [NWS api.weather.gov](https://www.weather.gov/documentation/services-web-api)
- Alert watchdog + NEXRAD radar: [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu)
- Radar fallback: [NOAA nowCOAST](https://nowcoast.noaa.gov) (MRMS)
- Basemap: [Protomaps](https://protomaps.com) / © OpenStreetMap contributors

Vendored frontend assets (MapLibre GL, PMTiles, Protomaps basemap styles/
sprites/fonts) and their licenses are listed in
[static/vendor/LICENSES.md](static/vendor/LICENSES.md).

## License

MIT — see [LICENSE](LICENSE).
