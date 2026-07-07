"""IEM NEXRAD radar frames: catalog poller, warm tile cache, backfill,
age/size pruning and the nowCOAST WMS fallback.

Verified upstream behavior encoded here (live recon 2026-07-04):
- The IEM catalog (tms.json) advertises ONLY the current frame; loop history
  is self-accumulated, and gaps are backfilled by constructing layer names on
  the 5-minute grid. Timestamped layers serve imagery for at least a year, so
  a full loop backfill always works after a restart.
- Tile URLs look like TMS ("/1.0.0/") but the y index is XYZ/slippy
  (top-down). Blank tiles return 200 PNG; nonexistent (future) frames return
  503 with a non-PNG body. PNG magic, not status, is the success signal.
- /c/tile.py serves immutable timestamped frames with 14-day cache headers;
  used for all tile fetches. Warm fetches are paced to ~5 req/s (PACE_S).
- The m05m..m55m lag layers are broken upstream (byte-identical images) and
  are deliberately not used; timestamped layers cover every need.
- Fallback: nowCOAST WMS conus_base_reflectivity_mosaic (MRMS, ~4-min
  cadence, ~7h time dimension, nearestValue=1 snaps requested times).
  Engaged when IEM has produced no frame for FALLBACK_AFTER_S, or when
  frames are advertised but tile fetches have been failing that long
  (catalog up, tile server down), or immediately on a cold start with IEM
  down; frames are whole-bbox images recorded with source=nowcoast.
"""

import logging
import math
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("aeolus.radar")

CATALOG_URL = "https://mesonet.agron.iastate.edu/json/tms.json"
TILE_URL = "https://mesonet.agron.iastate.edu/c/tile.py/1.0.0/{layer}/{z}/{x}/{y}.png"
NOWCOAST_URL = "https://nowcoast.noaa.gov/geoserver/observations/weather_radar/wms"
NOWCOAST_WMS_LAYER = "conus_base_reflectivity_mosaic"

PRODUCT_PREFIX = "ridge::USCOMP-N0Q-"
# \Z, not $: $ also matches before a trailing newline, and these patterns are
# the traversal gate for everything that reaches the tile cache filesystem.
IEM_LAYER_RE = re.compile(r"^ridge::USCOMP-N0Q-(\d{12})\Z")
NOWCOAST_LAYER_RE = re.compile(r"^nowcoast-(\d{12})\Z")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
FRAME_STEP = timedelta(minutes=5)
PRUNE_GRACE_S = 1800      # tile endpoint serves this far past the loop window
FALLBACK_AFTER_S = 1200   # IEM frame-silent this long -> nowCOAST fallback
PACE_S = 0.2              # warm-fetch budget: one tile per PACE_S (~5 req/s)
WARM_SPAN = 1             # 3x3 tile block around your location per warm zoom
MAX_TILE_ZOOM = 12
WARM_BREAKER = 5          # consecutive warm-fetch failures abort the tick
WARM_TILE_TIMEOUT = 5     # warm fetches fail fast; frames self-heal on demand
BACKFILL_CONCURRENCY = 4  # connections during a multi-frame backfill only:
                          # sequential fetch latency makes ~1.5 tiles/s
                          # effective (a 40-minute cold start); recon verified
                          # IEM tolerates ~5 req/s over 2-4 connections for a
                          # one-time backfill. Steady state stays sequential.
PROXY_TILE_TIMEOUT = 4    # on-demand fetches run in request threads
PROXY_CONCURRENCY = 2     # concurrent upstream tile fetches per process


class TilesDown(RuntimeError):
    """Circuit breaker: the IEM tile server is failing consecutively."""


# ---- naming / time ----

def floor_frame(dt: datetime) -> datetime:
    """Snap down to the IEM 5-minute frame grid."""
    return dt.replace(minute=dt.minute - dt.minute % 5, second=0, microsecond=0)


def layer_for(dt: datetime) -> str:
    return PRODUCT_PREFIX + dt.strftime("%Y%m%d%H%M")


