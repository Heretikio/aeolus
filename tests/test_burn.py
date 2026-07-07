"""Burn-window tests: threshold triggers, the wind+humidity combo, alert
gating (fire alerts only), caution boundaries, the look-ahead flip, and
missing-data degradation. Unit tests hit burn.evaluate/lookahead directly;
endpoint tests go through /v1/burn with source-native seeded rows."""

import time
from datetime import date, datetime, timedelta, timezone

import pytest

import burn
from alerting import process_alerts
from app import create_app
from conftest import DAY, make_feature


@pytest.fixture
def app(cfg, store):
    return create_app(cfg, run_pollers=False)


@pytest.fixture
def client(app):
    return app.test_client()


CALM = {"wind_mph": 5.0, "gusts_mph": 8.0, "humidity_pct": 55.0}


def row(**over):
    r = dict(CALM)
    r.update(over)
    return r


# ---- verdict unit tests: no_burn triggers ----

def test_calm_conditions_are_go(cfg):
    assert burn.evaluate(cfg, CALM, []) == ("go", [])


def test_sustained_wind_threshold(cfg):
    verdict, reasons = burn.evaluate(cfg, row(wind_mph=15.0), [])
    assert verdict == "no_burn"
    assert reasons == ["wind 15 mph"]
    # just under the limit (humidity high, no combo) is only caution
    verdict, _ = burn.evaluate(cfg, row(wind_mph=14.9), [])
    assert verdict == "caution"


def test_gusts_threshold(cfg):
    verdict, reasons = burn.evaluate(cfg, row(gusts_mph=27.0), [])
    assert verdict == "no_burn"
    assert reasons == ["gusts 27 mph"]
    assert burn.evaluate(cfg, row(gusts_mph=25.0), [])[0] == "no_burn"
    assert burn.evaluate(cfg, row(gusts_mph=24.9), [])[0] == "caution"


def test_humidity_threshold(cfg):
    verdict, reasons = burn.evaluate(cfg, row(humidity_pct=22.0), [])
    assert verdict == "no_burn"
    assert reasons == ["humidity 22%"]
    assert burn.evaluate(cfg, row(humidity_pct=25.0), [])[0] == "no_burn"
    assert burn.evaluate(cfg, row(humidity_pct=26.0), [])[0] == "caution"


def test_combo_wind_and_dry_air(cfg):
    verdict, reasons = burn.evaluate(cfg, row(wind_mph=10.0, humidity_pct=35.0), [])
    assert verdict == "no_burn"
    assert reasons == ["wind 10 mph with humidity 35%"]
    # either factor easing off the combo boundary drops back to caution
    assert burn.evaluate(cfg, row(wind_mph=9.9, humidity_pct=35.0), [])[0] == "caution"
    assert burn.evaluate(cfg, row(wind_mph=10.0, humidity_pct=36.0), [])[0] == "caution"


def test_combo_reason_not_duplicated_when_single_factor_fires(cfg):
    _, reasons = burn.evaluate(cfg, row(wind_mph=16.0, humidity_pct=30.0), [])
    assert reasons == ["wind 16 mph"]
    _, reasons = burn.evaluate(cfg, row(wind_mph=12.0, humidity_pct=20.0), [])
    assert reasons == ["humidity 20%"]


# ---- verdict unit tests: caution band boundaries ----

def test_wind_caution_band(cfg):
    verdict, reasons = burn.evaluate(cfg, row(wind_mph=10.0), [])
    assert (verdict, reasons) == ("caution", ["wind 10 mph"])
    assert burn.evaluate(cfg, row(wind_mph=9.9), [])[0] == "go"


def test_gusts_caution_band(cfg):
    verdict, reasons = burn.evaluate(cfg, row(gusts_mph=20.0), [])
    assert (verdict, reasons) == ("caution", ["gusts 20 mph"])
    assert burn.evaluate(cfg, row(gusts_mph=19.9), [])[0] == "go"


