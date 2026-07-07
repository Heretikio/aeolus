"""Serve-time adaptation of source-native forecast rows to the canonical
Aeolus schema.

Stored history stays source-native (whatever the upstream returned); every
/v1 forecast endpoint runs rows through adapt_rows() so consumers only ever
see one schema regardless of which source is serving. That covers old rows
too: a schema change here re-adapts history on the next request, no
migration.

Canonical per-timestep fields (hourly / current / nowcast):
    temp_f, feels_like_f, dew_point_f      degrees Fahrenheit
    humidity_pct, cloud_cover_pct          0-100
    wind_mph, gusts_mph                    miles per hour
    wind_dir_deg                           meteorological degrees (from)
    pressure_mb                            millibars (== hPa), sea level
    uv_index                               unitless UV index
    precip_prob_pct                        0-100
    precip_in, rain_in, snow_in            inches for the timestep
    is_day                                 1 day / 0 night (absent if unknown)
    condition                              human-readable string
    condition_code                         normalized enum, see CONDITION_CODES

Canonical daily fields:
    temp_max_f, temp_min_f, feels_like_max_f, feels_like_min_f
    sunrise, sunset                        local ISO minute strings
    uv_index_max, precip_prob_max_pct, precip_sum_in
    wind_max_mph, gusts_max_mph, wind_dir_deg
    condition, condition_code

Missing or non-numeric source values are dropped, never invented; the
frontend and API consumers treat absent keys as "n/a".

Verified source units (do not change without re-checking real payloads):
- Open-Meteo is *requested* with temperature_unit=fahrenheit,
  wind_speed_unit=mph, precipitation_unit=inch (sources.US_UNITS), so its
  temps/winds/precip are already F/mph/inch; humidity, probabilities and
  cloud cover are 0-100; pressure_msl is hPa; weather_code is WMO.
- Pirate Weather is requested with units=us (Dark Sky schema): temps F,
  winds mph, pressure mb, but humidity/precipProbability/cloudCover are
  0-1 fractions, precipIntensity is inches PER HOUR, and the minutely
  block is 61 one-minute steps. Conditions are icon strings.
"""

from datetime import datetime, timezone

CONDITION_CODES = (
    "clear", "partly_cloudy", "cloudy", "fog", "drizzle", "rain",
    "freezing_rain", "sleet", "snow", "hail", "thunderstorm", "windy",
    "tornado", "unknown",
)

# WMO weather_code (Open-Meteo) -> (label, condition_code)
WMO_CONDITIONS = {
    0: ("Clear", "clear"),
    1: ("Mostly clear", "clear"),
    2: ("Partly cloudy", "partly_cloudy"),
    3: ("Overcast", "cloudy"),
    45: ("Fog", "fog"), 48: ("Icy fog", "fog"),
    51: ("Light drizzle", "drizzle"), 53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "freezing_rain"),
    57: ("Freezing drizzle", "freezing_rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "freezing_rain"), 67: ("Freezing rain", "freezing_rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"), 81: ("Showers", "rain"),
    82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "thunderstorm"),
    96: ("Storm with hail", "hail"), 99: ("Storm with hail", "hail"),
}

