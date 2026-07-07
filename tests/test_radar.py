"""Radar backend tests: frame naming, backfill math, warm/prune, fallback,
tile serving with strict path validation, frames.json, alerts.geojson and
Range support on the basemap. No network; HTTP is stubbed."""

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import radar
from app import create_app
from alerting import process_alerts
from conftest import DAY, HIT_POLYGON, make_feature

NOW = datetime(2026, 7, 4, 1, 33, 20, tzinfo=timezone.utc)
CURRENT = datetime(2026, 7, 4, 1, 30, tzinfo=timezone.utc)
PNG = radar.PNG_MAGIC + b"fake-png-body"


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(radar, "PACE_S", 0)


@pytest.fixture
def app(cfg, store):
    return create_app(cfg, run_pollers=False)


@pytest.fixture
def client(app):
    return app.test_client()


class FakeResp:
    def __init__(self, status=200, content=b"", payload=None):
        self.status_code = status
        self.content = content
        self._payload = payload or {}

    def json(self):
        return self._payload


def catalog_resp(layer_id="ridge::USCOMP-N0Q-202607040130"):
    return FakeResp(payload={"services": [
        {"id": "ridge::USCOMP-N0R-202607040130", "utc_valid": "2026-07-04T01:30:00Z"},
        {"id": layer_id, "utc_valid": "2026-07-04T01:30:00Z"},
    ], "generated_at": "2026-07-04T01:31:00Z"})


def fake_transport(monkeypatch, catalog=None, tile=PNG, wms=PNG, calls=None):
    """Stub radar.requests.get: routes by URL to catalog / tile / WMS
    responses. Pass bytes (200 body), an int (status with junk body) or
    None (raise as if the network died)."""
    calls = calls if calls is not None else []

    def render(spec):
        if spec is None:
            import requests
            raise requests.ConnectionError("stubbed network failure")
        if isinstance(spec, int):
            return FakeResp(status=spec, content=b"not a png")
        return FakeResp(content=spec)

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {})))
        if url == radar.CATALOG_URL:
            if catalog is None:
                import requests
                raise requests.ConnectionError("stubbed catalog failure")
            return catalog
        if url == radar.NOWCOAST_URL:
            return render(wms)
        return render(tile)

    monkeypatch.setattr(radar.requests, "get", fake_get)
    return calls


def seed_frame(store, dt, source="iem"):
    if source == "iem":
        layer = radar.layer_for(dt)
    else:
        layer = f"nowcoast-{dt.strftime('%Y%m%d%H%M')}"
    store.insert_radar_frame(layer, radar.iso_utc(dt), source, time.time())
    return layer


# ---- naming / parsing ----

def test_layer_roundtrip():
    layer = radar.layer_for(CURRENT)
    assert layer == "ridge::USCOMP-N0Q-202607040130"
    assert radar.parse_layer_time(layer) == CURRENT
    assert radar.iso_utc(CURRENT) == "2026-07-04T01:30:00Z"
    assert radar.iso_to_epoch("2026-07-04T01:30:00Z") == CURRENT.timestamp()
    assert radar.parse_layer_time("nowcoast-202607040130") == CURRENT


def test_parse_layer_rejects_junk():
    for junk in ("ridge::USCOMP-N0R-202607040130",   # wrong product
                 "ridge::USCOMP-N0Q-m05m",           # broken lag layers, unused
                 "ridge::USCOMP-N0Q-2026070401301",  # 13 digits
                 "ridge::USCOMP-N0Q-20260704013",    # 11 digits
                 "ridge::USCOMP-N0Q-209913999999",   # month 13
                 "../../etc/passwd", "frame.png", "", "nowcoast-abc"):
        assert radar.parse_layer_time(junk) is None, junk


def test_floor_frame():
    assert radar.floor_frame(NOW) == datetime(2026, 7, 4, 1, 30, tzinfo=timezone.utc)
    assert radar.floor_frame(CURRENT) == CURRENT