def test_humidity_caution_band(cfg):
    verdict, reasons = burn.evaluate(cfg, row(humidity_pct=40.0), [])
    assert (verdict, reasons) == ("caution", ["humidity 40%"])
    assert burn.evaluate(cfg, row(humidity_pct=40.1), [])[0] == "go"


def test_multiple_caution_triggers_all_listed(cfg):
    verdict, reasons = burn.evaluate(
        cfg, row(wind_mph=12.0, gusts_mph=21.0, humidity_pct=38.0), [])
    assert verdict == "caution"
    assert reasons == ["wind 12 mph", "gusts 21 mph", "humidity 38%"]


# ---- verdict unit tests: alert gating ----

def test_red_flag_warning_is_no_burn(cfg):
    verdict, reasons = burn.evaluate(cfg, CALM, [{"event": "Red Flag Warning"}])
    assert (verdict, reasons) == ("no_burn", ["Red Flag Warning active"])


def test_fire_weather_warning_is_no_burn(cfg):
    verdict, reasons = burn.evaluate(cfg, CALM, [{"event": "Fire Weather Warning"}])
    assert (verdict, reasons) == ("no_burn", ["Fire Weather Warning active"])


def test_fire_weather_watch_is_caution(cfg):
    verdict, reasons = burn.evaluate(cfg, CALM, [{"event": "Fire Weather Watch"}])
    assert (verdict, reasons) == ("caution", ["Fire Weather Watch active"])


def test_non_fire_alerts_never_trigger(cfg):
    """Heat, flood and tornado alerts are irrelevant to the burn pile."""
    for event in ("Extreme Heat Warning", "Flood Warning", "Tornado Warning",
                  "Wind Chill Advisory"):
        assert burn.evaluate(cfg, CALM, [{"event": event}]) == ("go", [])


# ---- verdict unit tests: missing data ----

def test_missing_wind_is_never_go(cfg):
    verdict, reasons = burn.evaluate(
        cfg, {"gusts_mph": 8.0, "humidity_pct": 55.0}, [])
    assert (verdict, reasons) == ("caution", ["insufficient data"])


def test_missing_humidity_is_never_go(cfg):
    verdict, reasons = burn.evaluate(
        cfg, {"wind_mph": 5.0, "gusts_mph": 8.0}, [])
    assert (verdict, reasons) == ("caution", ["insufficient data"])


def test_missing_everything_is_caution(cfg):
    assert burn.evaluate(cfg, {}, []) == ("caution", ["insufficient data"])
    assert burn.evaluate(cfg, None, []) == ("caution", ["insufficient data"])


def test_missing_data_does_not_soften_a_no_burn(cfg):
    """Gusts alone can still force no_burn with wind/humidity missing."""
    verdict, reasons = burn.evaluate(cfg, {"gusts_mph": 30.0}, [])
    assert (verdict, reasons) == ("no_burn", ["gusts 30 mph"])


# ---- look-ahead unit tests ----

