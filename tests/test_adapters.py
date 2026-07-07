"""Canonical-schema adapter tests: unit conversions, enum mapping, and
tolerance for missing/garbage upstream values. Payload fixtures mirror real
stored rows (Open-Meteo with US units requested; Pirate Weather units=us)."""

from zoneinfo import ZoneInfo

import adapters

TZ = ZoneInfo("America/Chicago")

# Real shapes: Open-Meteo polled with fahrenheit/mph/inch (sources.US_UNITS)
OM_HOURLY = {
    "temperature_2m": 90.2, "apparent_temperature": 97.9, "dew_point_2m": 72.4,
    "relative_humidity_2m": 56, "precipitation_probability": 20,
    "precipitation": 0.04, "rain": 0.03, "showers": 0.01, "snowfall": 0.0,
    "weather_code": 80, "pressure_msl": 1011.7, "cloud_cover": 41,
    "wind_speed_10m": 9.8, "wind_direction_10m": 190, "wind_gusts_10m": 17.0,
    "uv_index": 1.75, "is_day": 1,
}

# A real Pirate Weather hourly payload (Dark Sky schema), captured verbatim
PIRATE_HOURLY = {
    "summary": "Partly Cloudy", "icon": "partly-cloudy-day",
    "precipIntensity": 0.0, "precipProbability": 0.0,
    "precipAccumulation": 0.0, "precipType": "rain",
    "temperature": 90.23, "apparentTemperature": 97.97, "dewPoint": 72.41,
    "humidity": 0.56, "pressure": 1011.67, "windSpeed": 9.83,
    "windGust": 16.98, "windBearing": 190, "cloudCover": 0.41,
    "uvIndex": 1.75, "visibility": 10.0,
}


# ---- Open-Meteo hourly ----

def test_open_meteo_hourly_maps_to_canonical():
    out = adapters.adapt_open_meteo_hourly(OM_HOURLY)
    assert out["temp_f"] == 90.2
    assert out["feels_like_f"] == 97.9
    assert out["dew_point_f"] == 72.4
    assert out["humidity_pct"] == 56          # already 0-100
    assert out["wind_mph"] == 9.8
    assert out["wind_dir_deg"] == 190
    assert out["gusts_mph"] == 17.0
    assert out["pressure_mb"] == 1011.7       # hPa == mb
    assert out["uv_index"] == 1.75
    assert out["precip_prob_pct"] == 20       # already 0-100
    assert out["precip_in"] == 0.04
    assert out["rain_in"] == 0.04             # rain + showers
    assert out["snow_in"] == 0.0
    assert out["cloud_cover_pct"] == 41
    assert out["is_day"] == 1
    assert out["condition"] == "Light showers"
    assert out["condition_code"] == "rain"


def test_open_meteo_hourly_tolerates_missing_and_garbage():
    out = adapters.adapt_open_meteo_hourly(
        {"temperature_2m": "junk", "relative_humidity_2m": None})
    assert out == {}  # nothing invented, nothing crashes


def test_wmo_enum_mapping_spot_checks():
    cases = {0: "clear", 1: "clear", 2: "partly_cloudy", 3: "cloudy",
             45: "fog", 55: "drizzle", 56: "freezing_rain", 66: "freezing_rain",
             75: "snow", 82: "rain", 86: "snow", 95: "thunderstorm",
             96: "hail", 99: "hail"}
    for code, expected in cases.items():
        out = adapters.adapt_open_meteo_hourly({"weather_code": code})
        assert out["condition_code"] == expected, code
    assert adapters.adapt_open_meteo_hourly(
        {"weather_code": 42})["condition_code"] == "unknown"


def test_open_meteo_daily_maps_to_canonical():
    out = adapters.adapt_open_meteo_daily({
        "weather_code": 3, "temperature_2m_max": 92.4, "temperature_2m_min": 76.2,
        "apparent_temperature_max": 103.0, "apparent_temperature_min": 81.5,
        "sunrise": "2026-07-03T05:59", "sunset": "2026-07-03T20:49",
        "uv_index_max": 7.8, "precipitation_sum": 0.12,
        "precipitation_probability_max": 40, "wind_speed_10m_max": 8.0,
        "wind_gusts_10m_max": 14.9, "wind_direction_10m_dominant": 191,
    })
    assert out["temp_max_f"] == 92.4
    assert out["temp_min_f"] == 76.2
    assert out["feels_like_max_f"] == 103.0
    assert out["feels_like_min_f"] == 81.5
    assert out["sunrise"] == "2026-07-03T05:59"
    assert out["uv_index_max"] == 7.8
    assert out["precip_sum_in"] == 0.12
    assert out["precip_prob_max_pct"] == 40
    assert out["wind_max_mph"] == 8.0
    assert out["gusts_max_mph"] == 14.9
    assert out["wind_dir_deg"] == 191
    assert out["condition_code"] == "cloudy"