def test_location_tile_matches_slippy_math():
    # z7 x29 y50 is the tile over the example point, Norman OK (XYZ y, not TMS)
    assert radar.tile_at(35.22, -97.44, 7) == (29, 50)


def test_warm_tiles_default_zooms(cfg):
    tiles = radar.warm_tiles(cfg)
    assert len(tiles) == 6 * 9  # zooms 6-11, 3x3 block each
    for z, x, y in tiles:
        assert 6 <= z <= 11
        assert 0 <= x < (1 << z) and 0 <= y < (1 << z)
    assert (7, 29, 50) in tiles  # the location's own tile is warmed


# ---- config parsing ----

def test_radar_config_parsing(monkeypatch, tmp_path):
    from config import Config
    monkeypatch.setenv("DB_PATH", str(tmp_path / "aeolus.db"))
    monkeypatch.setenv("RADAR_WARM_ZOOMS", "4,6-8")
    monkeypatch.setenv("RADAR_ENABLED", "0")
    monkeypatch.setenv("RADAR_INTERVAL", "5")  # clamped to the polite floor
    cfg = Config.from_env()
    assert cfg.radar_bbox_tuple == (-103.5, 30.5, -91.5, 39.5)
    assert cfg.radar_warm_zoom_list == [4, 6, 7, 8]
    assert cfg.radar_enabled is False
    assert cfg.radar_interval == 30
    assert cfg.radar_cache_dir == str(tmp_path / "radar")
    assert cfg.basemap_path == str(tmp_path / "basemap.pmtiles")
    assert cfg.stale_after("iem_radar") == 150


# ---- catalog ----

def test_catalog_parse(cfg, monkeypatch):
    fake_transport(monkeypatch, catalog=catalog_resp())
    assert radar.fetch_catalog_time(cfg) == CURRENT


def test_catalog_without_n0q_raises(cfg, monkeypatch):
    fake_transport(monkeypatch, catalog=FakeResp(payload={"services": [
        {"id": "ridge::USCOMP-N0R-202607040130"}]}))
    with pytest.raises(RuntimeError, match="no USCOMP-N0Q"):
        radar.fetch_catalog_time(cfg)


# ---- backfill window math ----

def test_missing_frames_full_backfill(store):
    missing = radar.missing_frames(store, CURRENT, NOW, 6)
    assert len(missing) == 72  # 6h loop at 5-min cadence
    assert missing[0] == datetime(2026, 7, 3, 19, 35, tzinfo=timezone.utc)
    assert missing[-1] == CURRENT
    assert all(b - a == timedelta(minutes=5) for a, b in zip(missing, missing[1:]))


def test_missing_frames_returns_only_gaps(store):
    for m in (0, 5, 15, 25):  # 01:20 missing from the recent run
        seed_frame(store, CURRENT - timedelta(minutes=m))
    missing = radar.missing_frames(store, CURRENT, NOW, 1)
    assert datetime(2026, 7, 4, 1, 20, tzinfo=timezone.utc) in missing
    assert CURRENT not in missing
    # nowcoast frames do not satisfy the IEM grid
    seed_frame(store, datetime(2026, 7, 4, 1, 20, tzinfo=timezone.utc), "nowcoast")
    assert datetime(2026, 7, 4, 1, 20, tzinfo=timezone.utc) in radar.missing_frames(
        store, CURRENT, NOW, 1)


def test_missing_frames_never_future_and_complete_is_empty(store):
    t = radar.floor_frame(NOW - timedelta(hours=1)) + timedelta(minutes=5)
    while t <= CURRENT:
        seed_frame(store, t)
        t += timedelta(minutes=5)
    assert radar.missing_frames(store, CURRENT, NOW, 1) == []


# ---- poll: cold-start backfill, steady state, failure containment ----

def small_cfg(cfg):
    cfg.radar_loop_hours = 1
    cfg.radar_warm_zooms = "7"
    return cfg


