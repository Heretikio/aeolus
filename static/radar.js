/* Aeolus radar view: MapLibre + the vendored Protomaps basemap + the 6h IEM
   frame loop from /v1/radar/frames.json, with NWS alert polygons on top.

   Structured like charts.js: pure helpers (frame ids, tile math, playback
   stepping, severity paint) are exported for the node verification harness;
   all DOM and map wiring lives in create(env), which takes injectable
   dependencies so the harness can drive it with stubs. The browser uses the
   lazy singleton behind AeolusRadar.enter()/leave(); nothing map-related
   happens until the radar tab is first opened. */

(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.AeolusRadar = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* Map center + marker location. The server seeds the default location from
     LAT / LON; the frontend pushes it in via AeolusRadar.setLocation() once
     /v1/locations resolves. These values are only the pre-resolution fallback
     (and what the node verification harness overrides via env.location). */
  var LOCATION = { lat: 35.22, lon: -97.44, name: "Home" };
  var RADAR_OPACITY = 0.7;
  var STEP_MS = 140;        // per frame while looping (72 frames ~ 10s)
  var DWELL_MS = 1400;      // hold on the newest frame before wrapping
  var REFRESH_MS = 60000;   // frames.json + alerts.geojson re-poll
  var MAX_WARM_TILES = 32;  // per frame, SW warming cap
  var WARM_BATCH = 4;       // concurrent warm fetches (the proxy has few upstream slots)
  var MAX_TILE_ZOOM = 12;   // mirrors radar.MAX_TILE_ZOOM server-side
  var PRUNE_SAFETY_MS = 5 * 60 * 1000;   // hold back frames this close to the prune boundary
  var ANCHOR_SLACK_MS = 10 * 60 * 1000;  // prune anchor never runs further past the newest frame
  var GATE_POLL_MS = 100;    // re-check cadence while a frame's tiles load
  var GATE_TIMEOUT_MS = 1000; // one slow tile may delay a step at most this long
  var NOTICE_GRACE_MS = 8000; // basemap errors must persist this long to banner

  // ---- pure helpers (node-testable) ----

  function frameLayerId(layer) { return "radar-frame-" + layer; }
  function frameSourceId(layer) { return "radar-src-" + layer; }

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function fmtFrameTime(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return "?";
    return d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  }

  /* Loop stepping over the playable window [windowStart .. count-1]; the
     window deepens as warming progresses. -1 when there is nothing to show. */
  function nextIndex(cur, windowStart, count) {
    if (count <= 0) return -1;
    var ws = Math.max(0, Math.min(windowStart, count - 1));
    var next = cur + 1;
    if (cur < 0 || next >= count || next < ws) next = ws;
    return next;
  }

  // XYZ/slippy tile under a point (same math as radar.tile_at server-side)
  function tileAt(lat, lon, z) {
    var n = 1 << z;
    var x = Math.floor((lon + 180) / 360 * n);
    var rad = lat * Math.PI / 180;
    var y = Math.floor((1 - Math.asinh(Math.tan(rad)) / Math.PI) / 2 * n);
    return [Math.min(Math.max(x, 0), n - 1), Math.min(Math.max(y, 0), n - 1)];
  }

  function viewportTiles(w, s, e, n, z) {
    var tl = tileAt(n, w, z);
    var br = tileAt(s, e, z);
    var out = [];
    for (var x = tl[0]; x <= br[0]; x++) {
      for (var y = tl[1]; y <= br[1]; y++) out.push([z, x, y]);
    }
    return out;
  }

  function tileUrlFor(template, layer, t) {
    return template.replace("{layer}", layer).replace("{z}", t[0])
      .replace("{x}", t[1]).replace("{y}", t[2]);
  }

  /* Frames safe to play: the server prunes frames older than the loop
     window on its own 90s tick while clients hold frames.json up to ~2 min
     stale, so the oldest frames within PRUNE_SAFETY_MS of that boundary are
     dropped before the loop can play a frame about to disappear. The
     boundary is anchored at wall clock but never more than ANCHOR_SLACK_MS
     past the newest frame: an offline client replaying a frozen SW-cached
     loop must not filter its whole list away (nothing prunes underneath it
     offline). */
  function playableFrames(list, loopHours, nowMs) {
    if (!list || !list.length || !loopHours) return list || [];
    var newest = Date.parse(list[list.length - 1].valid);
    var anchor = isNaN(newest) ? nowMs : Math.min(nowMs, newest + ANCHOR_SLACK_MS);
    var cutoff = anchor - loopHours * 3600000 + PRUNE_SAFETY_MS;
    return list.filter(function (f) {
      var t = Date.parse(f.valid);
      return isNaN(t) || t >= cutoff;
    });
  }

  /* Attribute a map "error" event: the basemap notice may only fire for
     failures of the pmtiles style/source itself. Source-tagged errors are
     the basemap's only when they name the protomaps source AND it has never
     finished loading (a transient tile error on an already-rendering
     basemap is not "failed to load"; radar sources never qualify at all).
     Untagged errors (style JSON, sprites, glyphs) are the basemap's only
     before the initial style load resolves; later untagged errors are
     something else going wrong and must not blame the basemap. */
  function basemapNoticeFor(ev, styleLoaded, basemapOk) {
    var sid = ev && ev.sourceId;
    if (sid) return sid === "protomaps" && !basemapOk;
    return !styleLoaded;
  }

  /* Advance gate: step to the next frame only when its tiles report
     loaded, or after timeoutMs so one slow tile cannot stall the loop. */
  function gateAdvance(ready, waitedMs, timeoutMs) {
    return !!ready || waitedMs >= timeoutMs;
  }

  /* One short line naming what actually failed, for the banner detail and
     the console log: "sourceId: message", truncated so a phone screenshot
     of the banner still identifies the culprit. */
  function errorDetail(ev) {
    var err = ev && ev.error;
    var msg = (err && (err.message || String(err))) || "unknown error";
    var s = ev && ev.sourceId ? ev.sourceId + ": " + msg : msg;
    return s.length > 80 ? s.slice(0, 77) + "..." : s;
  }

  /* Severity -> color expression; tokens come from the app.css custom
     properties so the map reuses the exact alert colors, per theme. */
  function sevMatch(tokens) {
    return ["match", ["get", "severity"],
      "Extreme", tokens.extreme,
      "Severe", tokens.severe,
      "Moderate", tokens.moderate,
      tokens.minor];
  }

  var EMPTY_FC = { type: "FeatureCollection", features: [] };

  var PLAY_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
    '<path d="M8.2 5.4v13.2L19 12z"/></svg>';
  var PAUSE_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
    '<path d="M7.6 5.5h3.2v13H7.6zM13.2 5.5h3.2v13h-3.2z"/></svg>';

  // ---- controller ----

  function create(env) {
    env = env || {};
    var win = env.window || (typeof window !== "undefined" ? window : null);
    var doc = env.document || (win && win.document);
    var ml = env.maplibregl || (win && win.maplibregl);
    var pm = env.pmtiles || (win && win.pmtiles);
    var fetchFn = env.fetch || (win && win.fetch && win.fetch.bind(win));
    var nowFn = env.now || function () { return Date.now(); };
    var loc = env.location || LOCATION;   // map center, marker, warm-bbox fallback

    var map = null, mapReady = false;
    var frames = [], tileTemplate = "", bbox = null, stale = false;
    var cur = -1, windowStart = 0;
    var playing = false, userPaused = false;   // pause persists per session
    var active = false;
    var playTimer = null, refreshTimer = null;
    var gateWaited = 0;                        // ms spent gated on the next frame
    var added = {};                            // layer name -> layers exist
    var alertData = EMPTY_FC;
    var warmGen = 0;
    var popup = null;
    var log = [];                              // harness breadcrumb trail

    function $(id) { return doc.getElementById(id); }
    function note(kind, detail) { if (log.length < 500) log.push([kind, detail]); }

    function fetchJSON(path) {
      return fetchFn(path, { headers: { Accept: "application/json" } })
        .then(function (r) {
          return r.json().catch(function () { return null; }).then(function (body) {
            return { ok: r.ok, status: r.status, body: body };
          });
        })
        .catch(function () { return { ok: false, status: 0, body: null }; });
    }

    function abs(u) {
      try { return new win.URL(u, win.location.href).href; }
      catch (e) { return u; }
    }

    /* Absolutize a root-relative tile template WITHOUT the URL parser: it
       percent-encodes the {z}/{x}/{y} placeholders, which MapLibre then
       fails to substitute. */
    function absTemplate(t) {
      try { return new win.URL(win.location.href).origin + t; }
      catch (e) { return t; }
    }

    function token(name) {
      var v = win.getComputedStyle(doc.documentElement).getPropertyValue(name);
      return (v || "").trim() || "#888888";
    }

    function sevTokens() {
      return {
        extreme: token("--sev-extreme-edge"),
        severe: token("--sev-severe-edge"),
        moderate: token("--sev-moderate-edge"),
        minor: token("--sev-minor-edge")
      };
    }

    function styleUrl() {
      var dark = win.matchMedia &&
        win.matchMedia("(prefers-color-scheme: dark)").matches;
      return "/static/vendor/style-" + (dark ? "dark" : "light") + ".json";
    }

    /* Banner over the map for basemap problems (missing archive, corrupt
       pmtiles): radar and alerts keep working on the gray canvas, but the
       user gets told why the map is blank instead of guessing. The detail
       line names the underlying failure so a screenshot is diagnostic.
       Dismissable: the basemap reaching loaded takes the banner down. */
    var mapNoticeShown = false, noticeEl = null;
    function showMapNotice(msg, detail) {
      if (mapNoticeShown) return;
      mapNoticeShown = true;
      var container = $("radarMap");
      if (container && container.appendChild) {
        noticeEl = doc.createElement("div");
        noticeEl.className = "map-notice";
        noticeEl.textContent = msg;
        if (detail) {
          var d = doc.createElement("div");
          d.className = "map-notice-detail";
          d.textContent = detail;
          noticeEl.appendChild(d);
        }
        container.appendChild(noticeEl);
      }
      note("map-notice", detail ? msg + " :: " + detail : msg);
    }

    function dismissMapNotice() {
      if (!mapNoticeShown) return;
      mapNoticeShown = false;
      if (noticeEl && noticeEl.parentNode && noticeEl.parentNode.removeChild) {
        noticeEl.parentNode.removeChild(noticeEl);
      }
      noticeEl = null;
      note("map-notice-dismissed", "");
    }

    // ---- map ----

    function initMap() {
      var container = $("radarMap");
      if (!container) return;
      if (!ml) {
        container.innerHTML = '<p class="empty-state"><strong>Map failed to ' +
          "load.</strong> The vendored MapLibre script is missing.</p>";
        return;
      }
      if (pm && ml.addProtocol) {
        var protocol = new pm.Protocol();
        ml.addProtocol("pmtiles", protocol.tile);
      }
      map = new ml.Map({
        container: container,
        style: styleUrl(),
        center: [loc.lon, loc.lat],
        zoom: 7, minZoom: 4, maxZoom: MAX_TILE_ZOOM,
        attributionControl: false  // the view renders its own line
      });

      // Location marker: a DOM marker sits above every canvas layer, so it is
      // always on top of radar and alert polygons by construction.
      var dot = doc.createElement("div");
      dot.className = "loc-marker";
      dot.setAttribute("aria-label", loc.name || "Location");
      new ml.Marker({ element: dot }).setLngLat([loc.lon, loc.lat]).addTo(map);

      // A missing basemap must say so, not silently float radar blobs on
      // gray: probe the archive (cheap HEAD; the server 404s with a README
      // pointer until it is installed) and surface basemap source errors
      // (covers a corrupt archive, whose header parse fails after load).
      if (fetchFn) {
        fetchFn("/basemap.pmtiles", { method: "HEAD" }).then(function (r) {
          if (!r.ok) {
            showMapNotice("Basemap not installed; radar and alerts still " +
              "work. See the README radar section.");
          }
        }).catch(function () { });
      }
      // Error attribution, debounced: radar overlay errors never claim the
      // basemap failed, and even a QUALIFYING error (protomaps source, or
      // untagged before the initial style load) only banners if the
      // protomaps source has STILL never reported loaded after the grace
      // period. A transient tile error on a basemap that then renders must
      // never banner; the basemap reaching loaded cancels a pending banner
      // and dismisses a shown one. Every qualifying error is logged with
      // its cause so a connected inspector has the history.
      var styleLoaded = false, basemapOk = false;
      var noticeTimer = null, lastBasemapErr = null;
      var noticeGraceMs = env.noticeGraceMs || NOTICE_GRACE_MS;
      var con = win && win.console;

      function basemapLoadedNow() {
        if (basemapOk) return true;
        if (!map || typeof map.isSourceLoaded !== "function") return false;
        try { return !!map.isSourceLoaded("protomaps"); }
        catch (e) { return false; }
      }

      map.on("load", function () { styleLoaded = true; });
      map.on("sourcedata", function (ev) {
        if (ev && ev.sourceId === "protomaps" && ev.isSourceLoaded) {
          basemapOk = true;
          if (noticeTimer) { win.clearTimeout(noticeTimer); noticeTimer = null; }
          dismissMapNotice();
        }
      });
      map.on("error", function (ev) {
        if (!basemapNoticeFor(ev, styleLoaded, basemapOk)) return;
        lastBasemapErr = errorDetail(ev);
        if (con && con.warn) con.warn("[aeolus-basemap]", lastBasemapErr);
        note("basemap-error", lastBasemapErr);
        if (mapNoticeShown || noticeTimer) return;
        noticeTimer = win.setTimeout(function () {
          noticeTimer = null;
          if (basemapLoadedNow()) return;  // it recovered; stay quiet
          showMapNotice("Basemap failed to load; radar and alerts still work.",
            lastBasemapErr);
        }, noticeGraceMs);
      });

      map.on("load", addOverlays);        // initial style only
      map.on("click", "alerts-fill", onAlertClick);
      map.on("mouseenter", "alerts-fill", function () {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "alerts-fill", function () {
        map.getCanvas().style.cursor = "";
      });

      // Theme flips swap the whole style; overlays are re-added on the new
      // style's load (setStyle drops every custom source and layer).
      if (win.matchMedia) {
        var mq = win.matchMedia("(prefers-color-scheme: dark)");
        var flip = function () {
          if (!map) return;
          mapReady = false;
          map.setStyle(styleUrl());
          map.once("style.load", addOverlays);
        };
        if (mq.addEventListener) mq.addEventListener("change", flip);
        else if (mq.addListener) mq.addListener(flip);
      }
      note("map", "init");
    }

    /* Idempotent per style: alert source + fill/line layers (colored from
       the app's severity tokens), then the loop's frame layers restored
       beneath them. Runs on first load and again after every setStyle. */
    function addOverlays() {
      mapReady = true;
      added = {};
      var tokens = sevTokens();
      if (!map.getSource("alerts")) {
        map.addSource("alerts", { type: "geojson", data: alertData });
      }
      if (!map.getLayer("alerts-fill")) {
        map.addLayer({
          id: "alerts-fill", type: "fill", source: "alerts",
          paint: { "fill-color": sevMatch(tokens), "fill-opacity": 0.14 }
        });
      }
      if (!map.getLayer("alerts-line")) {
        map.addLayer({
          id: "alerts-line", type: "line", source: "alerts",
          paint: { "line-color": sevMatch(tokens), "line-width": 1.6 }
        });
      }
      note("overlays", alertData.features.length);
      if (frames.length) {
        for (var i = windowStart; i < frames.length; i++) ensureFrame(i);
        if (cur >= 0 && frames[cur]) setOpacity(frames[cur].layer, RADAR_OPACITY);
      }
    }

    function onAlertClick(e) {
      var f = e.features && e.features[0];
      if (!f) return;
      var p = f.properties || {};
      if (popup) popup.remove();
      popup = new ml.Popup({ closeButton: true, maxWidth: "280px" })
        .setLngLat(e.lngLat)
        .setHTML('<div class="radar-popup"><strong>' + esc(p.event) + "</strong>" +
          (p.headline ? "<p>" + esc(p.headline) + "</p>" : "") + "</div>")
        .addTo(map);
      note("popup", p.event);
    }

    // ---- frames ----

    /* Add one frame's source + hidden layer. IEM frames are raster tiles
       through the app's proxy-cache; nowCOAST fallback frames are one
       whole-bbox image each. Frame layers always sit under alerts-fill. */
    function ensureFrame(i) {
      var f = frames[i];
      if (!f || !map || !mapReady || added[f.layer]) return;
      var srcId = frameSourceId(f.layer), layerId = frameLayerId(f.layer);
      if (!map.getSource(srcId)) {
        if (f.url) {
          var b = f.bbox || bbox;
          map.addSource(srcId, {
            type: "image", url: abs(f.url),
            coordinates: [[b[0], b[3]], [b[2], b[3]], [b[2], b[1]], [b[0], b[1]]]
          });
        } else {
          map.addSource(srcId, {
            type: "raster",
            tiles: [absTemplate(tileTemplate.replace("{layer}", f.layer))],
            tileSize: 256, minzoom: 0, maxzoom: MAX_TILE_ZOOM,
            bounds: bbox || undefined
          });
        }
      }
      if (!map.getLayer(layerId)) {
        map.addLayer({
          id: layerId, type: "raster", source: srcId,
          paint: {
            "raster-opacity": 0,
            "raster-fade-duration": 0,
            "raster-opacity-transition": { duration: 0, delay: 0 }
          }
        }, map.getLayer("alerts-fill") ? "alerts-fill" : undefined);
      }
      added[f.layer] = true;
      note("frame-added", f.layer);
    }

    function setOpacity(layer, v) {
      var id = frameLayerId(layer);
      if (map && added[layer] && map.getLayer(id)) {
        map.setPaintProperty(id, "raster-opacity", v);
      }
      note("opacity", [id, v]);
    }

    function show(i) {
      if (i < 0 || i >= frames.length) return;
      ensureFrame(i);
      if (i < windowStart) windowStart = i;
      if (cur >= 0 && cur !== i && frames[cur]) setOpacity(frames[cur].layer, 0);
      setOpacity(frames[i].layer, RADAR_OPACITY);
      cur = i;
      gateWaited = 0;
      updateBar();
    }

    function indexOfLayer(layer) {
      for (var i = 0; i < frames.length; i++) {
        if (frames[i].layer === layer) return i;
      }
      return -1;
    }

    function loadFrames() {
      return fetchJSON("/v1/radar/frames.json").then(function (r) {
        if (!r.ok || !r.body || !r.body.frames) { updateBar(); return; }
        tileTemplate = r.body.tile_url || tileTemplate;
        bbox = r.body.bbox || bbox;
        stale = !!r.body.stale;
        applyFrames(playableFrames(r.body.frames, r.body.loop_hours, nowFn()));
      });
    }

    /* Reconcile the loop with a fresh frame list: drop pruned frames' map
       layers, keep the current/window position by layer name, and on the
       very first load show the newest frame immediately, start the warming
       pass, and autoplay (unless the user paused this session). */
    function applyFrames(list) {
      var incoming = {};
      list.forEach(function (f) { incoming[f.layer] = true; });
      frames.forEach(function (f) {
        if (!incoming[f.layer] && added[f.layer] && map) {
          if (map.getLayer(frameLayerId(f.layer))) map.removeLayer(frameLayerId(f.layer));
          if (map.getSource(frameSourceId(f.layer))) map.removeSource(frameSourceId(f.layer));
          delete added[f.layer];
        }
      });
      var first = frames.length === 0;
      var curLayer = cur >= 0 && frames[cur] ? frames[cur].layer : null;
      var wsLayer = frames[windowStart] ? frames[windowStart].layer : null;
      frames = list;
      cur = curLayer ? indexOfLayer(curLayer) : -1;
      windowStart = wsLayer ? Math.max(0, indexOfLayer(wsLayer)) : 0;
      if (!frames.length) { cur = -1; windowStart = 0; updateBar(); return; }
      if (first) {
        cur = frames.length - 1;
        windowStart = cur;
        show(cur);
        startWarming();
        maybeAutoplay();
      } else {
        ensureFrame(frames.length - 1);  // the loop must reach the newest
        if (cur < 0) { cur = frames.length - 1; show(cur); }
      }
      updateBar();
    }

    /* SW-cache warming, newest frame backward: fetch each frame's viewport
       tiles (the service worker files them into the radar bucket), then add
       the frame's hidden layer and extend the playable window down to it.
       The loop therefore starts on the newest frame instantly and deepens
       toward the full 6 hours as this walks. */
    function startWarming() {
      if (!frames.length || !map) return;
      var gen = ++warmGen;
      var z = Math.max(4, Math.min(MAX_TILE_ZOOM,
        Math.round(map.getZoom ? map.getZoom() : 7)));
      var w = loc.lon - 2.5, s = loc.lat - 1.7, e = loc.lon + 2.5, n = loc.lat + 1.7;
      if (map.getBounds) {
        var vb = map.getBounds();
        w = vb.getWest(); s = vb.getSouth(); e = vb.getEast(); n = vb.getNorth();
      }
      if (bbox) {
        w = Math.max(w, bbox[0]); s = Math.max(s, bbox[1]);
        e = Math.min(e, bbox[2]); n = Math.min(n, bbox[3]);
      }
      var tiles = viewportTiles(w, s, e, n, z).slice(0, MAX_WARM_TILES);
      var order = [];
      for (var i = frames.length - 1; i >= 0; i--) order.push(frames[i].layer);
      (function walk(k) {
        if (gen !== warmGen || k >= order.length) return;
        var idx = indexOfLayer(order[k]);   // survives refresh reindexing
        if (idx < 0) { walk(k + 1); return; }
        var f = frames[idx];
        var urls = f.url ? [abs(f.url)] : tiles.map(function (t) {
          return abs(tileUrlFor(tileTemplate, f.layer, t));
        });
        note("warm", f.layer);
        // Small sequential batches, not one Promise.all burst: the tile
        // proxy has few upstream slots and a 32-wide volley from every
        // client would starve the worker thread pool.
        (function batch(j) {
          if (gen !== warmGen) return;
          if (j < urls.length) {
            Promise.all(urls.slice(j, j + WARM_BATCH).map(function (u) {
              return fetchFn(u).catch(function () { });
            })).then(function () { batch(j + WARM_BATCH); });
            return;
          }
          var at = indexOfLayer(f.layer);
          if (at >= 0) {
            ensureFrame(at);
            if (at < windowStart) windowStart = at;
          }
          win.setTimeout(function () { walk(k + 1); }, 60);
        })(0);
      })(0);
    }

    // ---- alerts ----

    function loadAlerts() {
      return fetchJSON("/v1/radar/alerts.geojson").then(function (r) {
        if (!r.ok || !r.body || r.body.type !== "FeatureCollection") return;
        alertData = { type: "FeatureCollection", features: r.body.features || [] };
        if (map && mapReady) {
          var src = map.getSource("alerts");
          if (src && src.setData) src.setData(alertData);
        }
        note("alerts", alertData.features.length);
      });
    }

    // ---- playback ----

    function scheduleTick() {
      win.clearTimeout(playTimer);
      var delay = cur === frames.length - 1 ? DWELL_MS : STEP_MS;
      playTimer = win.setTimeout(tick, delay);
    }

    /* True when frame i's map source reports its tiles loaded. Gated per
       source (map.isSourceLoaded), never map-wide: alert polygons or some
       other frame's stragglers must not hold the loop hostage. Calling
       ensureFrame here preloads the next frame's layer (hidden) so its
       tiles are already in flight while the current frame holds. */
    function frameReady(i) {
      var f = frames[i];
      if (!f || !map || !mapReady) return true;  // nothing to gate against
      ensureFrame(i);
      if (typeof map.isSourceLoaded !== "function") return true;
      try { return !!map.isSourceLoaded(frameSourceId(f.layer)); }
      catch (e) { return true; }
    }

    function tick() {
      if (!playing) return;
      var i = nextIndex(cur, windowStart, frames.length);
      if (i < 0) { scheduleTick(); return; }
      if (!gateAdvance(frameReady(i), gateWaited, GATE_TIMEOUT_MS)) {
        // Hold the CURRENT frame (shown layers stay mounted, so nothing
        // blanks or skips) and re-check shortly; gateAdvance's timeout
        // guarantees the loop cannot stall on one dead tile.
        gateWaited += GATE_POLL_MS;
        win.clearTimeout(playTimer);
        playTimer = win.setTimeout(tick, GATE_POLL_MS);
        note("gate", frames[i].layer);
        return;
      }
      gateWaited = 0;
      show(i);
      scheduleTick();
    }

    function startPlay() {
      if (playing) return;
      if (!frames.length) { updateBar(); return; }
      playing = true;
      scheduleTick();
      updateBar();
      note("play", cur);
    }

    function pause(byUser) {
      playing = false;
      win.clearTimeout(playTimer);
      if (byUser) userPaused = true;
      updateBar();
      note("pause", !!byUser);
    }

    function maybeAutoplay() {
      if (active && !userPaused && !playing) startPlay();
    }

    // ---- scrubber bar ----

    function wireBar() {
      var btn = $("radarPlay"), scrub = $("radarScrub");
      if (btn) {
        btn.onclick = function () {
          if (playing) { pause(true); }
          else { userPaused = false; startPlay(); }
        };
      }
      if (scrub) {
        scrub.oninput = function () {
          // Read the drag position BEFORE pause(): updateBar snaps the
          // slider back to the current frame, which would undo the drag.
          var i = parseInt(scrub.value, 10);
          pause(true);  // scrubbing is an explicit "let me look"
          if (!isNaN(i)) show(i);
        };
      }
      updateBar();
    }

    function updateBar() {
      var btn = $("radarPlay"), scrub = $("radarScrub"), time = $("radarTime");
      if (btn) {
        btn.innerHTML = playing ? PAUSE_SVG : PLAY_SVG;
        btn.setAttribute("aria-label", playing ? "Pause" : "Play");
      }
      if (scrub) {
        scrub.max = String(Math.max(0, frames.length - 1));
        if (cur >= 0) scrub.value = String(cur);
        scrub.disabled = !frames.length;
      }
      if (time) {
        if (!frames.length) {
          time.textContent = "No radar frames yet";
        } else if (cur >= 0 && frames[cur]) {
          time.textContent = fmtFrameTime(frames[cur].valid) +
            (stale ? " · stale" : "");
        }
        if (time.classList) time.classList.toggle("stale", stale);
      }
    }

    // ---- view lifecycle ----

    function enter() {
      active = true;
      if (!map) {
        initMap();
        wireBar();
      } else if (map.resize) {
        map.resize();  // the container was display:none while hidden
      }
      loadFrames().then(maybeAutoplay);
      loadAlerts();
      win.clearInterval(refreshTimer);
      refreshTimer = win.setInterval(function () {
        loadFrames();
        loadAlerts();
      }, REFRESH_MS);
      if (!userPaused) startPlay();
    }

    function leave() {
      active = false;
      win.clearInterval(refreshTimer);
      refreshTimer = null;
      win.clearTimeout(playTimer);
      playing = false;  // autoplay resumes on re-entry unless user paused
      if (popup) { popup.remove(); popup = null; }
    }

    return {
      enter: enter,
      leave: leave,
      show: show,
      debug: function () {
        return {
          playing: playing, userPaused: userPaused, active: active,
          cur: cur, windowStart: windowStart, frameCount: frames.length,
          frames: frames, alertFeatures: alertData.features.length, log: log
        };
      }
    };
  }

  // ---- browser singleton ----

  var singleton = null;

  return {
    LOCATION: LOCATION,
    setLocation: function (lat, lon, name) {
      // Called by the app once /v1/locations resolves so the map centers on
      // the configured default location instead of the fallback above.
      if (typeof lat === "number" && isFinite(lat)) LOCATION.lat = lat;
      if (typeof lon === "number" && isFinite(lon)) LOCATION.lon = lon;
      if (name) LOCATION.name = name;
    },
    RADAR_OPACITY: RADAR_OPACITY,
    frameLayerId: frameLayerId,
    frameSourceId: frameSourceId,
    fmtFrameTime: fmtFrameTime,
    nextIndex: nextIndex,
    playableFrames: playableFrames,
    basemapNoticeFor: basemapNoticeFor,
    errorDetail: errorDetail,
    gateAdvance: gateAdvance,
    tileAt: tileAt,
    viewportTiles: viewportTiles,
    tileUrlFor: tileUrlFor,
    sevMatch: sevMatch,
    esc: esc,
    create: create,
    enter: function () {
      if (!singleton) singleton = create();
      singleton.enter();
    },
    leave: function () {
      if (singleton) singleton.leave();
    }
  };
});
