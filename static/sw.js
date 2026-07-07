/* Aeolus service worker.
   Shell and static assets: cache-first (the app opens instantly, offline).
   /v1/* data: network-first with cache fallback, so a dead connection still
   shows the last known payloads and the app badges them stale itself.
   Radar tiles/frames: cache-first in a dedicated LRU bucket (immutable
   timestamped URLs; scrubbing the loop costs the network nothing). */

/* __BUILD_REV__ is stamped at image build time (see Dockerfile); a new
   deploy changes this byte, the browser re-checks /sw.js (served no-cache),
   and installed clients pick up the fresh shell. */
var VERSION = "aeolus-__BUILD_REV__";
var SHELL_CACHE = VERSION + "-shell";
var DATA_CACHE = VERSION + "-data";

/* The radar bucket is deliberately NOT version-tied: its tiles are
   immutable (the frame timestamp is in the URL), so a deploy must not
   throw away a warmed 6h loop. Size is bounded below instead. */
var RADAR_CACHE = "aeolus-radar-v1";
var RADAR_MAX_BYTES = 100 * 1024 * 1024;  // ~6h loop budget
var RADAR_TRIM_EVERY = 32;                // puts between trim sweeps
var RADAR_FALLBACK_BYTES = 12 * 1024;     // when an entry's size is unknowable

var SHELL = [
  "/",
  "/static/app.css",
  "/static/app.js",
  "/static/charts.js",
  "/static/radar.js",
  "/static/vendor/maplibre-gl.js",
  "/static/vendor/maplibre-gl.css",
  "/static/vendor/pmtiles.js",
  "/static/vendor/style-light.json",
  "/static/vendor/style-dark.json",
  "/static/vendor/sprites/light.json",
  "/static/vendor/sprites/light.png",
  "/static/vendor/sprites/light@2x.json",
  "/static/vendor/sprites/light@2x.png",
  "/static/vendor/sprites/dark.json",
  "/static/vendor/sprites/dark.png",
  "/static/vendor/sprites/dark@2x.json",
  "/static/vendor/sprites/dark@2x.png",
  "/static/vendor/fonts/Noto%20Sans%20Regular/0-255.pbf",
  "/static/vendor/fonts/Noto%20Sans%20Regular/256-511.pbf",
  "/static/vendor/fonts/Noto%20Sans%20Medium/0-255.pbf",
  "/static/vendor/fonts/Noto%20Sans%20Medium/256-511.pbf",
  "/static/vendor/fonts/Noto%20Sans%20Italic/0-255.pbf",
  "/static/vendor/fonts/Noto%20Sans%20Italic/256-511.pbf",
  "/manifest.json",
  "/static/icons/aeolus.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon.png"
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(SHELL_CACHE).then(function (c) { return c.addAll(SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        if (k !== SHELL_CACHE && k !== DATA_CACHE && k !== RADAR_CACHE) {
          return caches.delete(k);
        }
      }));
    }).then(function () {
      // Enforce the radar budget on every SW startup: the put counter is
      // in-memory and resets when the worker is killed, so counter-driven
      // trims alone could leave the bucket growing across short SW lives.
      return caches.open(RADAR_CACHE).then(trimRadarCache);
    }).then(function () { return self.clients.claim(); })
  );
});

/* Only genuine data may enter the caches. Behind an auth proxy an expired
   session can answer any GET with a 200 login page (often via redirect);
   caching that would poison the offline fallback or brick the shell. */
function cacheableData(resp) {
  return resp.ok && !resp.redirected &&
    new URL(resp.url).origin === self.location.origin &&
    (resp.headers.get("Content-Type") || "").indexOf("application/json") !== -1;
}

function cacheableShell(resp, path) {
  return resp.ok && !resp.redirected &&
    new URL(resp.url).origin === self.location.origin &&
    SHELL.indexOf(path) !== -1;
}

function cacheableRadar(resp) {
  return resp.ok && !resp.redirected &&
    new URL(resp.url).origin === self.location.origin &&
    (resp.headers.get("Content-Type") || "").indexOf("image/png") !== -1;
}

