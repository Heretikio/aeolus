"""Live smoke test: one real fetch per source against the free APIs, rows
land in a scratch DB, and /v1/hourly serves them through the Flask client.

Network required. Deliberately single-shot per source; do not loop this.
Run with: AEOLUS_LIVE=1 pytest tests/test_live_smoke.py -v
"""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AEOLUS_LIVE") != "1",
    reason="live smoke only runs with AEOLUS_LIVE=1 (network, real APIs)",
)


def test_live_smoke(cfg, store):
    import sources
    from alerting import process_alerts
    from app import create_app

    loc = store.default_location_id()

    # Open-Meteo forecast: hourly to 16 days + daily
    raw = sources.fetch_open_meteo_forecast(cfg)
    hourly = sources.normalize_block(raw, "hourly", sources.HOURLY_FIELDS)
    daily = sources.normalize_block(raw, "daily", sources.DAILY_FIELDS)
    assert len(hourly) == 16 * 24, f"expected 384 hourly steps, got {len(hourly)}"
    assert len(daily) == 16
    assert "temperature_2m" in hourly[0][1]
    now = time.time()
    store.save_forecast_run("open_meteo", "hourly", loc, now, hourly)
    store.save_forecast_run("open_meteo", "daily", loc, now, daily)
    store.set_source_success("open_meteo_forecast", now)

    # Open-Meteo minutely_15 nowcast
    raw = sources.fetch_open_meteo_nowcast(cfg)
    minutely = sources.normalize_block(raw, "minutely_15", sources.MINUTELY_FIELDS)
    assert len(minutely) >= 8, f"expected at least 8 nowcast steps, got {len(minutely)}"
    assert "precipitation" in minutely[0][1]
    now = time.time()
    store.save_forecast_run("open_meteo", "minutely15", loc, now, minutely)
    store.set_source_success("open_meteo_nowcast", now)

    # NWS alerts with the identifying User-Agent (may legitimately be empty)
    features = sources.fetch_nws_alerts(cfg)
    assert isinstance(features, list)
    process_alerts(cfg, store, features)  # DRY-RUN sends (no webhook in test cfg)
    store.set_source_success("nws_alerts", time.time())

    # The API serves what just landed
    client = create_app(cfg, run_pollers=False).test_client()
    data = client.get("/v1/hourly").get_json()
    assert data["source"] == "open_meteo"
    assert data["stale"] is False
    assert len(data["hourly"]) == 48
    assert data["hourly"][0]["temperature_2m"] is not None

    nowcast = client.get("/v1/nowcast").get_json()
    assert len(nowcast["nowcast"]) >= 1

    alerts = client.get("/v1/alerts").get_json()
    assert alerts["stale"] is False
    assert alerts["count"] == len([a for a in alerts["alerts"]])

    print(f"\nlive smoke: hourly={len(hourly)} daily={len(daily)}"
          f" minutely15={len(minutely)} nws_alerts={len(features)}"
          f" served_hourly={len(data['hourly'])}")
