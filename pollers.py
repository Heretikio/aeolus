"""Background pollers as daemon threads, with single-leader election.

Under gunicorn with 2 workers, every worker calls start(); exactly one wins a
non-blocking exclusive flock on a lock file next to the DB and runs the
pollers. The others serve requests only. The lock dies with the process, so
leadership fails over on worker restart.

Every poller loop: fetch with timeout, exponential backoff on failure, never
crashes the thread, records last_success/failures per source (drives the
staleness flags and /metrics).
"""

import fcntl
import logging
import os
import threading
import time

import alerting
import radar
import sources

log = logging.getLogger("aeolus.pollers")

_leader_fd = None  # module-level so the fd (and the lock) outlives start()


def try_acquire_leader(lock_path: str) -> bool:
    global _leader_fd
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _leader_fd = fd
    return True


def start(cfg, store) -> bool:
    """Attempt leadership; on success spawn all poller threads. Returns
    whether this process is the leader."""
    lock_path = os.path.join(os.path.dirname(os.path.abspath(cfg.db_path)), "aeolus.leader.lock")
    if not try_acquire_leader(lock_path):
        log.info("poller leadership held by another worker; serving requests only")
        return False
    log.info("poller leader elected (pid %d)", os.getpid())
    store.meta_set("pollers_started_at", str(time.time()))
    location_id = store.default_location_id()

    jobs = [
        ("open_meteo_forecast", lambda: _forecast_job(cfg, store, location_id),
         lambda: cfg.forecast_interval),
        ("open_meteo_nowcast", lambda: _nowcast_job(cfg, store, location_id),
         lambda: cfg.nowcast_interval),
    ]
    if cfg.alerts_enabled:
        jobs.append(("nws_alerts", lambda: _alerts_job(cfg, store),
                     lambda: _alerts_interval(cfg, store)))
        jobs.append(("iem_watchdog", lambda: _watchdog_job(cfg, store),
                     lambda: cfg.watchdog_interval))
    else:
        log.info("ALERTS_ENABLED=0; NWS alert + IEM watchdog pollers disabled")
    if cfg.pirate_weather_key:
        jobs.append(("pirate_weather", lambda: _pirate_job(cfg, store, location_id),
                     lambda: cfg.pirate_interval))
    else:
        log.info("PIRATE_WEATHER_KEY empty; pirate_weather poller disabled")
    if cfg.radar_enabled:
        jobs.append(("iem_radar", lambda: radar.poll(cfg, store),
                     lambda: cfg.radar_interval))
    else:
        log.info("RADAR_ENABLED=0; radar poller disabled")

    # source_status mirrors exactly the enabled jobs, so /metrics exposes
    # stale=1 from leader start and drops sources that were disabled.
    store.sync_sources([name for name, _, _ in jobs])

    for name, work, interval_fn in jobs:
        # The radar tick runs the nowCOAST fallback inside poll(); letting
        # the generic backoff stretch to BACKOFF_CAP would throttle fallback
        # frames to that cadence exactly when IEM is down, so the radar job
        # never backs off beyond its own (politeness-floored) interval.
        cap = cfg.radar_interval if name == "iem_radar" else None
        threading.Thread(target=_loop, args=(cfg, store, name, work, interval_fn, cap),
                         name=f"poll-{name}", daemon=True).start()
    threading.Thread(target=_housekeeping_loop, args=(cfg, store),
                     name="poll-housekeeping", daemon=True).start()
    return True


def _loop(cfg, store, name, work, interval_fn, backoff_cap=None):
    """Structurally unable to exit: even bookkeeping failures (DB locked,
    disk full) are contained, or a poller thread would die silently while
    the leader keeps the flock and heartbeat."""
    cap = backoff_cap or cfg.backoff_cap
    backoff = 0
    while True:
        try:
            work()
            store.set_source_success(name, time.time())
            backoff = 0
        except Exception:
            log.exception("poller %s failed", name)
            backoff = min(backoff * 2 if backoff else cfg.backoff_base, cap)
            try:
                store.record_source_failure(name, time.time())
            except Exception:
                log.exception("poller %s failure bookkeeping failed", name)
        time.sleep(backoff or interval_fn())


def _housekeeping_loop(cfg, store):
    """Leader heartbeat (drives the /metrics leader gauge), morning digest
    flush, the primary-poller silence alarm, and the daily retention prune."""
    while True:
        try:
            store.meta_set("leader_heartbeat", str(time.time()))
            alerting.flush_digest(cfg, store)
            alerting.check_primary_silence(cfg, store)
            _daily_prune(store)
        except Exception:
            log.exception("housekeeping tick failed")
        time.sleep(30)


def _daily_prune(store, retention_days=30):
    """Once a day, drop alert rows not touched in retention_days; the only
    otherwise-unbounded state on the data volume."""
    now = time.time()
    last = store.meta_get("last_alert_prune")
    try:
        if last and now - float(last) < 86400:
            return
    except ValueError:
        pass
    store.prune_alerts(now - retention_days * 86400)
    store.meta_set("last_alert_prune", str(now))


# ---- jobs ----

def _forecast_job(cfg, store, location_id):
    raw = sources.fetch_open_meteo_forecast(cfg)
    now = time.time()
    store.save_forecast_run("open_meteo", "hourly", location_id, now,
                            sources.normalize_block(raw, "hourly", sources.HOURLY_FIELDS),
                            cfg.forecast_keep_runs)
    store.save_forecast_run("open_meteo", "daily", location_id, now,
                            sources.normalize_block(raw, "daily", sources.DAILY_FIELDS),
                            cfg.forecast_keep_runs)


def _nowcast_job(cfg, store, location_id):
    raw = sources.fetch_open_meteo_nowcast(cfg)
    store.save_forecast_run("open_meteo", "minutely15", location_id, time.time(),
                            sources.normalize_block(raw, "minutely_15", sources.MINUTELY_FIELDS),
                            cfg.forecast_keep_runs)


def _pirate_job(cfg, store, location_id):
    raw = sources.fetch_pirate(cfg)
    now = time.time()
    tzinfo = cfg.tzinfo
    store.save_forecast_run("pirate_weather", "hourly", location_id, now,
                            sources.normalize_pirate_hourly(raw, tzinfo), cfg.forecast_keep_runs)
    store.save_forecast_run("pirate_weather", "daily", location_id, now,
                            sources.normalize_pirate_daily(raw, tzinfo), cfg.forecast_keep_runs)
    minutely = sources.normalize_pirate_minutely(raw, tzinfo)
    if minutely:
        store.save_forecast_run("pirate_weather", "minutely15", location_id, now,
                                minutely, cfg.forecast_keep_runs)


def _alerts_job(cfg, store):
    features = sources.fetch_nws_alerts(cfg, store)
    if features is None:
        return  # 304: feed unchanged since the last poll, nothing to process
    alerting.process_alerts(cfg, store, features)


def _watchdog_job(cfg, store):
    features = sources.fetch_iem_sbw(cfg)
    alerting.check_watchdog(cfg, store, features)


def _alerts_interval(cfg, store) -> int:
    try:
        if alerting.any_hot_watch(store):
            return cfg.alerts_interval_hot
    except Exception:
        log.exception("hot-watch check failed; using normal cadence")
    return cfg.alerts_interval