def test_poll_cold_start_backfills_and_is_idempotent(cfg, store, monkeypatch):
    cfg = small_cfg(cfg)
    calls = fake_transport(monkeypatch, catalog=catalog_resp())
    radar.poll(cfg, store, now=NOW)
    frames = store.list_radar_frames()
    assert len(frames) == 12  # 1h loop
    assert frames[-1]["layer"] == "ridge::USCOMP-N0Q-202607040130"
    assert all(f["source"] == "iem" for f in frames)
    assert os.path.exists(radar.tile_path(cfg, frames[-1]["layer"], 7, 29, 50))

    tile_calls = len(calls)
    radar.poll(cfg, store, now=NOW)  # same catalog: nothing new to do
    assert len(store.list_radar_frames()) == 12
    assert len(calls) == tile_calls + 1  # only the catalog was refetched


def test_poll_all_tiles_failing_raises_but_keeps_frames(cfg, store, monkeypatch):
    cfg = small_cfg(cfg)
    fake_transport(monkeypatch, catalog=catalog_resp(), tile=503, wms=503)
    with pytest.raises(RuntimeError, match="tile fetches failed"):
        radar.poll(cfg, store, now=NOW)
    # frames are advertised anyway; tiles self-heal via fetch-on-demand
    assert len(store.list_radar_frames()) == 12


def test_backfill_aborts_early_when_tiles_die(cfg, store, monkeypatch):
    """The circuit breaker: a dead tile server mid-backfill must abort the
    tick after WARM_BREAKER consecutive failures, not grind through the
    whole loop's warm set at one timeout per tile."""
    cfg = small_cfg(cfg)
    calls = fake_transport(monkeypatch, catalog=catalog_resp(), tile=503, wms=503)
    with pytest.raises(RuntimeError, match="aborted"):
        radar.poll(cfg, store, now=NOW)
    tile_attempts = sum(1 for url, _ in calls if url != radar.CATALOG_URL
                        and url != radar.NOWCOAST_URL)
    # 12 frames x 9 warm tiles = 108 doomed fetches without the breaker;
    # with it, at most one extra concurrent batch past the threshold.
    assert tile_attempts <= radar.WARM_BREAKER + radar.BACKFILL_CONCURRENCY
    assert store.meta_get("radar_tiles_failing") == "1"
    assert len(store.list_radar_frames()) == 12  # rows still advertised


def test_backfill_warms_newest_first_with_concurrency(cfg, store, monkeypatch):
    cfg = small_cfg(cfg)
    fake_transport(monkeypatch, catalog=catalog_resp())
    warmed = []

    def spy(cfg_, layer, concurrency=1):
        warmed.append((layer, concurrency))
        return 1, 0

    monkeypatch.setattr(radar, "warm_frame", spy)
    radar.poll(cfg, store, now=NOW)
    layers = [w[0] for w in warmed]
    assert layers[0] == "ridge::USCOMP-N0Q-202607040130"  # newest first
    assert layers == sorted(layers, reverse=True)
    assert all(c == radar.BACKFILL_CONCURRENCY for _, c in warmed)

    # steady state: exactly one new frame -> back to the sequential path
    warmed.clear()
    fake_transport(monkeypatch, catalog=catalog_resp("ridge::USCOMP-N0Q-202607040135"))
    radar.poll(cfg, store, now=NOW + timedelta(minutes=5))
    assert warmed == [("ridge::USCOMP-N0Q-202607040135", 1)]


def test_warm_frame_concurrent_fetches_all_tiles(cfg, store, monkeypatch):
    cfg = small_cfg(cfg)
    fake_transport(monkeypatch, catalog=catalog_resp())
    layer = radar.layer_for(CURRENT)
    fetched, failed = radar.warm_frame(cfg, layer, radar.BACKFILL_CONCURRENCY)
    assert (fetched, failed) == (9, 0)  # zoom 7, 3x3 block
    for z, x, y in radar.warm_tiles(cfg):
        assert os.path.exists(radar.tile_path(cfg, layer, z, x, y))
    # idempotent: everything cached, nothing refetched
    assert radar.warm_frame(cfg, layer, radar.BACKFILL_CONCURRENCY) == (0, 0)


