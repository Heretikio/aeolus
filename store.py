"""SQLite store. The cache is the product: consumers only ever read this.

Conventions:
- forecasts.ts / observations-from-models: local ISO strings ("2026-07-03T14:00",
  daily "2026-07-03") in the configured timezone, so lexicographic compare works.
- observations.ts and all bookkeeping timestamps (fetched_at, last_success): unix epoch.
- alerts.last_notified_state: JSON blob owned by alerting.py.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    kind TEXT NOT NULL,
    location_id INTEGER NOT NULL DEFAULT 1,
    ts TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_forecasts_lookup
    ON forecasts(source, kind, location_id, fetched_at, ts);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    station_id TEXT,
    ts REAL NOT NULL,
    temp REAL, rh REAL, pressure REAL, wind_speed REAL, wind_dir REAL,
    gust REAL, rain_rate REAL, rain_counter REAL, uv REAL, lux REAL
);
CREATE INDEX IF NOT EXISTS ix_observations_ts ON observations(source, ts);

CREATE TABLE IF NOT EXISTS alerts (
    event_key TEXT PRIMARY KEY,
    message_id TEXT,
    vtec TEXT,
    event TEXT,
    severity TEXT, certainty TEXT, urgency TEXT, response TEXT,
    headline TEXT, nws_headline TEXT, description TEXT, instruction TEXT,
    onset TEXT, ends TEXT, expires TEXT,
    area_desc TEXT, ugc TEXT, geometry TEXT, threat_params TEXT,
    affects_point INTEGER,
    last_notified_state TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS alert_messages (
    message_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY,
    last_success REAL,
    last_failure REAL,
    failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS digest_queue (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL,
    queued_at REAL NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS radar_frames (
    layer TEXT PRIMARY KEY,
    valid_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_radar_frames_valid ON radar_frames(valid_utc);
"""