function networkFirst(req) {
  return caches.open(DATA_CACHE).then(function (cache) {
    return fetch(req).then(function (resp) {
      if (cacheableData(resp)) cache.put(req, resp.clone());
      return resp;
    }).catch(function () {
      return cache.match(req).then(function (hit) {
        if (hit) return hit;
        return new Response(JSON.stringify({ error: "offline and no cached data" }), {
          status: 503,
          headers: { "Content-Type": "application/json" }
        });
      });
    });
  });
}

function cacheFirst(req, cacheKey) {
  return caches.match(cacheKey || req).then(function (hit) {
    if (hit) return hit;
    return fetch(req).then(function (resp) {
      var path = cacheKey || new URL(req.url).pathname;
      if (cacheableShell(resp, path)) {
        var clone = resp.clone();
        caches.open(SHELL_CACHE).then(function (c) { c.put(cacheKey || req, clone); });
      }
      return resp;
    });
  });
}

/* ---- radar bucket: cache-first with an LRU-by-frame-age size cap ---- */

/* The 12-digit frame timestamp inside a radar URL (tile or fallback frame);
   "" for anything unparsable, which sorts first and is evicted first. */
function radarFrameStamp(url) {
  var m = /(?:N0Q-|nowcoast-)(\d{12})/.exec(url);
  return m ? m[1] : "";
}

/* Pure cap logic (node-verified): given [{url, size}] and a byte budget,
   return the urls to evict, oldest frame first, until the rest fits.
   Oldest-frame-first IS the right LRU here: the loop window slides forward
   in time, so the least recently useful tiles are exactly the oldest. */
function radarEvictionPlan(entries, maxBytes) {
  var total = 0;
  entries.forEach(function (en) { total += en.size; });
  var order = entries.slice().sort(function (a, b) {
    var sa = radarFrameStamp(a.url), sb = radarFrameStamp(b.url);
    return sa < sb ? -1 : (sa > sb ? 1 : 0);
  });
  var out = [];
  for (var i = 0; total > maxBytes && i < order.length; i++) {
    out.push(order[i].url);
    total -= order[i].size;
  }
  return out;
}

function trimRadarCache(cache) {
  return cache.keys().then(function (reqs) {
    return Promise.all(reqs.map(function (req) {
      return cache.match(req).then(function (resp) {
        if (!resp) return { url: req.url, size: 0 };
        var len = Number(resp.headers.get("Content-Length"));
        if (len > 0) return { url: req.url, size: len };
        return resp.blob().then(function (b) {
          return { url: req.url, size: b.size || RADAR_FALLBACK_BYTES };
        });
      });
    })).then(function (entries) {
      var doomed = radarEvictionPlan(entries, RADAR_MAX_BYTES);
      return Promise.all(doomed.map(function (u) { return cache.delete(u); }));
    });
  });
}

var radarPuts = 0;

function radarCacheFirst(req, event) {
  return caches.open(RADAR_CACHE).then(function (cache) {
    return cache.match(req).then(function (hit) {
      if (hit) return hit;
      return fetch(req).then(function (resp) {
        if (cacheableRadar(resp)) {
          var clone = resp.clone();
          // First put after SW startup trims too (the counter resets when
          // the worker is killed); waitUntil keeps the SW alive through
          // the put+trim, and a rejected put (quota) must not break the
          // response.
          var done = cache.put(req, clone).then(function () {
            radarPuts += 1;
            if (radarPuts === 1 || radarPuts % RADAR_TRIM_EVERY === 0) {
              return trimRadarCache(cache);
            }
          }).catch(function () { });
          if (event && event.waitUntil) event.waitUntil(done);
        }
        return resp;
      });
    });
  });
}

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname === "/basemap.pmtiles") {
    return;  // Range reads; the Cache API cannot store partial responses
  }
  if (url.pathname.indexOf("/v1/radar/tiles/") === 0 ||
      url.pathname.indexOf("/v1/radar/frames/") === 0) {
    e.respondWith(radarCacheFirst(req, e));
  } else if (url.pathname.indexOf("/v1/") === 0) {
    e.respondWith(networkFirst(req));  // incl. frames.json + alerts.geojson
  } else if (req.mode === "navigate") {
    e.respondWith(cacheFirst(req, "/"));
  } else {
    e.respondWith(cacheFirst(req));
  }
});