def test_warm_frame_breaker_fires_under_concurrency(cfg, store, monkeypatch):
    cfg = small_cfg(cfg)
    fake_transport(monkeypatch, catalog=catalog_resp(), tile=503)
    with pytest.raises(radar.TilesDown):
        radar.warm_frame(cfg, radar.layer_for(CURRENT), radar.BACKFILL_CONCURRENCY)


def test_poll_iem_down_engages_fallback_and_reraises(cfg, store, monkeypatch):
    cfg = small_cfg(cfg)
    fake_transport(monkeypatch, catalog=None, wms=PNG)  # catalog dead, WMS alive
    with pytest.raises(RuntimeError):
        radar.poll(cfg, store, now=NOW)
    frames = store.list_radar_frames()
    assert [f["source"] for f in frames] == ["nowcoast"]
    assert frames[0]["layer"] == "nowcoast-202607040130"
    assert frames[0]["valid_utc"] == "2026-07-04T01:30:00Z"
    assert os.path.exists(radar.frame_image_path(cfg, frames[0]["layer"]))
    assert store.meta_get("radar_fallback_active") == "1"


def test_fallback_engages_after_20_min_quiet_and_recovers(cfg, store, monkeypatch):
    cfg = small_cfg(cfg)
    # IEM catalog answers but is stuck 25 min in the past, window fully seeded
    stuck = radar.floor_frame(NOW - timedelta(minutes=25))
    t = radar.floor_frame(NOW - timedelta(hours=1)) + timedelta(minutes=5)
    while t <= stuck:
        seed_frame(store, t)
        t += timedelta(minutes=5)
    fake_transport(monkeypatch,
                   catalog=catalog_resp(radar.layer_for(stuck)), wms=PNG)
    radar.poll(cfg, store, now=NOW)  # succeeds: catalog answered
    assert store.newest_radar_frame()["source"] == "nowcoast"
    assert store.meta_get("radar_fallback_active") == "1"

    # same tick again: the 5-min slot is covered, no duplicate frame
    radar.poll(cfg, store, now=NOW)
    assert sum(1 for f in store.list_radar_frames() if f["source"] == "nowcoast") == 1

    # IEM recovers: fallback disengages
    fake_transport(monkeypatch, catalog=catalog_resp(), tile=PNG)
    radar.poll(cfg, store, now=NOW)
    assert store.meta_get("radar_fallback_active") == ""
    assert store.newest_radar_frame("iem")["layer"] == "ridge::USCOMP-N0Q-202607040130"


def test_fallback_needs_nowcoast_to_answer(cfg, store, monkeypatch):
    cfg = small_cfg(cfg)
    stuck = radar.floor_frame(NOW - timedelta(minutes=25))
    t = radar.floor_frame(NOW - timedelta(hours=1)) + timedelta(minutes=5)
    while t <= stuck:
        seed_frame(store, t)
        t += timedelta(minutes=5)
    fake_transport(monkeypatch,
                   catalog=catalog_resp(radar.layer_for(stuck)), wms=503)
    radar.poll(cfg, store, now=NOW)  # IEM catalog fine, WMS down: contained
    assert all(f["source"] == "iem" for f in store.list_radar_frames())
    assert not store.meta_get("radar_fallback_active")


# ---- pruning ----

def test_prune_by_age_drops_rows_dirs_and_orphans(cfg, store):
    old = seed_frame(store, NOW - timedelta(hours=7))
    new = seed_frame(store, NOW - timedelta(minutes=10))
    radar.write_atomic(radar.tile_path(cfg, old, 7, 29, 50), PNG)
    radar.write_atomic(radar.tile_path(cfg, new, 7, 29, 50), PNG)
    # orphan dir with an old timestamp name, plus junk that must survive
    orphan = radar.layer_for(NOW - timedelta(hours=8))
    radar.write_atomic(radar.tile_path(cfg, orphan, 7, 29, 50), PNG)
    junk = os.path.join(cfg.radar_cache_dir, "not-a-frame")
    os.makedirs(junk)

    radar.prune(cfg, store, NOW)
    assert store.get_radar_frame(old) is None
    assert store.get_radar_frame(new) is not None
    assert not os.path.exists(radar.frame_dir(cfg, old))
    assert not os.path.exists(radar.frame_dir(cfg, orphan))
    assert os.path.exists(radar.tile_path(cfg, new, 7, 29, 50))
    assert os.path.isdir(junk)