def test_open_meteo_minutely_maps_to_canonical():
    out = adapters.adapt_open_meteo_minutely(
        {"precipitation": 0.05, "rain": 0.05, "snowfall": 0.0,
         "wind_gusts_10m": 22.0, "weather_code": 61})
    assert out == {"precip_in": 0.05, "rain_in": 0.05, "snow_in": 0.0,
                   "gusts_mph": 22.0, "condition": "Light rain",
                   "condition_code": "rain"}


# ---- Pirate Weather ----

def test_pirate_hourly_converts_units():
    out = adapters.adapt_pirate_hourly(PIRATE_HOURLY)
    assert out["temp_f"] == 90.23
    assert out["feels_like_f"] == 97.97
    assert out["dew_point_f"] == 72.41
    assert out["humidity_pct"] == 56          # 0.56 -> 56
    assert out["wind_mph"] == 9.83
    assert out["wind_dir_deg"] == 190
    assert out["gusts_mph"] == 16.98
    assert out["pressure_mb"] == 1011.67      # already mb with units=us
    assert out["uv_index"] == 1.75
    assert out["precip_prob_pct"] == 0        # 0.0 -> 0
    assert out["precip_in"] == 0.0
    assert out["cloud_cover_pct"] == 41       # 0.41 -> 41
    assert out["is_day"] == 1                 # icon ends in -day
    assert out["condition"] == "Partly Cloudy"  # summary wins over icon label
    assert out["condition_code"] == "partly_cloudy"


def test_pirate_hourly_precip_probability_scales():
    out = adapters.adapt_pirate_hourly({"precipProbability": 0.65})
    assert out["precip_prob_pct"] == 65


def test_pirate_hourly_snow_split_and_intensity_fallback():
    # No precipAccumulation: falls back to precipIntensity over the 1h step
    out = adapters.adapt_pirate_hourly(
        {"precipIntensity": 0.12, "precipType": "snow", "icon": "snow"})
    assert out["precip_in"] == 0.12
    assert out["snow_in"] == 0.12
    assert out["rain_in"] == 0.0
    assert out["condition_code"] == "snow"


def test_pirate_icon_enum_mapping():
    cases = {"clear-day": "clear", "clear-night": "clear",
             "partly-cloudy-night": "partly_cloudy", "cloudy": "cloudy",
             "fog": "fog", "wind": "windy", "rain": "rain", "sleet": "sleet",
             "snow": "snow", "hail": "hail", "thunderstorm": "thunderstorm",
             "tornado": "tornado"}
    for icon, expected in cases.items():
        out = adapters.adapt_pirate_hourly({"icon": icon})
        assert out["condition_code"] == expected, icon
    assert adapters.adapt_pirate_hourly(
        {"icon": "some-new-icon"})["condition_code"] == "unknown"


def test_pirate_night_icon_sets_is_day_zero():
    out = adapters.adapt_pirate_hourly({"icon": "partly-cloudy-night"})
    assert out["is_day"] == 0
    assert "is_day" not in adapters.adapt_pirate_hourly({"icon": "rain"})