# Dark Sky / Pirate Weather icon string -> (fallback label, condition_code)
PIRATE_CONDITIONS = {
    "clear-day": ("Clear", "clear"),
    "clear-night": ("Clear", "clear"),
    "partly-cloudy-day": ("Partly cloudy", "partly_cloudy"),
    "partly-cloudy-night": ("Partly cloudy", "partly_cloudy"),
    "cloudy": ("Cloudy", "cloudy"),
    "fog": ("Fog", "fog"),
    "wind": ("Windy", "windy"),
    "drizzle": ("Drizzle", "drizzle"),
    "rain": ("Rain", "rain"),
    "sleet": ("Sleet", "sleet"),
    "snow": ("Snow", "snow"),
    "hail": ("Hail", "hail"),
    "thunderstorm": ("Thunderstorm", "thunderstorm"),
    "tornado": ("Tornado", "tornado"),
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _pct(v):
    """0-1 fraction -> integer percent."""
    return round(v * 100) if _num(v) else None


def _passthrough(v):
    return v if _num(v) else None


def _local_iso(epoch, tzinfo):
    if not _num(epoch):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(
        tzinfo).strftime("%Y-%m-%dT%H:%M")


# ---- Open-Meteo ----

def _wmo_condition(code):
    """(label, condition_code) for a WMO code; None values when absent."""
    if not _num(code):
        return None, None
    label, cc = WMO_CONDITIONS.get(int(code), ("Unknown", "unknown"))
    return label, cc


def adapt_open_meteo_hourly(p: dict) -> dict:
    label, cc = _wmo_condition(p.get("weather_code"))
    rain = p.get("rain") if _num(p.get("rain")) else None
    showers = p.get("showers") if _num(p.get("showers")) else None
    rain_in = None
    if rain is not None or showers is not None:
        rain_in = (rain or 0.0) + (showers or 0.0)
    return _clean({
        "temp_f": _passthrough(p.get("temperature_2m")),
        "feels_like_f": _passthrough(p.get("apparent_temperature")),
        "dew_point_f": _passthrough(p.get("dew_point_2m")),
        "humidity_pct": _passthrough(p.get("relative_humidity_2m")),
        "wind_mph": _passthrough(p.get("wind_speed_10m")),
        "wind_dir_deg": _passthrough(p.get("wind_direction_10m")),
        "gusts_mph": _passthrough(p.get("wind_gusts_10m")),
        "pressure_mb": _passthrough(p.get("pressure_msl")),
        "uv_index": _passthrough(p.get("uv_index")),
        "precip_prob_pct": _passthrough(p.get("precipitation_probability")),
        "precip_in": _passthrough(p.get("precipitation")),
        "rain_in": rain_in,
        "snow_in": _passthrough(p.get("snowfall")),
        "cloud_cover_pct": _passthrough(p.get("cloud_cover")),
        "is_day": p.get("is_day") if p.get("is_day") in (0, 1) else None,
        "condition": label,
        "condition_code": cc,
    })


def adapt_open_meteo_daily(p: dict) -> dict:
    label, cc = _wmo_condition(p.get("weather_code"))
    return _clean({
        "temp_max_f": _passthrough(p.get("temperature_2m_max")),
        "temp_min_f": _passthrough(p.get("temperature_2m_min")),
        "feels_like_max_f": _passthrough(p.get("apparent_temperature_max")),
        "feels_like_min_f": _passthrough(p.get("apparent_temperature_min")),
        "sunrise": p.get("sunrise"),   # already local ISO strings
        "sunset": p.get("sunset"),
        "uv_index_max": _passthrough(p.get("uv_index_max")),
        "precip_prob_max_pct": _passthrough(p.get("precipitation_probability_max")),
        "precip_sum_in": _passthrough(p.get("precipitation_sum")),
        "wind_max_mph": _passthrough(p.get("wind_speed_10m_max")),
        "gusts_max_mph": _passthrough(p.get("wind_gusts_10m_max")),
        "wind_dir_deg": _passthrough(p.get("wind_direction_10m_dominant")),
        "condition": label,
        "condition_code": cc,
    })


def adapt_open_meteo_minutely(p: dict) -> dict:
    label, cc = _wmo_condition(p.get("weather_code"))
    return _clean({
        "precip_in": _passthrough(p.get("precipitation")),
        "rain_in": _passthrough(p.get("rain")),
        "snow_in": _passthrough(p.get("snowfall")),
        "gusts_mph": _passthrough(p.get("wind_gusts_10m")),
        "condition": label,
        "condition_code": cc,
    })


# ---- Pirate Weather (Dark Sky schema) ----

def _pirate_condition(p: dict):
    """(label, condition_code, is_day). Label prefers the human summary;
    day/night comes from the icon suffix when present."""
    icon = p.get("icon")
    label, cc = (None, None)
    if isinstance(icon, str) and icon in PIRATE_CONDITIONS:
        label, cc = PIRATE_CONDITIONS[icon]
    elif icon is not None:
        label, cc = "Unknown", "unknown"
    summary = p.get("summary")
    if isinstance(summary, str) and summary:
        label = summary
    is_day = None
    if isinstance(icon, str):
        if icon.endswith("-day"):
            is_day = 1
        elif icon.endswith("-night"):
            is_day = 0
    return label, cc, is_day


def _pirate_precip_split(precip_in, precip_type):
    """(rain_in, snow_in) from a step total and its precipType. Sleet stays
    in precip_in only; an unknown type counts as rain (liquid)."""
    if precip_in is None:
        return None, None
    if precip_type == "snow":
        return 0.0, precip_in
    if precip_type == "sleet":
        return 0.0, 0.0
    return precip_in, 0.0


def adapt_pirate_hourly(p: dict) -> dict:
    label, cc, is_day = _pirate_condition(p)
    # Hourly steps are one hour: precipAccumulation is the step total in
    # inches; fall back to precipIntensity (in/hr) * 1h when absent.
    precip = p.get("precipAccumulation")
    if not _num(precip):
        precip = p.get("precipIntensity")
    precip_in = precip if _num(precip) else None
    rain_in, snow_in = _pirate_precip_split(precip_in, p.get("precipType"))
    return _clean({
        "temp_f": _passthrough(p.get("temperature")),
        "feels_like_f": _passthrough(p.get("apparentTemperature")),
        "dew_point_f": _passthrough(p.get("dewPoint")),
        "humidity_pct": _pct(p.get("humidity")),
        "wind_mph": _passthrough(p.get("windSpeed")),
        "wind_dir_deg": _passthrough(p.get("windBearing")),
        "gusts_mph": _passthrough(p.get("windGust")),
        "pressure_mb": _passthrough(p.get("pressure")),
        "uv_index": _passthrough(p.get("uvIndex")),
        "precip_prob_pct": _pct(p.get("precipProbability")),
        "precip_in": precip_in,
        "rain_in": rain_in,
        "snow_in": snow_in,
        "cloud_cover_pct": _pct(p.get("cloudCover")),
        "is_day": is_day,
        "condition": label,
        "condition_code": cc,
    })


def adapt_pirate_daily(p: dict, tzinfo) -> dict:
    label, cc, _ = _pirate_condition(p)

    def first_num(*keys):
        for k in keys:
            if _num(p.get(k)):
                return p[k]
        return None

    return _clean({
        # temperatureMax/Min are the 24h extremes (High/Low are daytime/
        # overnight); prefer the 24h values to match Open-Meteo semantics.
        "temp_max_f": first_num("temperatureMax", "temperatureHigh"),
        "temp_min_f": first_num("temperatureMin", "temperatureLow"),
        "feels_like_max_f": first_num("apparentTemperatureMax", "apparentTemperatureHigh"),
        "feels_like_min_f": first_num("apparentTemperatureMin", "apparentTemperatureLow"),
        "sunrise": _local_iso(p.get("sunriseTime"), tzinfo),
        "sunset": _local_iso(p.get("sunsetTime"), tzinfo),
        "uv_index_max": _passthrough(p.get("uvIndex")),  # daily uvIndex is the day's max
        "precip_prob_max_pct": _pct(p.get("precipProbability")),
        "precip_sum_in": _passthrough(p.get("precipAccumulation")),
        "wind_max_mph": _passthrough(p.get("windSpeed")),
        "gusts_max_mph": _passthrough(p.get("windGust")),
        "wind_dir_deg": _passthrough(p.get("windBearing")),
        "condition": label,
        "condition_code": cc,
    })


def adapt_pirate_minutely_rows(rows: list) -> list:
    """Aggregate Pirate's 61 one-minute steps into the canonical 15-minute
    nowcast buckets (matching Open-Meteo's minutely_15 cadence). Each
    minute contributes precipIntensity (in/hr) / 60 inches to its bucket;
    probability is the bucket max."""
    buckets = {}
    order = []
    for ts, p in rows:
        if len(ts) < 16:
            continue
        bucket_ts = ts[:14] + f"{(int(ts[14:16]) // 15) * 15:02d}"
        if bucket_ts not in buckets:
            buckets[bucket_ts] = {"rain": 0.0, "snow": 0.0, "sleet": 0.0, "prob": None}
            order.append(bucket_ts)
        b = buckets[bucket_ts]
        intensity = p.get("precipIntensity")
        if _num(intensity) and intensity > 0:
            ptype = p.get("precipType")
            key = ptype if ptype in ("rain", "snow", "sleet") else "rain"
            b[key] += intensity / 60.0
        prob = p.get("precipProbability")
        if _num(prob):
            b["prob"] = max(b["prob"] or 0.0, prob)

    out = []
    for ts in order:
        b = buckets[ts]
        total = b["rain"] + b["snow"] + b["sleet"]
        label, cc = None, None
        if b["snow"] > 0:
            label, cc = "Snow", "snow"
        elif b["sleet"] > 0:
            label, cc = "Sleet", "sleet"
        elif b["rain"] > 0:
            label, cc = "Rain", "rain"
        out.append((ts, _clean({
            "precip_in": round(total, 4),
            "rain_in": round(b["rain"], 4),
            "snow_in": round(b["snow"], 4),
            "precip_prob_pct": _pct(b["prob"]),
            "condition": label,
            "condition_code": cc,
        })))
    return out


# ---- dispatch ----

_PER_ROW = {
    ("open_meteo", "hourly"): adapt_open_meteo_hourly,
    ("open_meteo", "daily"): adapt_open_meteo_daily,
    ("open_meteo", "minutely15"): adapt_open_meteo_minutely,
    ("pirate_weather", "hourly"): adapt_pirate_hourly,
}


def adapt_rows(source: str, kind: str, rows: list, tzinfo) -> list:
    """[(ts, native_payload)] -> [(ts, canonical_payload)]. Unknown
    source/kind combinations pass through unchanged (forward-compatible:
    a new source serves raw until an adapter lands, it never 500s)."""
    if (source, kind) == ("pirate_weather", "minutely15"):
        return adapt_pirate_minutely_rows(rows)
    if (source, kind) == ("pirate_weather", "daily"):
        return [(ts, adapt_pirate_daily(p, tzinfo)) for ts, p in rows]
    fn = _PER_ROW.get((source, kind))
    if fn is None:
        return rows
    return [(ts, fn(p)) for ts, p in rows]