def test_prune_by_size_evicts_oldest_first(cfg, store):
    cfg.radar_cache_max_mb = 1
    layers = [seed_frame(store, NOW - timedelta(minutes=m)) for m in (15, 10, 5)]
    for layer in layers:
        radar.write_atomic(radar.tile_path(cfg, layer, 7, 29, 50),
                           PNG + b"\0" * 600_000)
    radar.prune(cfg, store, NOW)  # 1.8 MB > 1 MB: evict two oldest
    remaining = store.list_radar_frames()
    assert [f["layer"] for f in remaining] == [layers[2]]
    assert not os.path.exists(radar.frame_dir(cfg, layers[0]))
    assert not os.path.exists(radar.frame_dir(cfg, layers[1]))
    assert radar.cache_size_bytes(cfg) <= 1024 * 1024


# ---- store round trip ----

def test_radar_frame_store_roundtrip(store):
    a = seed_frame(store, CURRENT - timedelta(minutes=5))
    b = seed_frame(store, CURRENT, "nowcoast")
    store.insert_radar_frame(a, radar.iso_utc(CURRENT - timedelta(minutes=5)),
                             "iem", 999.0)  # duplicate insert is a no-op
    assert [f["layer"] for f in store.list_radar_frames()] == [a, b]
    assert store.newest_radar_frame()["layer"] == b
    assert store.newest_radar_frame("iem")["layer"] == a
    assert store.oldest_radar_frame()["layer"] == a
    doomed = store.prune_radar_frames(radar.iso_utc(CURRENT))
    assert doomed == [a]
    store.delete_radar_frame(b)
    assert store.list_radar_frames() == []


# ---- frames.json ----

def test_frames_json_shape(cfg, store, client):
    seed_frame(store, CURRENT - timedelta(minutes=10))
    seed_frame(store, CURRENT - timedelta(minutes=5))
    nc = seed_frame(store, CURRENT, "nowcoast")
    store.set_source_success("iem_radar", time.time())

    resp = client.get("/v1/radar/frames.json")
    assert resp.headers["Cache-Control"] == "public, max-age=60"
    data = resp.get_json()
    assert data["stale"] is False
    assert data["fetched_at"] is not None
    assert data["source"] == "nowcoast"  # newest frame is a fallback frame
    assert data["bbox"] == [-103.5, 30.5, -91.5, 39.5]
    assert data["loop_hours"] == 6
    assert data["tile_url"] == "/v1/radar/tiles/{layer}/{z}/{x}/{y}.png"
    assert data["count"] == 3
    valids = [f["valid"] for f in data["frames"]]
    assert valids == sorted(valids)  # oldest first = animation order
    iem = data["frames"][0]
    assert set(iem) == {"layer", "valid", "source"}
    fallback = data["frames"][-1]
    assert fallback["source"] == "nowcoast"
    assert fallback["url"] == f"/v1/radar/frames/{nc}.png"
    assert fallback["bbox"] == [-103.5, 30.5, -91.5, 39.5]


def test_frames_json_empty_is_200_and_stale(client):
    data = client.get("/v1/radar/frames.json").get_json()
    assert data["count"] == 0 and data["frames"] == []
    assert data["stale"] is True  # radar poller has never succeeded
    assert data["source"] == "iem"


# ---- tile endpoint ----

def test_tile_served_from_disk_cache(cfg, store, client, monkeypatch):
    layer = seed_frame(store, CURRENT)
    radar.write_atomic(radar.tile_path(cfg, layer, 7, 29, 50), PNG)
    monkeypatch.setattr(radar, "fetch_tile",
                        lambda *a: pytest.fail("cache hit must not refetch"))
    resp = client.get(f"/v1/radar/tiles/{layer}/7/29/50.png")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert resp.data == PNG