def parse_layer_time(layer: str):
    """Frame time for an IEM tile layer or a nowcoast frame id; None for
    anything else (junk, lag layers, other products, traversal attempts)."""
    m = IEM_LAYER_RE.match(layer) or NOWCOAST_LAYER_RE.match(layer)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_epoch(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def frame_within_grace(cfg, layer: str, now: datetime | None = None) -> bool:
    """True when a layer's timestamp falls inside the loop window extended
    backward by PRUNE_GRACE_S. The 90s prune tick races clients holding
    frames.json up to ~2 min stale (60s HTTP cache + 60s re-poll): a tile
    request can arrive for a frame pruned moments ago. IEM serves
    timestamped frames for at least a year (recon 2026-07-04), so such a
    frame is refetched on demand instead of 404ing into a blank animation
    frame. Future stamps and unparsable layers stay excluded."""
    dt = parse_layer_time(layer)
    if dt is None:
        return False
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.radar_loop_hours, seconds=PRUNE_GRACE_S)
    return cutoff <= dt <= now


# ---- tile math (XYZ/slippy, top-down y; verified against live IEM tiles) ----

def tile_at(lat: float, lon: float, z: int) -> tuple:
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def warm_tiles(cfg) -> list:
    """The warm set: a 3x3 tile block centered on your location at every warm
    zoom (54 tiles at the default 6-11). The wider storm-region bbox is
    served through the tile endpoint's fetch-on-demand path instead."""
    tiles = []
    for z in cfg.radar_warm_zoom_list:
        cx, cy = tile_at(cfg.lat, cfg.lon, z)
        n = 1 << z
        for x in range(max(0, cx - WARM_SPAN), min(n - 1, cx + WARM_SPAN) + 1):
            for y in range(max(0, cy - WARM_SPAN), min(n - 1, cy + WARM_SPAN) + 1):
                tiles.append((z, x, y))
    return tiles


# ---- disk cache ----

def frame_dir(cfg, layer: str) -> str:
    return os.path.join(cfg.radar_cache_dir, layer)


def tile_path(cfg, layer: str, z: int, x: int, y: int) -> str:
    return os.path.join(frame_dir(cfg, layer), str(z), str(x), f"{y}.png")


def frame_image_path(cfg, layer: str) -> str:
    return os.path.join(frame_dir(cfg, layer), "frame.png")