def _future(cfg, rows):
    start = datetime.now(cfg.tzinfo).replace(minute=0, second=0, microsecond=0)
    return [((start + timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H:%M"), r)
            for i, r in enumerate(rows)]


def test_lookahead_reports_first_flip(cfg):
    future = _future(cfg, [CALM, CALM, row(wind_mph=20.0), row(wind_mph=22.0)])
    changes_at, next_verdict, next_reasons = burn.lookahead(
        cfg, "go", future, [], cfg.tzinfo)
    assert changes_at == future[2][0]
    assert next_verdict == "no_burn"
    assert next_reasons == ["wind 20 mph"]


def test_lookahead_no_flip_is_null(cfg):
    future = _future(cfg, [CALM] * 12)
    assert burn.lookahead(cfg, "go", future, [], cfg.tzinfo) == (None, None, None)


def test_lookahead_releases_when_alert_ends(cfg):
    """A Red Flag Warning ending mid-window flips no_burn back to go."""
    ends = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    alert = {"event": "Red Flag Warning", "ends": ends, "state": {}}
    future = _future(cfg, [CALM] * 6)
    changes_at, next_verdict, _ = burn.lookahead(
        cfg, "no_burn", future, [alert], cfg.tzinfo)
    assert next_verdict == "go"
    assert changes_at is not None


# ---- /v1/burn endpoint ----

def _native(r):
    """Canonical-ish test row -> Open-Meteo native hourly payload."""
    return {k: v for k, v in {
        "temperature_2m": 75.0,
        "wind_speed_10m": r.get("wind_mph"),
        "wind_gusts_10m": r.get("gusts_mph"),
        "relative_humidity_2m": r.get("humidity_pct"),
    }.items() if v is not None}


def _seed_burn_hourly(cfg, store, current=None, future=()):
    start = datetime.now(cfg.tzinfo).replace(minute=0, second=0, microsecond=0)
    rows = [(start.strftime("%Y-%m-%dT%H:%M"), _native(current or CALM))]
    rows += [((start + timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H:%M"), _native(r))
             for i, r in enumerate(future)]
    store.save_forecast_run("open_meteo", "hourly", store.default_location_id(),
                            time.time(), rows)
    store.set_source_success("open_meteo_forecast", time.time())
    store.set_source_success("nws_alerts", time.time())


def test_burn_503_before_first_poll(client):
    resp = client.get("/v1/burn")
    assert resp.status_code == 503
    assert "first successful poll" in resp.get_json()["error"]


def test_burn_go_envelope(cfg, store, client):
    _seed_burn_hourly(cfg, store, future=[CALM] * 14)
    data = client.get("/v1/burn").get_json()
    assert data["verdict"] == "go"
    assert data["reasons"] == []
    assert data["changes_at"] is None
    assert data["next_verdict"] is None
    assert data["source"] == "open_meteo"
    assert data["stale"] is False
    assert "fetched_at" in data
    assert data["timezone"] == cfg.tz


def test_burn_lookahead_flip_via_endpoint(cfg, store, client):
    """GO now, winds rise in hour 3: changes_at names that hour."""
    future = [CALM, CALM, row(wind_mph=18.0)] + [CALM] * 4
    _seed_burn_hourly(cfg, store, future=future)
    data = client.get("/v1/burn").get_json()
    assert data["verdict"] == "go"
    expected = (datetime.now(cfg.tzinfo).replace(minute=0, second=0, microsecond=0)
                + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    assert data["changes_at"] == expected
    assert data["next_verdict"] == "no_burn"
    assert data["next_reasons"] == ["wind 18 mph"]


def test_burn_lookahead_window_is_12_hours(cfg, store, client):
    """A flip in hour 13 is beyond the look-ahead window: no changes_at."""
    future = [CALM] * 12 + [row(wind_mph=25.0)]
    _seed_burn_hourly(cfg, store, future=future)
    data = client.get("/v1/burn").get_json()
    assert data["verdict"] == "go"
    assert data["changes_at"] is None
    assert data["next_verdict"] is None


def test_burn_red_flag_alert_blocks_via_endpoint(cfg, store, client):
    _seed_burn_hourly(cfg, store)
    feature = make_feature(event="Red Flag Warning", vtec=None, severity="Severe",
                           ends="2030-01-01T00:00:00-06:00",
                           expires="2030-01-01T00:00:00-06:00")
    process_alerts(cfg, store, [feature], now=DAY, send=lambda *a, **k: True)
    data = client.get("/v1/burn").get_json()
    assert data["verdict"] == "no_burn"
    assert "Red Flag Warning active" in data["reasons"]


def test_burn_extreme_heat_warning_does_not_block(cfg, store, client):
    """Non-fire alerts, even severity Extreme, must not touch the verdict."""
    _seed_burn_hourly(cfg, store)
    feature = make_feature(event="Extreme Heat Warning", vtec=None,
                           severity="Extreme",
                           ends="2030-01-01T00:00:00-06:00",
                           expires="2030-01-01T00:00:00-06:00")
    process_alerts(cfg, store, [feature], now=DAY, send=lambda *a, **k: True)
    assert client.get("/v1/alerts").get_json()["count"] == 1
    data = client.get("/v1/burn").get_json()
    assert data["verdict"] == "go"
    assert data["reasons"] == []


def test_burn_missing_fields_degrade_via_endpoint(cfg, store, client):
    _seed_burn_hourly(cfg, store, current={"gusts_mph": 8.0, "humidity_pct": 55.0})
    data = client.get("/v1/burn").get_json()
    assert data["verdict"] == "caution"
    assert data["reasons"] == ["insufficient data"]


def test_burn_stale_when_forecast_or_alerts_poller_stale(cfg, store, client):
    _seed_burn_hourly(cfg, store)
    assert client.get("/v1/burn").get_json()["stale"] is False
    # dead alert poller could hide a Red Flag Warning: badge stale
    store.set_source_success("nws_alerts", time.time() - 3600)
    assert client.get("/v1/burn").get_json()["stale"] is True
    # forecast staleness badges too (alerts fresh again)
    store.set_source_success("nws_alerts", time.time())
    store.set_source_success("open_meteo_forecast", time.time() - 4 * 3600)
    assert client.get("/v1/burn").get_json()["stale"] is True


# ---- multi-day outlook (unit) ----
# Fixed local dates: 2026-07-03 is a Friday, 2026-07-04 a Saturday.

OUTLOOK_DAY0 = date(2026, 7, 3)


def _outlook_rows(cfg, n_hours, overrides=None):
    """n hourly (local_iso, canonical_row) pairs from OUTLOOK_DAY0 00:00;
    overrides maps "YYYY-MM-DDTHH:MM" -> row dict."""
    overrides = overrides or {}
    start = datetime(2026, 7, 3, 0, 0)
    out = []
    for i in range(n_hours):
        ts = (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
        out.append((ts, overrides.get(ts, dict(CALM))))
    return out


def test_outlook_calm_day_blocked_by_next_day_wind(cfg):
    """Our fires burn ~2 days: gusts tomorrow afternoon block lighting on a
    perfectly calm today, with the reason attributed to tomorrow's hour."""
    rows = _outlook_rows(cfg, 96, {"2026-07-04T14:00": row(gusts_mph=27.0)})
    days = burn.outlook(cfg, rows, [], cfg.tzinfo, OUTLOOK_DAY0)
    assert days[0]["date"] == "2026-07-03"
    assert days[0]["verdict"] == "no_burn"
    assert days[0]["reasons"] == ["Sat 14:00: gusts 27 mph"]
    assert days[1]["verdict"] == "no_burn"  # windy hour is in its own window
    assert days[2]["verdict"] == "go"       # Jul 5 window starts after it


def test_outlook_window_spans_midnight(cfg):
    """Overnight wind matters to a smoldering pile: an 01:00 blow the next
    local day still lands in the previous day's window."""
    rows = _outlook_rows(cfg, 48, {"2026-07-04T01:00": row(wind_mph=20.0)})
    days = burn.outlook(cfg, rows, [], cfg.tzinfo, OUTLOOK_DAY0)
    assert days[0]["verdict"] == "no_burn"
    assert days[0]["reasons"] == ["Sat 01:00: wind 20 mph"]


def test_outlook_partial_coverage_and_omission(cfg):
    """72h of data: day 1 and 2 have full 48h windows, day 3 is partial,
    days with zero coverage are omitted entirely."""
    days = burn.outlook(cfg, _outlook_rows(cfg, 72), [], cfg.tzinfo, OUTLOOK_DAY0)
    assert [d["date"] for d in days] == ["2026-07-03", "2026-07-04", "2026-07-05"]
    assert [d["partial"] for d in days] == [False, False, True]
    assert all(d["verdict"] == "go" for d in days)


def test_outlook_low_confidence_from_day_4(cfg):
    days = burn.outlook(cfg, _outlook_rows(cfg, 168), [], cfg.tzinfo, OUTLOOK_DAY0)
    assert len(days) == 7
    assert [d["low_confidence"] for d in days] == [False, False, False,
                                                   True, True, True, True]
    assert days[5]["partial"] is False  # Jul 8: window fully inside the 168h
    assert days[6]["partial"] is True   # Jul 9: only 24 of 48 hours exist


def test_outlook_alert_active_window_interaction(cfg):
    """A Red Flag Warning ending Saturday noon blocks Friday and Saturday
    (their windows overlap its active hours) but not Sunday."""
    alert = {"event": "Red Flag Warning",
             "ends": "2026-07-04T12:00:00-05:00", "state": {}}
    days = burn.outlook(cfg, _outlook_rows(cfg, 96), [alert], cfg.tzinfo, OUTLOOK_DAY0)
    assert days[0]["verdict"] == "no_burn"
    assert days[0]["reasons"] == ["Fri 00:00: Red Flag Warning active"]
    assert days[1]["verdict"] == "no_burn"
    assert days[2]["verdict"] == "go"


def test_outlook_reasons_dedupe_to_worst_hour(cfg):
    rows = _outlook_rows(cfg, 48, {
        "2026-07-04T10:00": row(gusts_mph=26.0),
        "2026-07-04T14:00": row(gusts_mph=28.0),
        "2026-07-04T18:00": row(humidity_pct=20.0),
    })
    days = burn.outlook(cfg, rows, [], cfg.tzinfo, OUTLOOK_DAY0)
    assert days[0]["verdict"] == "no_burn"
    assert days[0]["reasons"] == ["Sat 14:00: gusts 28 mph",
                                  "Sat 18:00: humidity 20%"]


# ---- /v1/burn/outlook endpoint ----

def _seed_outlook_hourly(cfg, store, n_hours, windy_hour=None):
    """Seed source-native hourly rows from today 00:00 local; windy_hour
    (index from midnight) gets no_burn gusts."""
    day0 = datetime.now(cfg.tzinfo).replace(hour=0, minute=0, second=0,
                                            microsecond=0)
    rows = []
    for i in range(n_hours):
        ts = (day0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
        r = row(gusts_mph=27.0) if i == windy_hour else dict(CALM)
        rows.append((ts, _native(r)))
    store.save_forecast_run("open_meteo", "hourly", store.default_location_id(),
                            time.time(), rows)
    store.set_source_success("open_meteo_forecast", time.time())
    store.set_source_success("nws_alerts", time.time())


def test_outlook_endpoint_503_before_first_poll(client):
    resp = client.get("/v1/burn/outlook")
    assert resp.status_code == 503
    assert "first successful poll" in resp.get_json()["error"]


def test_outlook_endpoint_serves_days_with_envelope(cfg, store, client):
    _seed_outlook_hourly(cfg, store, 72, windy_hour=38)  # tomorrow 14:00
    data = client.get("/v1/burn/outlook").get_json()
    assert data["source"] == "open_meteo"
    assert data["stale"] is False
    assert "fetched_at" in data
    assert data["timezone"] == cfg.tz
    days = data["days"]
    assert len(days) == 3
    today = datetime.now(cfg.tzinfo).date()
    assert days[0]["date"] == today.isoformat()
    assert days[0]["verdict"] == "no_burn"      # tomorrow's blow is in window
    assert days[0]["reasons"][0].endswith("gusts 27 mph")
    assert days[0]["low_confidence"] is False
    assert days[1]["verdict"] == "no_burn"
    assert days[2]["verdict"] == "go"
    assert days[2]["partial"] is True


def test_outlook_endpoint_stale_when_alerts_poller_stale(cfg, store, client):
    _seed_outlook_hourly(cfg, store, 48)
    assert client.get("/v1/burn/outlook").get_json()["stale"] is False
    store.set_source_success("nws_alerts", time.time() - 3600)
    assert client.get("/v1/burn/outlook").get_json()["stale"] is True