def test_tile_fetch_on_demand_then_cached(cfg, store, client, monkeypatch):
    layer = seed_frame(store, CURRENT)
    fetches = []

    def fake_fetch(cfg_, layer_, z, x, y, timeout=None):
        fetches.append((layer_, z, x, y))
        return PNG

    monkeypatch.setattr(radar, "fetch_tile", fake_fetch)
    assert client.get(f"/v1/radar/tiles/{layer}/7/29/50.png").status_code == 200
    assert fetches == [(layer, 7, 29, 50)]
    assert os.path.exists(radar.tile_path(cfg, layer, 7, 29, 50))
    assert client.get(f"/v1/radar/tiles/{layer}/7/29/50.png").data == PNG
    assert len(fetches) == 1  # second hit came from disk


def test_tile_upstream_failure_is_502(cfg, store, client, monkeypatch):
    layer = seed_frame(store, CURRENT)
    monkeypatch.setattr(radar, "fetch_tile", lambda *a: None)
    resp = client.get(f"/v1/radar/tiles/{layer}/7/29/50.png")
    assert resp.status_code == 502
    assert "upstream" in resp.get_json()["error"]


def test_tile_unknown_out_of_window_frame_is_404_never_proxied(cfg, store, client,
                                                               monkeypatch):
    """Unknown frames outside loop+grace are a hard 404 (inside the window
    they refetch on demand instead; see the grace tests below)."""
    monkeypatch.setattr(radar, "fetch_tile",
                        lambda *a: pytest.fail("out-of-window frames must not be fetched"))
    resp = client.get("/v1/radar/tiles/ridge::USCOMP-N0Q-202001010000/7/29/50.png")
    assert resp.status_code == 404


# ---- tile grace window (prune race) ----

def test_frame_within_grace_bounds(cfg):
    now = datetime.now(timezone.utc)
    inside = radar.layer_for(radar.floor_frame(
        now - timedelta(hours=cfg.radar_loop_hours, minutes=10)))
    beyond = radar.layer_for(radar.floor_frame(
        now - timedelta(hours=cfg.radar_loop_hours,
                        seconds=radar.PRUNE_GRACE_S + 600)))
    future = radar.layer_for(radar.floor_frame(now + timedelta(hours=1)))
    assert radar.frame_within_grace(cfg, inside, now=now)
    assert not radar.frame_within_grace(cfg, beyond, now=now)
    assert not radar.frame_within_grace(cfg, future, now=now)
    assert not radar.frame_within_grace(cfg, "ridge::USCOMP-N0Q-m05m", now=now)
    assert not radar.frame_within_grace(cfg, "junk", now=now)


def test_tile_grace_refetches_just_pruned_frame(cfg, store, client, monkeypatch):
    """The prune race: a client with a ~2-min-stale frames.json requests a
    tile for a frame the 90s prune tick just dropped. Within PRUNE_GRACE_S
    of the loop boundary the endpoint refetches from IEM on demand (IEM
    keeps timestamped frames for at least a year) instead of 404ing the
    animation into a blank frame."""
    dt = radar.floor_frame(datetime.now(timezone.utc)
                           - timedelta(hours=cfg.radar_loop_hours, minutes=10))
    layer = radar.layer_for(dt)
    assert store.get_radar_frame(layer) is None  # pruned: no row anymore
    fetches = []

    def fake_fetch(cfg_, layer_, z, x, y, timeout=None):
        fetches.append((layer_, z, x, y))
        return PNG

    monkeypatch.setattr(radar, "fetch_tile", fake_fetch)
    resp = client.get(f"/v1/radar/tiles/{layer}/7/29/50.png")
    assert resp.status_code == 200
    assert resp.data == PNG
    assert fetches == [(layer, 7, 29, 50)]
    assert client.get(f"/v1/radar/tiles/{layer}/7/29/50.png").data == PNG
    assert len(fetches) == 1  # second hit came from the disk cache


