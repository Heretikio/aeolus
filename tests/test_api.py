"""API and staleness tests via the Flask test client. No network, no pollers."""

import time
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app import create_app, is_stale
from conftest import DAY, HIT_POLYGON, make_feature
from alerting import process_alerts


@pytest.fixture
def app(cfg, store):
    return create_app(cfg, run_pollers=False)


@pytest.fixture
def client(app):
    return app.test_client()


def _seed_hourly(cfg, store, hours=500):
    start = datetime.now(cfg.tzinfo).replace(minute=0, second=0, microsecond=0)
    rows = [((start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"),
             {"temperature_2m": 70.0 + i * 0.1}) for i in range(hours)]
    store.save_forecast_run("open_meteo", "hourly", store.default_location_id(),
                            time.time(), rows)
    store.set_source_success("open_meteo_forecast", time.time())


def _seed_daily(cfg, store, days=16):
    start = datetime.now(cfg.tzinfo)
    rows = [((start + timedelta(days=i)).strftime("%Y-%m-%d"),
             {"temperature_2m_max": 90.0 + i}) for i in range(days)]
    store.save_forecast_run("open_meteo", "daily", store.default_location_id(),
                            time.time(), rows)
    store.set_source_success("open_meteo_forecast", time.time())


def _seed_nowcast(cfg, store, steps=24):
    now = datetime.now(cfg.tzinfo)
    start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    rows = [((start + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M"),
             {"precipitation": 0.0}) for i in range(steps)]
    store.save_forecast_run("open_meteo", "minutely15", store.default_location_id(),
                            time.time(), rows)
    store.set_source_success("open_meteo_nowcast", time.time())


# ---- empty store: 503 only before the first successful poll ----

def test_empty_store_returns_503_with_message(client):
    for path in ("/v1/hourly", "/v1/daily", "/v1/nowcast", "/v1/current"):
        resp = client.get(path)
        assert resp.status_code == 503, path
        assert "first successful poll" in resp.get_json()["error"]


# ---- hourly ----

def test_hourly_defaults_and_envelope(cfg, store, client):
    _seed_hourly(cfg, store)
    data = client.get("/v1/hourly").get_json()
    assert data["source"] == "open_meteo"
    assert data["stale"] is False
    assert "fetched_at" in data
    assert len(data["hourly"]) == 48
    # stored source-native, served canonical
    assert data["hourly"][0]["temp_f"] == 70.0
    assert "temperature_2m" not in data["hourly"][0]


def test_hourly_clamps_h_to_384(cfg, store, client):
    _seed_hourly(cfg, store, hours=500)
    assert len(client.get("/v1/hourly?h=1000").get_json()["hourly"]) == 384
    assert len(client.get("/v1/hourly?h=12").get_json()["hourly"]) == 12
    assert len(client.get("/v1/hourly?h=0").get_json()["hourly"]) == 1
    assert len(client.get("/v1/hourly?h=junk").get_json()["hourly"]) == 48


def test_hourly_serves_last_known_good_when_stale(cfg, store, client):
    _seed_hourly(cfg, store)
    store.set_source_success("open_meteo_forecast", time.time() - 4 * 3600)
    data = client.get("/v1/hourly").get_json()
    assert data["stale"] is True
    assert len(data["hourly"]) == 48  # still serves, only badged


def test_hourly_fails_over_to_fresh_secondary_when_primary_stale(cfg, store, client):
    """A stale Open-Meteo must not shadow fresh Pirate Weather data."""
    _seed_hourly(cfg, store)
    store.set_source_success("open_meteo_forecast", time.time() - 4 * 3600)  # stale

    start = datetime.now(cfg.tzinfo).replace(minute=0, second=0, microsecond=0)
    rows = [((start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"),
             {"temperature": 60.0 + i}) for i in range(72)]
    store.save_forecast_run("pirate_weather", "hourly", store.default_location_id(),
                            time.time(), rows)
    store.set_source_success("pirate_weather", time.time())

    data = client.get("/v1/hourly").get_json()
    assert data["source"] == "pirate_weather"
    assert data["stale"] is False
    assert data["hourly"][0]["temp_f"] == 60.0  # canonical even on fallback

    # Both stale: the freshest run serves, badged stale
    store.set_source_success("pirate_weather", time.time() - 4 * 3600)
    data = client.get("/v1/hourly").get_json()
    assert data["source"] == "pirate_weather"  # newer fetched_at than open_meteo
    assert data["stale"] is True


def test_unknown_integer_loc_is_404_junk_falls_back(cfg, store, client):
    _seed_hourly(cfg, store)
    resp = client.get("/v1/hourly?loc=999")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "unknown location"
    assert client.get("/v1/hourly?loc=junk").status_code == 200  # falls back to default


# ---- daily ----

def test_daily_serves_max_10_of_16_stored(cfg, store, client):
    _seed_daily(cfg, store, days=16)
    assert len(client.get("/v1/daily").get_json()["daily"]) == 10
    assert len(client.get("/v1/daily?d=3").get_json()["daily"]) == 3
    assert len(client.get("/v1/daily?d=99").get_json()["daily"]) == 10


# ---- nowcast ----

def test_nowcast_serves_from_current_bucket(cfg, store, client):
    _seed_nowcast(cfg, store)
    data = client.get("/v1/nowcast").get_json()
    assert data["source"] == "open_meteo"
    assert len(data["nowcast"]) == 24


# ---- pirate-only store: the Open-Meteo-outage scenario ----

def _seed_pirate_everything(cfg, store):
    """Pirate Weather payloads shaped like real stored rows (Dark Sky schema,
    units=us), covering hourly, daily and one-minute nowcast."""
    loc = store.default_location_id()
    now = time.time()
    start = datetime.now(cfg.tzinfo).replace(minute=0, second=0, microsecond=0)
    hourly = [((start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"),
               {"summary": "Partly Cloudy", "icon": "partly-cloudy-day",
                "temperature": 90.0 + i, "apparentTemperature": 97.0 + i,
                "dewPoint": 72.4, "humidity": 0.56, "pressure": 1011.67,
                "windSpeed": 9.83, "windGust": 16.98, "windBearing": 190,
                "cloudCover": 0.41, "uvIndex": 1.75,
                "precipIntensity": 0.0, "precipProbability": 0.35,
                "precipAccumulation": 0.02, "precipType": "rain"})
              for i in range(72)]
    store.save_forecast_run("pirate_weather", "hourly", loc, now, hourly)

    today = datetime.now(cfg.tzinfo)
    daily = [((today + timedelta(days=i)).strftime("%Y-%m-%d"),
              {"summary": "Hot.", "icon": "clear-day",
               "temperatureMax": 92.0 + i, "temperatureMin": 76.0 + i,
               "apparentTemperatureMax": 103.0, "apparentTemperatureMin": 81.5,
               "sunriseTime": 1783076216, "sunsetTime": 1783129606,
               "uvIndex": 7.8, "precipProbability": 0.3,
               "precipAccumulation": 0.0, "windSpeed": 8.01,
               "windGust": 14.87, "windBearing": 191})
             for i in range(8)]
    store.save_forecast_run("pirate_weather", "daily", loc, now, daily)

    q = today.replace(minute=(today.minute // 15) * 15, second=0, microsecond=0)
    minutely = [((q + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M"),
                 {"precipIntensity": 0.6, "precipProbability": 0.8,
                  "precipType": "rain"}) for i in range(61)]
    store.save_forecast_run("pirate_weather", "minutely15", loc, now, minutely)
    store.set_source_success("pirate_weather", now)


def test_pirate_only_store_serves_complete_canonical_payloads(cfg, store, client):
    """Open-Meteo never polled (outage from first boot): every forecast
    endpoint must still serve the full canonical schema from Pirate rows."""
    _seed_pirate_everything(cfg, store)

    cur = client.get("/v1/current").get_json()
    assert cur["source"] == "pirate_weather"
    c = cur["current"]
    assert c["temp_f"] == 90.0
    assert c["feels_like_f"] == 97.0
    assert c["humidity_pct"] == 56
    assert c["wind_mph"] == 9.83
    assert c["gusts_mph"] == 16.98
    assert c["wind_dir_deg"] == 190
    assert c["pressure_mb"] == 1011.67
    assert c["uv_index"] == 1.75
    assert c["precip_prob_pct"] == 35
    assert c["precip_in"] == 0.02
    assert c["condition"] == "Partly Cloudy"
    assert c["condition_code"] == "partly_cloudy"
    assert c["is_day"] == 1
    assert "temperature" not in c and "windSpeed" not in c

    hr = client.get("/v1/hourly?h=6").get_json()
    assert hr["source"] == "pirate_weather"
    assert len(hr["hourly"]) == 6
    for row in hr["hourly"]:
        for key in ("temp_f", "feels_like_f", "humidity_pct", "wind_mph",
                    "gusts_mph", "pressure_mb", "uv_index", "precip_prob_pct",
                    "precip_in", "condition", "condition_code"):
            assert key in row, key

    dy = client.get("/v1/daily").get_json()
    assert dy["source"] == "pirate_weather"
    d = dy["daily"][0]
    assert d["temp_max_f"] == 92.0
    assert d["temp_min_f"] == 76.0
    assert d["precip_prob_max_pct"] == 30
    assert d["uv_index_max"] == 7.8
    assert d["condition_code"] == "clear"
    assert "temperatureMax" not in d

    nc = client.get("/v1/nowcast").get_json()
    assert nc["source"] == "pirate_weather"
    assert nc["nowcast"], "bucketed nowcast must not be empty"
    first = nc["nowcast"][0]
    assert first["precip_prob_pct"] == 80
    assert first["precip_in"] > 0
    assert first["condition_code"] == "rain"
    # 61 one-minute steps collapse into 15-minute buckets
    assert len(nc["nowcast"]) <= 6


# ---- current ----

def test_current_serves_model_row_for_this_hour(cfg, store, client):
    _seed_hourly(cfg, store)
    data = client.get("/v1/current").get_json()
    assert data["source"] == "open_meteo"
    expected_ts = datetime.now(cfg.tzinfo).strftime("%Y-%m-%dT%H:00")
    assert data["current"]["ts"] == expected_ts


def test_current_prefers_fresh_station_observation(cfg, store, client):
    _seed_hourly(cfg, store)
    store.insert_observation({"source": "station", "station_id": "station1",
                              "ts": time.time() - 60, "temp": 88.5})
    data = client.get("/v1/current").get_json()
    assert data["source"] == "station"
    assert data["current"]["temp"] == 88.5


# ---- alerts ----

def test_alerts_endpoint_carries_affects_point(cfg, store, client):
    future_ends = "2030-01-01T00:00:00-06:00"
    feature = make_feature(geometry=HIT_POLYGON, ends=future_ends, expires=future_ends)
    process_alerts(cfg, store, [feature], now=DAY, send=lambda *a, **k: True)
    store.set_source_success("nws_alerts", time.time())
    data = client.get("/v1/alerts").get_json()
    assert data["count"] == 1
    assert data["alerts"][0]["affects_point"] is True
    assert data["alerts"][0]["event"] == "Tornado Warning"
    assert data["stale"] is False


def test_alerts_endpoint_empty_is_200_not_503(client):
    data = client.get("/v1/alerts").get_json()
    assert data["count"] == 0
    assert data["stale"] is True  # alerts poller has never succeeded


# ---- locations / health / metrics ----

def test_locations(client):
    data = client.get("/v1/locations").get_json()
    assert data["locations"][0]["name"] == "Home"
    assert data["stale"] is False


def test_healthz(cfg, store, client):
    store.set_source_success("open_meteo_forecast", time.time())
    data = client.get("/healthz").get_json()
    assert data["status"] == "ok"
    assert data["sources"]["open_meteo_forecast"]["stale"] is False


def test_corrupt_heartbeat_does_not_500_monitoring(cfg, store, client):
    store.meta_set("leader_heartbeat", "garbage")
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.get_json()["poller_leader_alive"] is False
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "aeolus_poller_leader 0.0" in metrics.get_data(as_text=True)


def test_metrics_exposes_gauges(cfg, store, client):
    store.set_source_success("open_meteo_forecast", time.time())
    store.record_source_failure("nws_alerts", time.time())
    body = client.get("/metrics").get_data(as_text=True)
    assert "aeolus_source_last_success_timestamp_seconds" in body
    assert 'aeolus_source_failures_total{source="nws_alerts"} 1.0' in body
    assert "aeolus_active_alerts" in body
    assert "aeolus_poller_leader 0.0" in body
    assert 'aeolus_source_stale{source="open_meteo_forecast"} 0.0' in body


# ---- staleness flag logic (unit) ----

def test_is_stale_logic(cfg):
    now = time.time()
    assert is_stale(cfg, None, "open_meteo_forecast", now) is True
    assert is_stale(cfg, {"last_success": None}, "open_meteo_forecast", now) is True
    fresh = {"last_success": now - 10}
    assert is_stale(cfg, fresh, "open_meteo_forecast", now) is False
    old = {"last_success": now - 4 * 3600}  # window is 3x the hourly interval
    assert is_stale(cfg, old, "open_meteo_forecast", now) is True
    # Window scales per source: 10 min old nowcast (5 min cadence) is stale
    assert is_stale(cfg, {"last_success": now - 3000}, "open_meteo_nowcast", now) is True
    assert is_stale(cfg, {"last_success": now - 600}, "open_meteo_nowcast", now) is False


# ---- ALERTS_ENABLED=0 (non-US deployments) ----

def test_alerts_disabled_returns_honest_empty(cfg, store):
    cfg2 = replace(cfg, alerts_enabled=False)
    client = create_app(cfg2, run_pollers=False).test_client()
    data = client.get("/v1/alerts").get_json()
    assert data["source"] == "disabled"
    assert data["alerts"] == [] and data["count"] == 0
    assert data["stale"] is False  # no poller runs; must not read as a stale error


def test_burn_not_marked_stale_by_alerts_when_disabled(cfg, store):
    """With alerts off, a fresh forecast must yield a fresh burn verdict; the
    never-run alert poller must not drag the burn payload to stale."""
    cfg2 = replace(cfg, alerts_enabled=False)
    _seed_hourly(cfg2, store)
    client = create_app(cfg2, run_pollers=False).test_client()
    data = client.get("/v1/burn").get_json()
    assert data["verdict"] in ("go", "caution", "no_burn")
    assert data["stale"] is False


def test_radar_alerts_geojson_disabled_is_empty_not_stale(cfg, store):
    cfg2 = replace(cfg, alerts_enabled=False)
    client = create_app(cfg2, run_pollers=False).test_client()
    data = client.get("/v1/radar/alerts.geojson").get_json()
    assert data["type"] == "FeatureCollection"
    assert data["features"] == []
    assert data["source"] == "disabled"
    assert data["stale"] is False