def test_pirate_daily_maps_and_converts_sun_times():
    out = adapters.adapt_pirate_daily({
        "summary": "Possible thunderstorms overnight.", "icon": "clear-day",
        "sunriseTime": 1783076216, "sunsetTime": 1783129606,
        "precipProbability": 0.3, "precipAccumulation": 0.25,
        "temperatureMax": 92.39, "temperatureMin": 76.19,
        "apparentTemperatureMax": 102.99, "apparentTemperatureMin": 81.51,
        "temperatureHigh": 92.0, "temperatureLow": 76.37,
        "uvIndex": 7.8, "windSpeed": 8.01, "windGust": 14.87, "windBearing": 191,
    }, TZ)
    assert out["temp_max_f"] == 92.39      # 24h Max preferred over daytime High
    assert out["temp_min_f"] == 76.19
    assert out["feels_like_max_f"] == 102.99
    assert out["feels_like_min_f"] == 81.51
    # 1783076216 = 2026-07-03 05:56 America/Chicago
    assert out["sunrise"] == "2026-07-03T05:56"
    assert out["sunset"] == "2026-07-03T20:46"
    assert out["uv_index_max"] == 7.8
    assert out["precip_prob_max_pct"] == 30
    assert out["precip_sum_in"] == 0.25
    assert out["wind_max_mph"] == 8.01
    assert out["gusts_max_mph"] == 14.87
    assert out["wind_dir_deg"] == 191
    assert out["condition"] == "Possible thunderstorms overnight."
    assert out["condition_code"] == "clear"


def test_pirate_daily_falls_back_to_high_low():
    out = adapters.adapt_pirate_daily(
        {"temperatureHigh": 91.0, "temperatureLow": 70.0}, TZ)
    assert out["temp_max_f"] == 91.0
    assert out["temp_min_f"] == 70.0


def test_pirate_minutely_buckets_to_quarter_hours():
    # One-minute steps 19:10-19:39 of rain at 0.6 in/hr -> buckets 19:00
    # (5 min), 19:15 (full 15 min = 0.6/60 * 15 = 0.15 in), 19:30 (10 min)
    rows = [(f"2026-07-03T19:{10 + m:02d}",
             {"precipIntensity": 0.6, "precipProbability": 0.5 + m * 0.01,
              "precipType": "rain"}) for m in range(30)]
    out = adapters.adapt_pirate_minutely_rows(rows)
    ts_list = [ts for ts, _ in out]
    assert ts_list == ["2026-07-03T19:00", "2026-07-03T19:15", "2026-07-03T19:30"]
    full = dict(out)["2026-07-03T19:15"]
    assert full["precip_in"] == 0.15
    assert full["rain_in"] == 0.15
    assert full["snow_in"] == 0.0
    assert full["precip_prob_pct"] == 69   # max within the bucket (m=19)
    assert full["condition_code"] == "rain"
    assert dict(out)["2026-07-03T19:00"]["precip_in"] == 0.05  # 5 minutes


def test_pirate_minutely_dry_bucket_has_no_condition():
    rows = [("2026-07-03T19:00", {"precipIntensity": 0.0,
                                  "precipProbability": 0.0, "precipType": "none"})]
    out = adapters.adapt_pirate_minutely_rows(rows)
    assert out[0][1] == {"precip_in": 0.0, "rain_in": 0.0, "snow_in": 0.0,
                         "precip_prob_pct": 0}


def test_pirate_minutely_snow_dominant_condition():
    rows = [("2026-07-03T19:00", {"precipIntensity": 0.3, "precipType": "snow"}),
            ("2026-07-03T19:01", {"precipIntensity": 0.3, "precipType": "rain"})]
    out = adapters.adapt_pirate_minutely_rows(rows)
    p = out[0][1]
    assert p["snow_in"] == 0.005
    assert p["rain_in"] == 0.005
    assert p["precip_in"] == 0.01
    assert p["condition_code"] == "snow"


# ---- dispatch ----

def test_adapt_rows_dispatches_per_source():
    om = adapters.adapt_rows("open_meteo", "hourly",
                             [("t0", {"temperature_2m": 70.0})], TZ)
    assert om[0][1]["temp_f"] == 70.0
    pw = adapters.adapt_rows("pirate_weather", "hourly",
                             [("t0", {"temperature": 60.0, "humidity": 0.5})], TZ)
    assert pw[0][1] == {"temp_f": 60.0, "humidity_pct": 50}
    pd = adapters.adapt_rows("pirate_weather", "daily",
                             [("2026-07-03", {"temperatureMax": 90.0})], TZ)
    assert pd[0][1]["temp_max_f"] == 90.0


def test_adapt_rows_unknown_source_passes_through():
    rows = [("t0", {"whatever": 1})]
    assert adapters.adapt_rows("new_source", "hourly", rows, TZ) == rows