ALERT_COLUMNS = [
    "event_key", "message_id", "vtec", "event", "severity", "certainty",
    "urgency", "response", "headline", "nws_headline", "description",
    "instruction", "onset", "ends", "expires", "area_desc", "ugc",
    "geometry", "threat_params", "affects_point", "updated_at",
]


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    @contextmanager
    def _db(self):
        # One short-lived connection per operation: simple and safe across
        # gunicorn threads and poller threads. WAL makes readers cheap.
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ensure_schema(self):
        with self._db() as c:
            c.execute("PRAGMA journal_mode = WAL")
            c.executescript(SCHEMA)

    # ---- locations ----

    def seed_default_location(self, name: str, lat: float, lon: float):
        # Atomic insert-if-empty: both gunicorn workers run this at startup,
        # so a check-then-insert would race into two default rows.
        with self._db() as c:
            c.execute(
                "INSERT INTO locations(name, lat, lon, is_default)"
                " SELECT ?,?,?,1 WHERE NOT EXISTS (SELECT 1 FROM locations)",
                (name, lat, lon),
            )

    def default_location_id(self) -> int:
        with self._db() as c:
            row = c.execute("SELECT id FROM locations WHERE is_default=1 LIMIT 1").fetchone()
            return row["id"] if row else 1

    def location_exists(self, location_id: int) -> bool:
        with self._db() as c:
            return c.execute(
                "SELECT 1 FROM locations WHERE id=?", (location_id,)
            ).fetchone() is not None

    def locations(self) -> list:
        with self._db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM locations ORDER BY id")]

    # ---- forecasts ----

    def save_forecast_run(self, source, kind, location_id, fetched_at, rows, keep_runs=5):
        """rows = [(ts, payload_dict), ...]; prunes to the last keep_runs runs."""
        with self._db() as c:
            c.executemany(
                "INSERT INTO forecasts(source, fetched_at, kind, location_id, ts, payload_json)"
                " VALUES(?,?,?,?,?,?)",
                [(source, fetched_at, kind, location_id, ts, json.dumps(p)) for ts, p in rows],
            )
            c.execute(
                "DELETE FROM forecasts WHERE source=? AND kind=? AND location_id=?"
                " AND fetched_at NOT IN ("
                "   SELECT DISTINCT fetched_at FROM forecasts"
                "   WHERE source=? AND kind=? AND location_id=?"
                "   ORDER BY fetched_at DESC LIMIT ?)",
                (source, kind, location_id, source, kind, location_id, keep_runs),
            )

    def latest_run(self, source, kind, location_id):
        with self._db() as c:
            row = c.execute(
                "SELECT MAX(fetched_at) AS m FROM forecasts"
                " WHERE source=? AND kind=? AND location_id=?",
                (source, kind, location_id),
            ).fetchone()
            if row["m"] is None:
                return None
            rows = c.execute(
                "SELECT ts, payload_json FROM forecasts"
                " WHERE source=? AND kind=? AND location_id=? AND fetched_at=?"
                " ORDER BY ts",
                (source, kind, location_id, row["m"]),
            ).fetchall()
            return {
                "fetched_at": row["m"],
                "rows": [(r["ts"], json.loads(r["payload_json"])) for r in rows],
            }

    def run_count(self, source, kind, location_id) -> int:
        with self._db() as c:
            row = c.execute(
                "SELECT COUNT(DISTINCT fetched_at) AS n FROM forecasts"
                " WHERE source=? AND kind=? AND location_id=?",
                (source, kind, location_id),
            ).fetchone()
            return row["n"]

    # ---- alerts ----

    def upsert_alert(self, alert: dict):
        """Insert or update by event_key. last_notified_state is never clobbered
        here (owned by set_alert_state); geometry survives updates that omit it."""
        cols = ",".join(ALERT_COLUMNS)
        ph = ",".join("?" * len(ALERT_COLUMNS))
        sets = ",".join(
            f"{col}=COALESCE(excluded.{col}, alerts.{col})" if col == "geometry"
            else f"{col}=excluded.{col}"
            for col in ALERT_COLUMNS if col != "event_key"
        )
        with self._db() as c:
            c.execute(
                f"INSERT INTO alerts({cols}) VALUES({ph})"
                f" ON CONFLICT(event_key) DO UPDATE SET {sets}",
                [alert.get(col) for col in ALERT_COLUMNS],
            )

    def get_alert(self, event_key: str):
        with self._db() as c:
            row = c.execute("SELECT * FROM alerts WHERE event_key=?", (event_key,)).fetchone()
        return self._alert_dict(row) if row else None

    def list_alerts(self) -> list:
        with self._db() as c:
            rows = c.execute("SELECT * FROM alerts ORDER BY updated_at DESC").fetchall()
        return [self._alert_dict(r) for r in rows]

    @staticmethod
    def _alert_dict(row) -> dict:
        d = dict(row)
        d["state"] = json.loads(d["last_notified_state"]) if d.get("last_notified_state") else {}
        return d

    def set_alert_state(self, event_key: str, state: dict):
        with self._db() as c:
            c.execute(
                "UPDATE alerts SET last_notified_state=? WHERE event_key=?",
                (json.dumps(state), event_key),
            )

    def record_alert_message(self, message_id: str, event_key: str):
        with self._db() as c:
            c.execute(
                "INSERT OR REPLACE INTO alert_messages(message_id, event_key) VALUES(?,?)",
                (message_id, event_key),
            )

    def event_key_for_message(self, message_id: str):
        if not message_id:
            return None
        with self._db() as c:
            row = c.execute(
                "SELECT event_key FROM alert_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            return row["event_key"] if row else None

    # ---- source status ----

    def set_source_success(self, source: str, epoch: float):
        with self._db() as c:
            c.execute(
                "INSERT INTO source_status(source, last_success, failures) VALUES(?,?,0)"
                " ON CONFLICT(source) DO UPDATE SET last_success=excluded.last_success",
                (source, epoch),
            )

    def record_source_failure(self, source: str, epoch: float):
        with self._db() as c:
            c.execute(
                "INSERT INTO source_status(source, last_failure, failures) VALUES(?,?,1)"
                " ON CONFLICT(source) DO UPDATE SET"
                " last_failure=excluded.last_failure, failures=failures+1",
                (source, epoch),
            )

    def source_status(self, source: str):
        with self._db() as c:
            row = c.execute("SELECT * FROM source_status WHERE source=?", (source,)).fetchone()
            return dict(row) if row else None

    def source_statuses(self) -> dict:
        with self._db() as c:
            return {r["source"]: dict(r) for r in c.execute("SELECT * FROM source_status")}

    def sync_sources(self, enabled: list):
        """Make source_status rows match the enabled pollers exactly: seed
        missing rows (so /metrics exposes stale=1 from leader start, not only
        after a first attempt) and drop rows for disabled sources (so e.g. a
        removed Pirate key does not leave a permanently-stale gauge)."""
        with self._db() as c:
            c.executemany(
                "INSERT OR IGNORE INTO source_status(source, failures) VALUES(?, 0)",
                [(name,) for name in enabled],
            )
            c.execute(
                f"DELETE FROM source_status WHERE source NOT IN"
                f" ({','.join('?' * len(enabled))})",
                enabled,
            )

    # ---- digest queue ----

    def queue_digest(self, event_key: str, summary: str, epoch: float):
        with self._db() as c:
            c.execute(
                "INSERT INTO digest_queue(event_key, queued_at, summary) VALUES(?,?,?)",
                (event_key, epoch, summary),
            )

    def peek_digest(self) -> list:
        """Read the queue without draining it; delete_digest confirms after
        the send succeeds so a failed post is retried, not lost."""
        with self._db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM digest_queue ORDER BY queued_at")]

    def delete_digest(self, ids: list):
        if not ids:
            return
        with self._db() as c:
            c.execute(
                f"DELETE FROM digest_queue WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )

    def prune_alerts(self, older_than: float):
        """Retention sweep: drop alert rows whose last update is older than
        the cutoff (active alerts are re-upserted every poll, so their
        updated_at stays fresh) plus any orphaned message mappings."""
        with self._db() as c:
            c.execute("DELETE FROM alerts WHERE updated_at < ?", (older_than,))
            c.execute(
                "DELETE FROM alert_messages WHERE event_key NOT IN"
                " (SELECT event_key FROM alerts)"
            )

    # ---- radar frames (v2) ----
    # valid_utc is a fixed-format ISO string ("2026-07-04T01:30:00Z") so
    # lexicographic ORDER BY / comparisons are chronological.

    def insert_radar_frame(self, layer: str, valid_utc: str, source: str, fetched_at: float):
        """Idempotent: re-ingesting a known frame is a no-op."""
        with self._db() as c:
            c.execute(
                "INSERT OR IGNORE INTO radar_frames(layer, valid_utc, source, fetched_at)"
                " VALUES(?,?,?,?)",
                (layer, valid_utc, source, fetched_at),
            )

    def get_radar_frame(self, layer: str):
        with self._db() as c:
            row = c.execute("SELECT * FROM radar_frames WHERE layer=?", (layer,)).fetchone()
            return dict(row) if row else None

    def list_radar_frames(self) -> list:
        """Oldest to newest: frames.json order is animation order."""
        with self._db() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM radar_frames ORDER BY valid_utc")]

    def newest_radar_frame(self, source: str | None = None):
        query = "SELECT * FROM radar_frames"
        args: tuple = ()
        if source:
            query += " WHERE source=?"
            args = (source,)
        query += " ORDER BY valid_utc DESC LIMIT 1"
        with self._db() as c:
            row = c.execute(query, args).fetchone()
            return dict(row) if row else None

    def oldest_radar_frame(self):
        with self._db() as c:
            row = c.execute(
                "SELECT * FROM radar_frames ORDER BY valid_utc LIMIT 1").fetchone()
            return dict(row) if row else None

    def delete_radar_frame(self, layer: str):
        with self._db() as c:
            c.execute("DELETE FROM radar_frames WHERE layer=?", (layer,))

    def prune_radar_frames(self, older_than: str) -> list:
        """Drop frames with valid_utc before the ISO cutoff; returns the
        dropped layer ids so the caller can remove their disk directories."""
        with self._db() as c:
            doomed = [r["layer"] for r in c.execute(
                "SELECT layer FROM radar_frames WHERE valid_utc < ?", (older_than,))]
            c.execute("DELETE FROM radar_frames WHERE valid_utc < ?", (older_than,))
            return doomed

    # ---- meta ----

    def meta_get(self, key: str):
        with self._db() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def meta_set(self, key: str, value: str):
        with self._db() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))

    # ---- observations (station ingest lands here in v3) ----

    def insert_observation(self, obs: dict):
        cols = ["source", "station_id", "ts", "temp", "rh", "pressure", "wind_speed",
                "wind_dir", "gust", "rain_rate", "rain_counter", "uv", "lux"]
        with self._db() as c:
            c.execute(
                f"INSERT INTO observations({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                [obs.get(col) for col in cols],
            )

    def latest_station_observation(self, max_age_s: float, now: float | None = None):
        now = now if now is not None else time.time()
        with self._db() as c:
            row = c.execute(
                "SELECT * FROM observations WHERE source='station' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row or now - float(row["ts"]) > max_age_s:
            return None
        return dict(row)
