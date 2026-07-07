"""Upstream fetchers and normalizers.

Fetchers do the network IO (timeout, identifying User-Agent, raise on HTTP
errors; retry/backoff lives in pollers.py). Normalizers are pure functions
that reshape raw responses into store rows: [(ts, payload_dict), ...] with
ts as a local ISO string in the configured timezone.
"""

from datetime import datetime, timezone

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PIRATE_URL = "https://api.pirateweather.net/forecast/{key}/{lat},{lon}"
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
IEM_SBW_URL = "https://mesonet.agron.iastate.edu/geojson/sbw.geojson"

HOURLY_FIELDS = [
    "temperature_2m", "apparent_temperature", "relative_humidity_2m",
    "dew_point_2m", "precipitation_probability", "precipitation", "rain",
    "showers", "snowfall", "weather_code", "pressure_msl", "surface_pressure",
    "cloud_cover", "visibility", "wind_speed_10m", "wind_direction_10m",
    "wind_gusts_10m", "uv_index", "is_day",
]
DAILY_FIELDS = [
    "weather_code", "temperature_2m_max", "temperature_2m_min",
    "apparent_temperature_max", "apparent_temperature_min", "sunrise",
    "sunset", "uv_index_max", "precipitation_sum", "precipitation_hours",
    "precipitation_probability_max", "wind_speed_10m_max",
    "wind_gusts_10m_max", "wind_direction_10m_dominant",
]
MINUTELY_FIELDS = ["precipitation", "rain", "snowfall", "wind_gusts_10m", "weather_code"]

US_UNITS = {
    "temperature_unit": "fahrenheit",
    "wind_speed_unit": "mph",
    "precipitation_unit": "inch",
}


def _headers(cfg) -> dict:
    return {"User-Agent": cfg.user_agent}


def _redact(cfg, url: str) -> str:
    if cfg.pirate_weather_key:
        url = url.replace(cfg.pirate_weather_key, "***")
    return url


def _request(cfg, url, params=None, headers=None):
    """GET with sanitized failures. requests exceptions embed the full URL
    (the Pirate Weather key rides in the path), so re-raise without chaining
    to keep secrets out of poller tracebacks. Known risk, accepted: timeout
    is per-connect/read, not a total deadline; a byte-trickling upstream can
    stretch one poll beyond it."""
    try:
        resp = requests.get(url, params=params, headers=headers or _headers(cfg),
                            timeout=cfg.http_timeout)
    except requests.RequestException as e:
        raise RuntimeError(
            f"GET {_redact(cfg, url)} failed: {type(e).__name__}") from None
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GET {_redact(cfg, url)} failed: HTTP {resp.status_code}")
    return resp


def _get_json(cfg, url, params=None, headers=None):
    return _request(cfg, url, params, headers).json()


# ---- Open-Meteo (forecast primary) ----

def fetch_open_meteo_forecast(cfg) -> dict:
    """Hourly to 16 days plus daily, US units, local timezone."""
    params = {
        "latitude": cfg.lat,
        "longitude": cfg.lon,
        "hourly": ",".join(HOURLY_FIELDS),
        "daily": ",".join(DAILY_FIELDS),
        "forecast_days": 16,
        "timezone": cfg.tz,
        **US_UNITS,
    }
    return _get_json(cfg, OPEN_METEO_URL, params)


def fetch_open_meteo_nowcast(cfg) -> dict:
    """minutely_15 (HRRR-native sub-hourly), next 6 hours (24 steps)."""
    params = {
        "latitude": cfg.lat,
        "longitude": cfg.lon,
        "minutely_15": ",".join(MINUTELY_FIELDS),
        "forecast_minutely_15": 24,
        "timezone": cfg.tz,
        **US_UNITS,
    }
    return _get_json(cfg, OPEN_METEO_URL, params)


def normalize_block(raw: dict, block_name: str, fields: list) -> list:
    """Zip an Open-Meteo time-series block into per-timestep rows. Guarded
    per field so schema drift (an array shorter than time) drops only that
    field, not the whole poll; nulls are tolerated downstream."""
    block = raw.get(block_name) or {}
    times = block.get("time") or []
    return [
        (t, {f: block[f][i] for f in fields
             if isinstance(block.get(f), list) and i < len(block[f])})
        for i, t in enumerate(times)
    ]


# ---- Pirate Weather (secondary, key required) ----

def fetch_pirate(cfg) -> dict:
    url = PIRATE_URL.format(key=cfg.pirate_weather_key, lat=cfg.lat, lon=cfg.lon)
    return _get_json(cfg, url, {"units": "us", "exclude": "alerts"})


def _pirate_rows(raw: dict, block: str, tzinfo, fmt: str) -> list:
    data = (raw.get(block) or {}).get("data") or []
    rows = []
    for d in data:
        ts = datetime.fromtimestamp(d["time"], tz=timezone.utc).astimezone(tzinfo).strftime(fmt)
        rows.append((ts, {k: v for k, v in d.items() if k != "time"}))
    return rows


def normalize_pirate_hourly(raw: dict, tzinfo) -> list:
    return _pirate_rows(raw, "hourly", tzinfo, "%Y-%m-%dT%H:%M")


def normalize_pirate_daily(raw: dict, tzinfo) -> list:
    return _pirate_rows(raw, "daily", tzinfo, "%Y-%m-%d")


def normalize_pirate_minutely(raw: dict, tzinfo) -> list:
    return _pirate_rows(raw, "minutely", tzinfo, "%Y-%m-%dT%H:%M")


# ---- NWS alerts (source of truth) ----

def fetch_nws_alerts(cfg, store=None):
    """Active alerts for our zone. NWS requires an identifying User-Agent.

    Honors HTTP caching when given a store: replays the last ETag and
    Last-Modified and returns None on 304 (feed unchanged, nothing to
    process; an alert dropping out of the feed changes the body and yields
    a 200, so expiry all-clears are unaffected). Verified live 2026-07-03:
    the endpoint returns a stable weak ETag but currently answers matching
    If-None-Match with 200 anyway; we send the validators regardless (costs
    one header, and the 304 path is ready whenever their CDN honors it)."""
    params = {"zone": cfg.nws_zone, "status": "actual"}
    headers = {"User-Agent": cfg.user_agent, "Accept": "application/geo+json"}
    if store is not None:
        etag = store.meta_get("nws_alerts_etag")
        modified = store.meta_get("nws_alerts_last_modified")
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
    resp = _request(cfg, NWS_ALERTS_URL, params, headers)
    if resp.status_code == 304:
        return None
    if store is not None:
        if resp.headers.get("ETag"):
            store.meta_set("nws_alerts_etag", resp.headers["ETag"])
        if resp.headers.get("Last-Modified"):
            store.meta_set("nws_alerts_last_modified", resp.headers["Last-Modified"])
    return resp.json().get("features") or []


# ---- IEM storm-based warnings (watchdog, true second infrastructure) ----

def fetch_iem_sbw(cfg) -> list:
    raw = _get_json(cfg, IEM_SBW_URL)
    return raw.get("features") or []