def test_tile_beyond_grace_is_404(cfg, store, client, monkeypatch):
    monkeypatch.setattr(radar, "fetch_tile",
                        lambda *a: pytest.fail("beyond-grace frames must not be fetched"))
    dt = radar.floor_frame(datetime.now(timezone.utc) - timedelta(
        hours=cfg.radar_loop_hours, seconds=radar.PRUNE_GRACE_S + 900))
    resp = client.get(f"/v1/radar/tiles/{radar.layer_for(dt)}/7/29/50.png")
    assert resp.status_code == 404


def test_tile_grace_upstream_failure_is_502(cfg, store, client, monkeypatch):
    monkeypatch.setattr(radar, "fetch_tile", lambda *a: None)
    dt = radar.floor_frame(datetime.now(timezone.utc)
                           - timedelta(hours=cfg.radar_loop_hours, minutes=10))
    resp = client.get(f"/v1/radar/tiles/{radar.layer_for(dt)}/7/29/50.png")
    assert resp.status_code == 502


def test_tile_path_validation_rejects_traversal_and_junk(cfg, store, client, monkeypatch):
    layer = seed_frame(store, CURRENT)
    monkeypatch.setattr(radar, "fetch_tile",
                        lambda *a: pytest.fail("invalid paths must not be fetched"))
    bad = [
        "/v1/radar/tiles/ridge::USCOMP-N0Q-m05m/7/29/50.png",     # broken lag layer
        "/v1/radar/tiles/ridge::USCOMP-N0R-202607040130/7/29/50.png",  # other product
        "/v1/radar/tiles/..%2F..%2Fsecrets/7/29/50.png",          # traversal
        f"/v1/radar/tiles/{layer}/13/0/0.png",                    # zoom out of range
        f"/v1/radar/tiles/{layer}/7/128/48.png",                  # x >= 2^z
        f"/v1/radar/tiles/{layer}/7/30/128.png",                  # y >= 2^z
        f"/v1/radar/tiles/{layer}/7/30/-1.png",                   # negative index
        f"/v1/radar/tiles/{layer}/7/30/abc.png",                  # non-integer
    ]
    for url in bad:
        assert client.get(url).status_code == 404, url


# ---- nowCOAST whole-frame endpoint ----

def test_nowcoast_frame_served_and_refetched(cfg, store, client, monkeypatch):
    layer = seed_frame(store, CURRENT, "nowcoast")
    radar.write_atomic(radar.frame_image_path(cfg, layer), PNG)
    resp = client.get(f"/v1/radar/frames/{layer}.png")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert resp.data == PNG

    os.remove(radar.frame_image_path(cfg, layer))  # disk loss heals via WMS
    monkeypatch.setattr(radar, "fetch_nowcoast_image", lambda cfg_, when: PNG)
    assert client.get(f"/v1/radar/frames/{layer}.png").data == PNG

    assert client.get("/v1/radar/frames/nowcoast-209901010000.png").status_code == 404
    assert client.get(f"/v1/radar/frames/{radar.layer_for(CURRENT)}.png").status_code == 404


# ---- alerts.geojson ----

def test_alerts_geojson_carries_active_polygons(cfg, store, client):
    future = "2030-01-01T00:00:00-06:00"
    feature = make_feature(geometry=HIT_POLYGON, ends=future, expires=future)
    process_alerts(cfg, store, [feature], now=DAY, send=lambda *a, **k: True)
    store.set_source_success("nws_alerts", time.time())

    resp = client.get("/v1/radar/alerts.geojson")
    assert resp.headers["Cache-Control"] == "public, max-age=60"
    data = resp.get_json()
    assert data["type"] == "FeatureCollection"
    assert data["stale"] is False
    assert len(data["features"]) == 1
    f = data["features"][0]
    assert f["geometry"] == HIT_POLYGON
    assert f["properties"] == {"event": "Tornado Warning", "severity": "Extreme",
                               "headline": "Tornado Warning until 2 PM",
                               "affects_point": True}