def write_atomic(path: str, data: bytes):
    """Atomic write, safe across gunicorn threads: the temp name must be
    unique per WRITER (pid alone collides across the worker's threads), and
    a lost race with the pruner (rmtree between write and rename) is treated
    as success; radar payloads are immutable so the loser's data matched."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.replace(tmp, path)
    except FileNotFoundError:
        pass


def cache_size_bytes(cfg) -> int:
    total = 0
    for root, _dirs, files in os.walk(cfg.radar_cache_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


# ---- fetchers ----

def fetch_catalog_time(cfg) -> datetime:
    """Current N0Q frame time from tms.json. The timestamped layer name has
    been observed under both keys: "id" at recon time, "layername" with a
    slug id ("ridge_uscomp_n0q") live on 2026-07-04; accept either."""
    try:
        resp = requests.get(CATALOG_URL, headers={"User-Agent": cfg.user_agent},
                            timeout=cfg.http_timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"GET {CATALOG_URL} failed: {type(e).__name__}") from None
    if resp.status_code != 200:
        raise RuntimeError(f"GET {CATALOG_URL} failed: HTTP {resp.status_code}")
    for svc in resp.json().get("services") or []:
        dt = (parse_layer_time(svc.get("layername") or "")
              or parse_layer_time(svc.get("id") or ""))
        if dt:
            return dt
    raise RuntimeError("IEM catalog lists no USCOMP-N0Q service")


def fetch_tile(cfg, layer: str, z: int, x: int, y: int, timeout: int | None = None):
    """One tile from IEM's immutable /c/ cache. Returns PNG bytes or None.
    Blank tiles are 200 PNG and are kept (blank = no rain, a valid answer);
    missing frames answer 503 non-PNG, hence the magic check."""
    url = TILE_URL.format(layer=layer, z=z, x=x, y=y)
    try:
        resp = requests.get(url, headers={"User-Agent": cfg.user_agent},
                            timeout=timeout or cfg.http_timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.content.startswith(PNG_MAGIC):
        return None
    return resp.content


def fetch_nowcoast_image(cfg, when: datetime | None = None):
    """Whole-frame WMS render of the storm bbox (CRS:84 = lon,lat order).
    nearestValue=1 upstream: the requested time snaps to the nearest real
    ~4-minute frame. Returns PNG bytes or None."""
    w, s, e, n = cfg.radar_bbox_tuple
    width = 1024
    height = max(1, round(width * (n - s) / (e - w)))
    params = {
        "service": "WMS", "version": "1.3.0", "request": "GetMap",
        "layers": NOWCOAST_WMS_LAYER, "styles": "", "crs": "CRS:84",
        "bbox": f"{w},{s},{e},{n}", "width": width, "height": height,
        "format": "image/png", "transparent": "true",
    }
    if when is not None:
        params["time"] = iso_utc(when)
    try:
        resp = requests.get(NOWCOAST_URL, params=params,
                            headers={"User-Agent": cfg.user_agent},
                            timeout=cfg.http_timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.content.startswith(PNG_MAGIC):
        return None
    return resp.content


# ---- warm fetch / backfill ----

def warm_frame(cfg, layer: str, concurrency: int = 1) -> tuple:
    """Fetch the warm set for one frame into the disk cache, politely paced.
    Tiles already on disk are skipped (timestamped frames are immutable, a
    tile is never refetched); scattered per-tile failures are tolerated
    because the tile endpoint fetches on demand, but WARM_BREAKER
    consecutive failures raise TilesDown so a dead tile server cannot wedge
    a backfill tick for hours at one timeout per tile.

    Steady state calls this sequentially (concurrency=1). A multi-frame
    backfill passes BACKFILL_CONCURRENCY: tiles are fetched in small
    concurrent batches while the overall rate stays capped at one tile per
    PACE_S (the batch sleeps off the remainder of its budget), so a cold
    start actually reaches ~5 tiles/s instead of being serialized on fetch
    latency. Returns (fetched, failed)."""
    todo = []
    for z, x, y in warm_tiles(cfg):
        path = tile_path(cfg, layer, z, x, y)
        if not os.path.exists(path):
            todo.append((z, x, y, path))
    fetched = failed = streak = 0
    batch = max(1, concurrency)
    pool = ThreadPoolExecutor(max_workers=batch) if batch > 1 else None
    try:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            started = time.monotonic()
            if pool:
                results = list(pool.map(
                    lambda t: fetch_tile(cfg, layer, t[0], t[1], t[2],
                                         WARM_TILE_TIMEOUT), chunk))
            else:
                z, x, y, _path = chunk[0]
                results = [fetch_tile(cfg, layer, z, x, y, WARM_TILE_TIMEOUT)]
            for (_z, _x, _y, path), data in zip(chunk, results):
                if data is None:
                    failed += 1
                    streak += 1
                else:
                    write_atomic(path, data)
                    fetched += 1
                    streak = 0
            if streak >= WARM_BREAKER:
                raise TilesDown(f"{streak} consecutive tile fetches failed")
            if PACE_S:
                time.sleep(max(0.0, PACE_S * len(chunk)
                               - (time.monotonic() - started)))
    finally:
        if pool:
            pool.shutdown(wait=False)
    return fetched, failed


# On-demand misses run in gunicorn request threads: a small semaphore plus
# per-tile single-flight, so one client scrubbing an uncached viewport can
# neither pin every request thread on upstream waits nor send duplicate
# fetches for the same tile to IEM.
_proxy_sem = threading.BoundedSemaphore(PROXY_CONCURRENCY)
_inflight: dict = {}
_inflight_lock = threading.Lock()


def ensure_tile(cfg, layer: str, z: int, x: int, y: int):
    """Tile path for a known frame, fetched on demand when missing. Returns
    None when the tile cannot be produced right now (upstream down, or every
    proxy slot busy for PROXY_TILE_TIMEOUT); the caller answers 502 and the
    map retries naturally."""
    path = tile_path(cfg, layer, z, x, y)
    if os.path.exists(path):
        return path
    with _inflight_lock:
        lock = _inflight.setdefault(path, threading.Lock())
    with lock:
        try:
            if os.path.exists(path):
                return path  # a concurrent miss just fetched it
            if not _proxy_sem.acquire(timeout=PROXY_TILE_TIMEOUT):
                return None
            try:
                data = fetch_tile(cfg, layer, z, x, y, PROXY_TILE_TIMEOUT)
                if data is None:
                    return None
                write_atomic(path, data)
                return path
            finally:
                _proxy_sem.release()
        finally:
            with _inflight_lock:
                _inflight.pop(path, None)


def missing_frames(store, current: datetime, now: datetime, loop_hours: int) -> list:
    """5-minute grid times inside the loop window, up to the catalog's
    current frame (never the future), that the frames table lacks. An empty
    or stale table yields the full backfill; steady state yields one or
    none. The window starts one step after the prune cutoff so a backfilled
    frame is not pruned in the same tick, and never reaches back to the
    size-eviction watermark, or size-evicted frames would be refetched and
    re-evicted every tick, forever."""
    have = {row["valid_utc"] for row in store.list_radar_frames()
            if row["source"] == "iem"}
    floor = store.meta_get("radar_backfill_floor") or ""
    t = floor_frame(now - timedelta(hours=loop_hours)) + FRAME_STEP
    missing = []
    while t <= current:
        if iso_utc(t) not in have and iso_utc(t) > floor:
            missing.append(t)
        t += FRAME_STEP
    return missing


# ---- the poller tick ----

def poll(cfg, store, now: datetime | None = None):
    """One radar tick: ingest new/missing IEM frames (steady state: one new
    frame every ~5 min; cold start: a clearly-logged backfill of the whole
    loop), evaluate the nowCOAST fallback, prune by age and size. An IEM
    failure still runs the fallback and the prune, then re-raises so the
    poller loop records the failure for iem_radar and backs off."""
    now = now or datetime.now(timezone.utc)
    iem_error = None
    try:
        _poll_iem(cfg, store, now)
    except Exception as e:
        iem_error = e
    try:
        _maybe_fallback(cfg, store, now)
    except Exception:
        log.exception("nowCOAST fallback attempt failed")
    prune(cfg, store, now)
    if iem_error:
        raise iem_error


def _poll_iem(cfg, store, now: datetime):
    current = fetch_catalog_time(cfg)
    missing = missing_frames(store, current, now, cfg.radar_loop_hours)
    if not missing:
        return
    if len(missing) > 1:
        log.info("radar backfill: %d frames missing %s..%s; warm-fetching from IEM",
                 len(missing), iso_utc(missing[0]), iso_utc(missing[-1]))
    # Rows before tiles: a partially warmed frame is advertised anyway and
    # self-heals through the tile endpoint's fetch-on-demand path.
    for dt in missing:
        store.insert_radar_frame(layer_for(dt), iso_utc(dt), "iem", time.time())
    _supersede_nowcoast(cfg, store)
    fetched = failed = 0
    # A real backfill (more than the steady-state single new frame) may use
    # a few concurrent connections; the per-tile pacing budget still holds.
    concurrency = BACKFILL_CONCURRENCY if len(missing) > 1 else 1
    try:
        # Newest first: the current frame reaches the map seconds into a
        # cold start, not after the whole backfill.
        for dt in reversed(missing):
            ok, bad = warm_frame(cfg, layer_for(dt), concurrency)
            fetched += ok
            failed += bad
    except TilesDown as e:
        if fetched:
            store.meta_set("radar_last_tile_ok", str(now.timestamp()))
        store.meta_set("radar_tiles_failing", "1")
        raise RuntimeError(f"radar warm fetch aborted: {e}") from None
    if fetched:
        # Imagery evidence drives the fallback trigger; frame rows alone
        # say nothing when the tile server is the part that is down.
        store.meta_set("radar_last_tile_ok", str(now.timestamp()))
        store.meta_set("radar_tiles_failing", "")
    elif failed:
        store.meta_set("radar_tiles_failing", "1")
        raise RuntimeError(f"radar warm fetch: all {failed} tile fetches failed")
    log.info("radar: %d frame(s) ingested, %d tiles fetched, %d failed",
             len(missing), fetched, failed)


def _supersede_nowcoast(cfg, store):
    """IEM frames supersede fallback frames: drop nowcoast rows whose
    5-minute slot an IEM row now covers, or frames.json would list the same
    valid time twice and the loop would stutter through both."""
    rows = store.list_radar_frames()
    covered = {r["valid_utc"] for r in rows if r["source"] == "iem"}
    for r in rows:
        if r["source"] == "nowcoast" and r["valid_utc"] in covered:
            store.delete_radar_frame(r["layer"])
            shutil.rmtree(frame_dir(cfg, r["layer"]), ignore_errors=True)


def _maybe_fallback(cfg, store, now: datetime):
    """Source whole-bbox nowCOAST frames while IEM is silent: no frame for
    FALLBACK_AFTER_S, OR frames advertised but the tile server failing with
    no tile success for as long (rows are not imagery; a catalog-up,
    tiles-down outage must engage the fallback too). Engages immediately on
    a cold start with IEM down: something on the map beats an empty loop.
    Drops back the moment IEM flows again; transitions are logged once,
    frames carry source=nowcoast."""
    newest = store.newest_radar_frame("iem")
    age = (now.timestamp() - iso_to_epoch(newest["valid_utc"])) if newest else None
    tiles_dead = False
    if store.meta_get("radar_tiles_failing") == "1":
        try:
            last_ok = float(store.meta_get("radar_last_tile_ok") or 0)
        except ValueError:
            last_ok = 0
        tiles_dead = now.timestamp() - last_ok > FALLBACK_AFTER_S
    was_active = store.meta_get("radar_fallback_active") == "1"
    if age is not None and age <= FALLBACK_AFTER_S and not tiles_dead:
        if was_active:
            store.meta_set("radar_fallback_active", "")
            log.info("radar fallback ended: IEM frames flowing again")
        return
    slot = floor_frame(now)
    layer = f"nowcoast-{slot.strftime('%Y%m%d%H%M')}"
    if store.get_radar_frame(layer):
        return  # this 5-minute slot is already covered
    data = fetch_nowcoast_image(cfg, slot)
    if data is None:
        raise RuntimeError("nowCOAST GetMap failed while IEM is frame-silent")
    write_atomic(frame_image_path(cfg, layer), data)
    store.insert_radar_frame(layer, iso_utc(slot), "nowcoast", time.time())
    if not was_active:
        store.meta_set("radar_fallback_active", "1")
        why = ("IEM tiles failing" if tiles_dead
               else f"no IEM frame for {int(age // 60)} min" if age is not None
               else "no IEM frame ever")
        log.warning("radar fallback engaged: %s;"
                    " sourcing nowCOAST WMS frames for the bbox", why)


# ---- pruning ----

def prune(cfg, store, now: datetime):
    """Age prune (frames older than the loop window: table rows and their
    disk dirs, plus a sweep for orphaned frame dirs), then the size cap:
    one walk computes per-frame sizes (sweeping stale write temp files on
    the way), then whole frames are evicted oldest-first until under
    RADAR_CACHE_MAX_MB. Size evictions raise the backfill watermark so
    missing_frames does not refetch the same frames next tick, and warn
    once per tick, not per frame. The measured size is stashed in meta for
    the /metrics gauge (scrapes must not walk the tree)."""
    cutoff = iso_utc(now - timedelta(hours=cfg.radar_loop_hours))
    for layer in store.prune_radar_frames(cutoff):
        shutil.rmtree(frame_dir(cfg, layer), ignore_errors=True)
    if os.path.isdir(cfg.radar_cache_dir):
        for name in os.listdir(cfg.radar_cache_dir):
            dt = parse_layer_time(name)
            if dt and iso_utc(dt) < cutoff:
                shutil.rmtree(os.path.join(cfg.radar_cache_dir, name),
                              ignore_errors=True)
    total, sizes = _frame_sizes(cfg)
    max_bytes = cfg.radar_cache_max_mb * 1024 * 1024
    evicted = 0
    while total > max_bytes:
        oldest = store.oldest_radar_frame()
        if not oldest:
            break  # cap exceeded by non-frame junk; nothing safe to evict
        store.delete_radar_frame(oldest["layer"])
        shutil.rmtree(frame_dir(cfg, oldest["layer"]), ignore_errors=True)
        total -= sizes.pop(oldest["layer"], 0)
        if oldest["valid_utc"] > (store.meta_get("radar_backfill_floor") or ""):
            store.meta_set("radar_backfill_floor", oldest["valid_utc"])
        evicted += 1
    if evicted:
        log.warning("radar cache over %d MB; evicted %d oldest frame(s)",
                    cfg.radar_cache_max_mb, evicted)
    store.meta_set("radar_cache_bytes", str(total))


def _frame_sizes(cfg) -> tuple:
    """One walk of the cache dir: (total bytes, {top-level dir name: bytes}).
    Stale write_atomic leftovers (a crash between write and rename strands
    *.tmp.* files) are deleted instead of counted."""
    total, sizes = 0, {}
    stale_before = time.time() - 3600
    for root, _dirs, files in os.walk(cfg.radar_cache_dir):
        top = os.path.relpath(root, cfg.radar_cache_dir).split(os.sep, 1)[0]
        for name in files:
            path = os.path.join(root, name)
            try:
                if ".tmp." in name and os.path.getmtime(path) < stale_before:
                    os.unlink(path)
                    continue
                size = os.path.getsize(path)
            except OSError:
                continue
            total += size
            sizes[top] = sizes.get(top, 0) + size
    return total, sizes
