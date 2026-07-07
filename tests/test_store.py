"""Store round-trip and pruning tests. No network."""

import time


def test_forecast_run_round_trip(store):
    loc = store.default_location_id()
    rows = [("2026-07-03T13:00", {"temperature_2m": 91.4, "uv_index": 8.2}),
            ("2026-07-03T14:00", {"temperature_2m": 92.1, "uv_index": 7.9})]
    store.save_forecast_run("open_meteo", "hourly", loc, 1000.0, rows)
    run = store.latest_run("open_meteo", "hourly", loc)
    assert run["fetched_at"] == 1000.0
    assert run["rows"] == rows


def test_forecast_prune_keeps_last_n_runs(store):
    loc = store.default_location_id()
    for i in range(5):
        store.save_forecast_run("open_meteo", "hourly", loc, float(i),
                                [("2026-07-03T13:00", {"run": i})], keep_runs=2)
    assert store.run_count("open_meteo", "hourly", loc) == 2
    assert store.latest_run("open_meteo", "hourly", loc)["rows"][0][1] == {"run": 4}


def test_latest_run_empty_returns_none(store):
    assert store.latest_run("open_meteo", "hourly", store.default_location_id()) is None


def test_alert_upsert_preserves_notified_state(store):
    row = {"event_key": "KOUN.TO.W.0032.2026", "message_id": "m1", "event": "Tornado Warning",
           "severity": "Extreme", "affects_point": 1, "updated_at": 1.0,
           "geometry": '{"type": "Polygon"}'}
    store.upsert_alert(row)
    store.set_alert_state("KOUN.TO.W.0032.2026", {"notified": True, "tier": 1})

    # Update from a continuation message without geometry
    store.upsert_alert({"event_key": "KOUN.TO.W.0032.2026", "message_id": "m2",
                        "event": "Tornado Warning", "severity": "Extreme",
                        "affects_point": 1, "updated_at": 2.0, "geometry": None})
    got = store.get_alert("KOUN.TO.W.0032.2026")
    assert got["message_id"] == "m2"
    assert got["state"] == {"notified": True, "tier": 1}
    assert got["geometry"] == '{"type": "Polygon"}'  # survives a null update


def test_alert_message_mapping(store):
    store.record_alert_message("urn:oid:1", "KEY")
    assert store.event_key_for_message("urn:oid:1") == "KEY"
    assert store.event_key_for_message("urn:oid:2") is None
    assert store.event_key_for_message(None) is None


def test_source_status_success_and_failures(store):
    store.record_source_failure("nws_alerts", 10.0)
    store.record_source_failure("nws_alerts", 20.0)
    status = store.source_status("nws_alerts")
    assert status["failures"] == 2
    assert status["last_failure"] == 20.0
    assert status["last_success"] is None

    store.set_source_success("nws_alerts", 30.0)
    status = store.source_status("nws_alerts")
    assert status["last_success"] == 30.0
    assert status["failures"] == 2  # counter is cumulative, success does not reset it


def test_digest_queue_peek_then_delete(store):
    store.queue_digest("K1", "watch one", 1.0)
    store.queue_digest("K2", "watch two", 2.0)
    items = store.peek_digest()
    assert [i["summary"] for i in items] == ["watch one", "watch two"]
    assert store.peek_digest() == items  # peek does not drain
    store.delete_digest([items[0]["id"]])
    assert [i["summary"] for i in store.peek_digest()] == ["watch two"]
    store.delete_digest([items[1]["id"]])
    assert store.peek_digest() == []


def test_prune_alerts_drops_old_rows_and_orphaned_messages(store):
    store.upsert_alert({"event_key": "OLD.1", "message_id": "m1", "updated_at": 100.0})
    store.upsert_alert({"event_key": "NEW.1", "message_id": "m2", "updated_at": 900.0})
    store.record_alert_message("m1", "OLD.1")
    store.record_alert_message("m2", "NEW.1")
    store.prune_alerts(older_than=500.0)
    assert store.get_alert("OLD.1") is None
    assert store.get_alert("NEW.1") is not None
    assert store.event_key_for_message("m1") is None
    assert store.event_key_for_message("m2") == "NEW.1"


def test_sync_sources_seeds_and_drops(store):
    store.set_source_success("pirate_weather", 10.0)
    store.sync_sources(["nws_alerts", "open_meteo_forecast"])
    statuses = store.source_statuses()
    assert set(statuses) == {"nws_alerts", "open_meteo_forecast"}
    assert statuses["nws_alerts"]["last_success"] is None
    # Existing rows for still-enabled sources survive a re-sync
    store.set_source_success("nws_alerts", 20.0)
    store.sync_sources(["nws_alerts", "open_meteo_forecast"])
    assert store.source_status("nws_alerts")["last_success"] == 20.0


def test_default_location_seeded(store):
    locs = store.locations()
    assert len(locs) == 1
    assert locs[0]["name"] == "Home"
    assert locs[0]["is_default"] == 1


def test_observation_round_trip_and_freshness(store):
    now = time.time()
    store.insert_observation({"source": "station", "station_id": "station1",
                              "ts": now - 60, "temp": 88.0, "rh": 55.0})
    obs = store.latest_station_observation(900, now=now)
    assert obs["temp"] == 88.0
    assert store.latest_station_observation(30, now=now) is None  # too old for 30s window

    store.insert_observation({"source": "model", "station_id": None, "ts": now, "temp": 90.0})
    obs = store.latest_station_observation(900, now=now)
    assert obs["station_id"] == "station1"  # model rows never serve the station branch


def test_meta_round_trip(store):
    assert store.meta_get("k") is None
    store.meta_set("k", "v")
    assert store.meta_get("k") == "v"
    store.meta_set("k", "")
    assert store.meta_get("k") == ""