def test_alerts_geojson_excludes_expired_and_geometryless(cfg, store, client):
    assert client.get("/v1/radar/alerts.geojson").get_json()["features"] == []
    # geometry-less zone product: active but nothing to draw
    zone = make_feature(msg_id="urn:oid:2.49.0.1.840.0.200", geometry=None,
                        vtec="/O.NEW.KOUN.SV.A.0100.260703T1730Z-300101T0000Z/",
                        ends="2030-01-01T00:00:00-06:00",
                        expires="2030-01-01T00:00:00-06:00")
    # polygon alert that already ended
    done = make_feature(geometry=HIT_POLYGON)
    process_alerts(cfg, store, [zone, done], now=DAY, send=lambda *a, **k: True)
    data = client.get("/v1/radar/alerts.geojson").get_json()
    assert data["features"] == []


# ---- basemap with Range support ----

BASEMAP_BYTES = b"0123456789ABCDEF"


def test_basemap_full_and_range_requests(cfg, client):
    with open(cfg.basemap_path, "wb") as f:
        f.write(BASEMAP_BYTES)

    full = client.get("/basemap.pmtiles")
    assert full.status_code == 200
    assert full.headers["Accept-Ranges"] == "bytes"
    assert full.data == BASEMAP_BYTES

    head = client.get("/basemap.pmtiles", headers={"Range": "bytes=0-3"})
    assert head.status_code == 206
    assert head.data == b"0123"
    assert head.headers["Content-Range"] == "bytes 0-3/16"

    mid = client.get("/basemap.pmtiles", headers={"Range": "bytes=4-9"})
    assert mid.status_code == 206 and mid.data == b"456789"

    tail = client.get("/basemap.pmtiles", headers={"Range": "bytes=-4"})
    assert tail.status_code == 206 and tail.data == b"CDEF"


def test_basemap_missing_is_404(cfg, client):
    assert not os.path.exists(cfg.basemap_path)
    resp = client.get("/basemap.pmtiles")
    assert resp.status_code == 404
    assert "not installed" in resp.get_json()["error"]


# ---- metrics ----

def test_metrics_radar_gauges(cfg, store, client):
    seed_frame(store, datetime.now(timezone.utc) - timedelta(minutes=7))
    layer = seed_frame(store, datetime.now(timezone.utc) - timedelta(minutes=2))
    radar.write_atomic(radar.tile_path(cfg, layer, 7, 29, 50), PNG)
    store.set_source_success("iem_radar", time.time())

    body = client.get("/metrics").get_data(as_text=True)
    assert "aeolus_radar_frames 2.0" in body
    assert "aeolus_radar_newest_frame_age_seconds" in body
    assert 'aeolus_source_stale{source="iem_radar"} 0.0' in body
    for line in body.splitlines():
        if line.startswith("aeolus_radar_cache_bytes"):
            assert float(line.split()[-1]) >= len(PNG)
            break
    else:
        pytest.fail("aeolus_radar_cache_bytes missing")


def test_metrics_newest_frame_age_clamps_at_zero(store, client):
    # IEM sometimes advertises a frame stamped slightly ahead of wall clock;
    # the age gauge must clamp at 0, never read negative.
    seed_frame(store, radar.floor_frame(datetime.now(timezone.utc)
                                        + timedelta(minutes=5)))
    body = client.get("/metrics").get_data(as_text=True)
    for line in body.splitlines():
        if line.startswith("aeolus_radar_newest_frame_age_seconds"):
            assert float(line.split()[-1]) == 0.0
            break
    else:
        pytest.fail("aeolus_radar_newest_frame_age_seconds missing")


def test_metrics_radar_empty_state(client):
    body = client.get("/metrics").get_data(as_text=True)
    assert "aeolus_radar_frames 0.0" in body
    assert "aeolus_radar_newest_frame_age_seconds" not in body
    assert "aeolus_radar_cache_bytes 0.0" in body
