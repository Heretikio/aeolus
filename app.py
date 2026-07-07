"""Aeolus Flask app: JSON API + /metrics + the PWA shell.

Serve last-known-good ALWAYS. Every payload carries source, fetched_at and a
stale flag so UIs can badge honestly. A 503 happens only before the very
first successful poll.

The frontend is a static, self-contained PWA under static/ (no CDN, no
external fonts); this app serves its shell at /, the manifest at
/manifest.json and the service worker at /sw.js (root scope).
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from flask import Flask, Response, abort, jsonify, make_response, request, send_file
from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

import adapters
import alerting
import burn
import pollers
import radar
from config import Config
from store import Store

log = logging.getLogger("aeolus.app")

# Which poller's last_success governs staleness for each (source, kind)
STATUS_SOURCE = {
    ("open_meteo", "hourly"): "open_meteo_forecast",
    ("open_meteo", "daily"): "open_meteo_forecast",
    ("open_meteo", "minutely15"): "open_meteo_nowcast",
    ("pirate_weather", "hourly"): "pirate_weather",
    ("pirate_weather", "daily"): "pirate_weather",
    ("pirate_weather", "minutely15"): "pirate_weather",
}
SOURCE_PREFERENCE = ("open_meteo", "pirate_weather")
STATION_MAX_AGE_S = 900  # station beats the model when fresher than 15 min


def is_stale(cfg, status: dict | None, source: str, now: float | None = None) -> bool:
    """A source is stale when it has never succeeded or its last success is
    older than the configured freshness window."""
    now = now if now is not None else time.time()
    if not status or not status.get("last_success"):
        return True
    return (now - status["last_success"]) > cfg.stale_after(source)


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def leader_alive(store, now: float) -> bool:
    """True while a live poller-leader heartbeat exists. Tolerates a
    corrupt meta value; monitoring must not 500 when things are broken."""
    heartbeat = store.meta_get("leader_heartbeat")
    try:
        return bool(heartbeat) and now - float(heartbeat) < 120
    except (TypeError, ValueError):
        return False


class AeolusCollector:
    """Reads all metric state from the DB at scrape time, so any gunicorn
    worker reports identical numbers regardless of which one leads."""

    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store

    def collect(self):
        now = time.time()
        last = GaugeMetricFamily("aeolus_source_last_success_timestamp_seconds",
                                 "Unix time of last successful poll per source",
                                 labels=["source"])
        stale = GaugeMetricFamily("aeolus_source_stale",
                                  "1 when the source is past its freshness window",
                                  labels=["source"])
        fails = CounterMetricFamily("aeolus_source_failures",
                                    "Cumulative poll failures per source",
                                    labels=["source"])
        for name, status in self.store.source_statuses().items():
            last.add_metric([name], status.get("last_success") or 0)
            stale.add_metric([name], 1.0 if is_stale(self.cfg, status, name, now) else 0.0)
            fails.add_metric([name], status.get("failures") or 0)
        yield last
        yield stale
        yield fails

        now_utc = datetime.now(timezone.utc)
        active = sum(1 for a in self.store.list_alerts() if alerting.alert_is_active(a, now_utc))
        gauge = GaugeMetricFamily("aeolus_active_alerts", "Active alerts in the local cache")
        gauge.add_metric([], active)
        yield gauge

        leader = GaugeMetricFamily("aeolus_poller_leader",
                                   "1 when a live poller leader heartbeat exists")
        leader.add_metric([], 1.0 if leader_alive(self.store, now) else 0.0)
        yield leader

        frames = self.store.list_radar_frames()
        count = GaugeMetricFamily("aeolus_radar_frames",
                                  "Radar frames in the loop cache")
        count.add_metric([], len(frames))
        yield count
        if frames:
            try:  # a corrupt row must not 500 the scrape
                newest = radar.iso_to_epoch(frames[-1]["valid_utc"])
                age = GaugeMetricFamily("aeolus_radar_newest_frame_age_seconds",
                                        "Age of the newest radar frame")
                age.add_metric([], max(0.0, now - newest))
                yield age
            except ValueError:
                pass
        size = GaugeMetricFamily("aeolus_radar_cache_bytes",
                                 "Bytes on disk in the radar frame cache")
        try:
            # prune stashes the measured size each radar tick; the scrape
            # must not walk a tree that can hold tens of thousands of tiles
            cache_bytes = float(self.store.meta_get("radar_cache_bytes"))
        except (TypeError, ValueError):
            cache_bytes = float(radar.cache_size_bytes(self.cfg))
        size.add_metric([], cache_bytes)
        yield size


def create_app(cfg: Config | None = None, run_pollers: bool | None = None) -> Flask:
    cfg = cfg or Config.from_env()
    if run_pollers is None:
        run_pollers = os.environ.get("AEOLUS_POLLERS", "1") != "0"

    store = Store(cfg.db_path)
    store.ensure_schema()
    store.seed_default_location(cfg.location_name, cfg.lat, cfg.lon)

    app = Flask(__name__)
    registry = CollectorRegistry()
    registry.register(AeolusCollector(cfg, store))

    def _status_for(source: str, kind: str):
        name = STATUS_SOURCE.get((source, kind), source)
        return name, store.source_status(name)

    def _location_id() -> int:
        """Optional ?loc= selects a saved location. Non-integer junk or a
        missing value falls back to the default so links stay shareable; an
        integer that is not a saved location is a 404, not a misleading 503.
        v1 pollers only fill the default; other saved locations 503 until
        they have data."""
        raw = request.args.get("loc")
        if raw:
            try:
                loc = int(raw)
            except ValueError:
                return store.default_location_id()
            if not store.location_exists(loc):
                abort(make_response(jsonify({"error": "unknown location"}), 404))
            return loc
        return store.default_location_id()

    def _run_any(kind: str, location_id: int):
        """Freshest usable run across sources: the first non-stale source in
        preference order wins; when every source is stale, serve the run
        with the newest fetched_at (badged stale). A dead primary must not
        shadow a fresh secondary, that is what the second source is for.

        Rows are adapted to the canonical Aeolus schema (adapters.py) at
        serve time, so consumers see one schema no matter which source's
        native payloads are stored."""
        fallback = None
        chosen = None
        for source in SOURCE_PREFERENCE:
            run = store.latest_run(source, kind, location_id)
            if not run:
                continue
            name, status = _status_for(source, kind)
            if not is_stale(cfg, status, name):
                chosen = (source, run)
                break
            if fallback is None or run["fetched_at"] > fallback[1]["fetched_at"]:
                fallback = (source, run)
        source, run = chosen or fallback or (None, None)
        if run:
            run = {**run, "rows": adapters.adapt_rows(source, kind, run["rows"], cfg.tzinfo)}
        return source, run

    def _no_data():
        return jsonify({"error": "no data yet; first successful poll pending"}), 503

    def _envelope(source: str, kind: str, run: dict, extra: dict) -> dict:
        name, status = _status_for(source, kind)
        return {
            "source": source,
            "fetched_at": _iso_utc(run["fetched_at"]),
            "stale": is_stale(cfg, status, name),
            "timezone": cfg.tz,
            **extra,
        }

    def _int_arg(name: str, default: int) -> int:
        try:
            return int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    def _now_local() -> datetime:
        return datetime.now(cfg.tzinfo)

    @app.get("/v1/current")
    def current():
        location_id = _location_id()
        # Station branch: preferred when fresher than 15 min, default location
        # only (a station lives at one point). Stub until the optional local
        # station ingest lands; observations stay empty so the model serves.
        if location_id == store.default_location_id():
            obs = store.latest_station_observation(STATION_MAX_AGE_S)
            if obs:
                return jsonify({
                    "source": "station",
                    "fetched_at": _iso_utc(obs["ts"]),
                    "stale": False,
                    "timezone": cfg.tz,
                    "current": {k: obs[k] for k in obs if k not in ("id",)},
                })
        source, run = _run_any("hourly", location_id)
        if not run:
            return _no_data()
        cutoff = _now_local().strftime("%Y-%m-%dT%H:00")
        past = [(ts, p) for ts, p in run["rows"] if ts <= cutoff]
        ts, payload = past[-1] if past else run["rows"][0]
        return jsonify(_envelope(source, "hourly", run, {"current": {"ts": ts, **payload}}))

    @app.get("/v1/hourly")
    def hourly():
        hours = max(1, min(_int_arg("h", 48), 384))
        source, run = _run_any("hourly", _location_id())
        if not run:
            return _no_data()
        cutoff = _now_local().strftime("%Y-%m-%dT%H:00")
        rows = [{"ts": ts, **p} for ts, p in run["rows"] if ts >= cutoff][:hours]
        return jsonify(_envelope(source, "hourly", run, {"hourly": rows}))

    @app.get("/v1/daily")
    def daily():
        # 16 days are stored; serve at most 10 (skill collapses past day 8).
        days = max(1, min(_int_arg("d", 10), 10))
        source, run = _run_any("daily", _location_id())
        if not run:
            return _no_data()
        cutoff = _now_local().strftime("%Y-%m-%d")
        rows = [{"ts": ts, **p} for ts, p in run["rows"] if ts >= cutoff][:days]
        return jsonify(_envelope(source, "daily", run, {"daily": rows}))

    @app.get("/v1/nowcast")
    def nowcast():
        source, run = _run_any("minutely15", _location_id())
        if not run:
            return _no_data()
        local = _now_local()
        cutoff = local.replace(minute=(local.minute // 15) * 15).strftime("%Y-%m-%dT%H:%M")
        rows = [{"ts": ts, **p} for ts, p in run["rows"] if ts >= cutoff]
        return jsonify(_envelope(source, "minutely15", run, {"nowcast": rows}))

    @app.get("/v1/alerts")
    def alerts():
        if not cfg.alerts_enabled:
            # US-only feature turned off (e.g. a non-US deployment). Report an
            # honest empty, not a stale error against a poller that never runs.
            return jsonify({"source": "disabled", "fetched_at": None,
                            "stale": False, "alerts": [], "count": 0})
        now_utc = datetime.now(timezone.utc)
        active = [a for a in store.list_alerts() if alerting.alert_is_active(a, now_utc)]
        items = [{
            "event_key": a["event_key"],
            "event": a["event"],
            "severity": a["severity"],
            "certainty": a["certainty"],
            "urgency": a["urgency"],
            "headline": a["headline"],
            "nws_headline": a["nws_headline"],
            "onset": a["onset"],
            "ends": a["ends"],
            "expires": a["expires"],
            "area_desc": a["area_desc"],
            "affects_point": bool(a["affects_point"]),
            "instruction": a["instruction"],
        } for a in active]
        status = store.source_status("nws_alerts")
        return jsonify({
            "source": "nws",
            "fetched_at": _iso_utc(status["last_success"])
            if status and status.get("last_success") else None,
            "stale": is_stale(cfg, status, "nws_alerts"),
            "alerts": items,
            "count": len(items),
        })

    @app.get("/v1/burn")
    def burn_window():
        """Burn-pile verdict for the default location only (a burn pile does
        not move). Current conditions come from the model's current-hour row
        (the optional station branch joins later); active alerts gate the
        verdict; the next hours are scanned for a flip."""
        location_id = store.default_location_id()
        source, run = _run_any("hourly", location_id)
        if not run:
            return _no_data()
        cutoff = _now_local().strftime("%Y-%m-%dT%H:00")
        past = [(ts, p) for ts, p in run["rows"] if ts <= cutoff]
        _, current_row = past[-1] if past else run["rows"][0]
        future = [(ts, p) for ts, p in run["rows"]
                  if ts > cutoff][:cfg.burn_lookahead_hours]

        all_alerts = store.list_alerts()
        now_utc = datetime.now(timezone.utc)
        active = [a for a in all_alerts if alerting.alert_is_active(a, now_utc)]
        verdict, reasons = burn.evaluate(cfg, current_row, active)
        changes_at, next_verdict, next_reasons = burn.lookahead(
            cfg, verdict, future, all_alerts, cfg.tzinfo)

        payload = _envelope(source, "hourly", run, {
            "verdict": verdict,
            "reasons": reasons,
            "changes_at": changes_at,
            "next_verdict": next_verdict,
            "next_reasons": next_reasons,
        })
        # The verdict leans on the forecast AND the alert cache: a dead
        # alert poller can hide a Red Flag Warning, so either going stale
        # badges the whole payload.
        if cfg.alerts_enabled:
            payload["stale"] = payload["stale"] or is_stale(
                cfg, store.source_status("nws_alerts"), "nws_alerts")
        return jsonify(payload)

    @app.get("/v1/burn/outlook")
    def burn_outlook():
        """Multi-day burn outlook for the default location: one verdict per day
        covering the fire's whole ~2-day life (worst hour in [00:00 local,
        +window)), for the DAILY view's chips. Same staleness as /v1/burn."""
        source, run = _run_any("hourly", store.default_location_id())
        if not run:
            return _no_data()
        days = burn.outlook(cfg, run["rows"], store.list_alerts(), cfg.tzinfo,
                            _now_local().date())
        payload = _envelope(source, "hourly", run, {"days": days})
        if cfg.alerts_enabled:
            payload["stale"] = payload["stale"] or is_stale(
                cfg, store.source_status("nws_alerts"), "nws_alerts")
        return jsonify(payload)

    @app.get("/v1/locations")
    def locations():
        return jsonify({
            "source": "local",
            "fetched_at": _iso_utc(time.time()),
            "stale": False,
            "locations": store.locations(),
        })

    # ---- radar (v2): frame list, tile proxy-cache, alert polygons, basemap ----

    @app.get("/v1/radar/frames.json")
    def radar_frames():
        """The animation loop, oldest first. IEM frames are tiled (use
        tile_url); nowCOAST fallback frames are whole-bbox images and carry
        their own url + bbox. Top-level source reflects the newest frame."""
        frames = []
        for row in store.list_radar_frames():
            item = {"layer": row["layer"], "valid": row["valid_utc"],
                    "source": row["source"]}
            if row["source"] == "nowcoast":
                item["url"] = f"/v1/radar/frames/{row['layer']}.png"
                item["bbox"] = list(cfg.radar_bbox_tuple)
            frames.append(item)
        status = store.source_status("iem_radar")
        resp = jsonify({
            "source": frames[-1]["source"] if frames else "iem",
            "fetched_at": _iso_utc(status["last_success"])
            if status and status.get("last_success") else None,
            "stale": is_stale(cfg, status, "iem_radar"),
            "bbox": list(cfg.radar_bbox_tuple),
            "loop_hours": cfg.radar_loop_hours,
            "tile_url": "/v1/radar/tiles/{layer}/{z}/{x}/{y}.png",
            "frames": frames,
            "count": len(frames),
        })
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp

    @app.get("/v1/radar/tiles/<layer>/<int:z>/<int:x>/<int:y>.png")
    def radar_tile(layer, z, x, y):
        """Disk-cache proxy for IEM tiles. Strict validation: the layer must
        match the N0Q pattern AND be either a known frame or inside the loop
        window plus PRUNE_GRACE_S (junk or out-of-window is a 404, never a
        proxy pass-through), and z/x/y must be in range (the int converters
        already reject non-digits, so nothing unvalidated can reach the
        filesystem path). On-demand misses are fetched, cached, then served:
        timestamped frames are immutable."""
        if (not radar.IEM_LAYER_RE.match(layer) or not 0 <= z <= radar.MAX_TILE_ZOOM
                or not 0 <= x < (1 << z) or not 0 <= y < (1 << z)):
            abort(make_response(jsonify({"error": "unknown frame or tile"}), 404))
        # The grace window closes the prune race: the 90s prune tick can drop
        # a frame while clients hold frames.json up to ~2 min stale, and
        # their tile requests for it must not 404 into blank animation
        # frames. Within loop+grace the tile refetches from IEM on demand
        # (IEM keeps timestamped frames for at least a year); beyond it,
        # hard 404.
        if not store.get_radar_frame(layer) and not radar.frame_within_grace(cfg, layer):
            abort(make_response(jsonify({"error": "unknown frame"}), 404))
        # ensure_tile fetches misses on demand behind a per-tile
        # single-flight and a small semaphore with a short timeout, so a
        # burst of uncached-viewport misses cannot pin every request thread.
        path = radar.ensure_tile(cfg, layer, z, x, y)
        if path is None:
            abort(make_response(jsonify({"error": "upstream tile unavailable"}), 502))
        try:
            resp = send_file(path, mimetype="image/png", conditional=True)
        except FileNotFoundError:  # prune evicted the frame mid-request
            abort(make_response(jsonify({"error": "unknown frame"}), 404))
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.get("/v1/radar/frames/<layer>.png")
    def radar_frame_image(layer):
        """Whole-bbox nowCOAST fallback frames. Refetch-on-miss re-requests
        the frame's own timestamp (the WMS snaps it to its nearest real
        frame, so a disk loss heals to effectively the same image)."""
        if not radar.NOWCOAST_LAYER_RE.match(layer) or not store.get_radar_frame(layer):
            abort(make_response(jsonify({"error": "unknown frame"}), 404))
        path = radar.frame_image_path(cfg, layer)
        if not os.path.exists(path):
            data = radar.fetch_nowcoast_image(cfg, radar.parse_layer_time(layer))
            if data is None:
                abort(make_response(jsonify({"error": "upstream frame unavailable"}), 502))
            radar.write_atomic(path, data)
        try:
            resp = send_file(path, mimetype="image/png", conditional=True)
        except FileNotFoundError:  # prune evicted the frame mid-request
            abort(make_response(jsonify({"error": "unknown frame"}), 404))
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.get("/v1/radar/alerts.geojson")
    def radar_alerts_geojson():
        """Active alert polygons for the radar map: a GeoJSON
        FeatureCollection with the house envelope as foreign members.
        Zone-only alerts (no geometry) are skipped; there is nothing to
        draw and /v1/alerts already lists them."""
        now_utc = datetime.now(timezone.utc)
        features = []
        for a in store.list_alerts():
            if not a.get("geometry") or not alerting.alert_is_active(a, now_utc):
                continue
            try:
                geom = json.loads(a["geometry"])
            except (TypeError, ValueError):
                continue
            if not geom:
                continue
            features.append({"type": "Feature", "geometry": geom, "properties": {
                "event": a["event"],
                "severity": a["severity"],
                "headline": a["headline"],
                "affects_point": bool(a["affects_point"]),
            }})
        status = store.source_status("nws_alerts")
        resp = jsonify({
            "type": "FeatureCollection",
            "features": features,
            "source": "nws" if cfg.alerts_enabled else "disabled",
            "fetched_at": _iso_utc(status["last_success"])
            if status and status.get("last_success") else None,
            "stale": is_stale(cfg, status, "nws_alerts") if cfg.alerts_enabled else False,
        })
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp

    @app.get("/basemap.pmtiles")
    def basemap():
        """Self-hosted Protomaps extract. conditional=True makes werkzeug
        honor Range requests (the pmtiles JS client reads the archive via
        byte ranges and never fetches the whole file)."""
        if not os.path.isfile(cfg.basemap_path):
            abort(make_response(jsonify(
                {"error": "basemap not installed; see the README radar section"}), 404))
        resp = send_file(cfg.basemap_path, mimetype="application/octet-stream",
                         conditional=True)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    # ---- PWA shell (self-contained static frontend, same app) ----

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/manifest.json")
    def manifest():
        return app.send_static_file("manifest.json")

    @app.get("/sw.js")
    def service_worker():
        # Served at the root so the worker's scope covers the whole app.
        # no-cache so a new deploy is picked up on the next visit.
        resp = app.send_static_file("sw.js")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/healthz")
    def healthz():
        now = time.time()
        sources_health = {
            name: {
                "last_success": _iso_utc(s["last_success"]) if s.get("last_success") else None,
                "failures": s.get("failures", 0),
                "stale": is_stale(cfg, s, name, now),
            }
            for name, s in store.source_statuses().items()
        }
        return jsonify({
            "status": "ok",
            "sources": sources_health,
            "poller_leader_alive": leader_alive(store, now),
        })

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)

    if run_pollers:
        pollers.start(cfg, store)

    return app
