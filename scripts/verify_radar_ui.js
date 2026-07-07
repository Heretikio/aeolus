/* Radar tab verification harness. Node only, zero dependencies: drives the
   real static/app.js, static/radar.js and static/sw.js sources against DOM,
   MapLibre and Cache API stubs and asserts the behaviors the pytest suite
   cannot reach:

   1. the hash router registers/enters/leaves the radar view
   2. the loop autoplays on open and pause persists across view switches
   3. the scrubber maps frame index -> frame layer id (opacity flip)
   4. the service worker's radar bucket cap logic and request routing
   5. the alert layer renders seeded GeoJSON with the app's severity colors
   6. the SW shell precache list matches files that actually exist
   7. basemap error attribution: radar source errors never show the notice
   8. frames refreshes keep the timestamp-keyed loop position
   9. the loop drops frames inside the prune safety margin
   10. frame advance gates on tiles-loaded, with an anti-stall timeout

   Usage: node scripts/verify_radar_ui.js  (exit 0 = all green) */

"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
const STATIC = path.join(ROOT, "static");

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks += 1;
  console.log("  ok - " + label);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- shared DOM stub ----

function makeElement(id) {
  const classes = new Set();
  return {
    id, hidden: false, innerHTML: "", textContent: "", value: "0", max: "0",
    disabled: false, style: {}, attrs: {}, onclick: null, oninput: null,
    onchange: null, className: "",
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, f) => {
        if (f === undefined) f = !classes.has(c);
        f ? classes.add(c) : classes.delete(c);
        return f;
      },
    },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    appendChild() {},
  };
}

function makeDocument() {
  const els = {};
  return {
    _els: els,
    documentElement: makeElement("root"),
    getElementById(id) { return els[id] || (els[id] = makeElement(id)); },
    createElement(tag) { return makeElement("created-" + tag); },
    addEventListener() {},
  };
}

// ---- MapLibre stub ----

function makeMaplibreStub() {
  const created = { maps: [], markers: [], popups: [], protocols: [] };

  class StubMap {
    constructor(opts) {
      this.opts = opts;
      this.sources = {};
      this.layers = [];
      this.paint = {};
      this.handlers = {};
      this.calls = [];
      created.maps.push(this);
    }
    on(type, a, b) {
      const layer = b ? a : null, fn = b || a;
      (this.handlers[type] = this.handlers[type] || []).push({ layer, fn });
    }
    once(type, fn) { this.on(type, fn); }
    fire(type, ev) {
      (this.handlers[type] || []).forEach((h) => h.fn(ev || {}));
    }
    addSource(id, def) {
      const self = this;
      this.sources[id] = {
        def,
        data: def.type === "geojson" ? def.data : null,
        setData(d) { self.sources[id].data = d; self.calls.push(["setData", id]); },
      };
    }
    getSource(id) { return this.sources[id]; }
    removeSource(id) { delete this.sources[id]; }
    addLayer(def, before) { this.layers.push({ def, before }); }
    getLayer(id) { return this.layers.some((l) => l.def.id === id) ? { id } : undefined; }
    removeLayer(id) { this.layers = this.layers.filter((l) => l.def.id !== id); }
    setPaintProperty(id, prop, v) {
      (this.paint[id] = this.paint[id] || {})[prop] = v;
      this.calls.push(["paint", id, prop, v]);
    }
    setStyle(url) { this.styleUrl = url; this.sources = {}; this.layers = []; }
    getCanvas() { return { style: {} }; }
    getZoom() { return 7; }
    getBounds() {
      return {
        getWest: () => -96.4, getSouth: () => 37.9,
        getEast: () => -92.4, getNorth: () => 40.1,
      };
    }
    isSourceLoaded(id) { return this._srcLoaded ? this._srcLoaded(id) : true; }
    resize() { this.calls.push(["resize"]); }
  }

  class StubMarker {
    constructor(opts) { this.opts = opts || {}; created.markers.push(this); }
    setLngLat(ll) { this.lngLat = ll; return this; }
    addTo(map) { this.map = map; return this; }
  }

  class StubPopup {
    constructor(opts) { this.opts = opts || {}; created.popups.push(this); }
    setLngLat(ll) { this.lngLat = ll; return this; }
    setHTML(h) { this.html = h; return this; }
    addTo(map) { this.map = map; return this; }
    remove() { this.removed = true; }
  }

  return {
    created,
    maplibregl: {
      Map: StubMap,
      Marker: StubMarker,
      Popup: StubPopup,
      addProtocol(name, fn) { created.protocols.push(name); },
    },
  };
}

// ---- fixtures ----

const FRAMES = [
  { layer: "ridge::USCOMP-N0Q-202607040100", valid: "2026-07-04T01:00:00Z", source: "iem" },
  { layer: "nowcoast-202607040105", valid: "2026-07-04T01:05:00Z", source: "nowcoast",
    url: "/v1/radar/frames/nowcoast-202607040105.png",
    bbox: [-103.5, 30.5, -91.5, 39.5] },
  { layer: "ridge::USCOMP-N0Q-202607040110", valid: "2026-07-04T01:10:00Z", source: "iem" },
  { layer: "ridge::USCOMP-N0Q-202607040115", valid: "2026-07-04T01:15:00Z", source: "iem" },
];

const FRAMES_BODY = {
  source: "iem", fetched_at: "2026-07-04T01:16:00+00:00", stale: false,
  bbox: [-103.5, 30.5, -91.5, 39.5], loop_hours: 6,
  tile_url: "/v1/radar/tiles/{layer}/{z}/{x}/{y}.png",
  frames: FRAMES, count: FRAMES.length,
};

const ALERTS_BODY = {
  type: "FeatureCollection",
  source: "nws", fetched_at: "2026-07-04T01:16:00+00:00", stale: false,
  features: [
    { type: "Feature",
      geometry: { type: "Polygon", coordinates: [[[-95, 39], [-94, 39], [-94, 40], [-95, 39]]] },
      properties: { event: "Severe Thunderstorm Warning", severity: "Severe",
        headline: "60 mph gusts and quarter size hail", affects_point: true } },
    { type: "Feature",
      geometry: { type: "Polygon", coordinates: [[[-96, 38], [-95, 38], [-95, 39], [-96, 38]]] },
      properties: { event: "Flood Advisory", severity: "Moderate",
        headline: "Minor flooding in low lying areas", affects_point: false } },
  ],
};

const TOKENS = {
  "--sev-extreme-edge": "#d03b3b",
  "--sev-severe-edge": "#ec835a",
  "--sev-moderate-edge": "#fab219",
  "--sev-minor-edge": "#8fa0b0",
};

// Frozen "now" matching the fixtures: the controller filters frames near
// the prune boundary against injected time, so tests must not use wall
// clock or the fixture frames would age out of the loop window.
const NOW_MS = Date.parse("2026-07-04T01:16:00Z");

/* Fresh controller + stubs for the focused tests; framesBody is swappable
   mid-test to simulate a refresh delivering a changed frame list. */
function bootController(opts) {
  opts = opts || {};
  const { created, maplibregl } = makeMaplibreStub();
  const doc = makeDocument();
  const fetchLog = [];
  let framesBody = opts.framesBody || FRAMES_BODY;
  const fetchStub = (url) => {
    fetchLog.push(String(url));
    const p = String(url);
    let body = null;
    if (p.indexOf("/v1/radar/frames.json") !== -1) body = framesBody;
    else if (p.indexOf("/v1/radar/alerts.geojson") !== -1) body = ALERTS_BODY;
    return Promise.resolve({
      ok: true, status: 200,
      json: () => (body ? Promise.resolve(JSON.parse(JSON.stringify(body)))
        : Promise.reject(new Error("not json"))),
    });
  };
  const warns = [];
  const win = {
    URL, location: { href: "http://localhost:5002/" },
    setTimeout, clearTimeout, setInterval, clearInterval,
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: (n) => TOKENS[n] || "" }),
    console: { warn: (...args) => warns.push(args.join(" ")) },
  };
  const AeolusRadar = require(path.join(STATIC, "radar.js"));
  const ctl = AeolusRadar.create({
    window: win, document: doc, maplibregl,
    pmtiles: { Protocol: class { constructor() { this.tile = () => {}; } } },
    fetch: fetchStub,
    now: opts.now || (() => NOW_MS),
    noticeGraceMs: opts.noticeGraceMs,
  });
  return { ctl, created, doc, fetchLog, warns,
    setFramesBody: (b) => { framesBody = b; } };
}

// =====================================================================
// 1 + 5 (browser side) - radar controller: autoplay, scrubber, alerts
// =====================================================================

async function testRadarController() {
  console.log("radar controller (static/radar.js):");
  const AeolusRadar = require(path.join(STATIC, "radar.js"));

  // pure helpers first
  ok(AeolusRadar.tileAt(35.22, -97.44, 7).join(",") === "29,50",
    "tileAt matches the server-side slippy math (35.22,-97.44 z7 -> 29/50)");
  ok(AeolusRadar.nextIndex(-1, 3, 4) === 3, "nextIndex starts at the window");
  ok(AeolusRadar.nextIndex(3, 2, 4) === 2, "nextIndex wraps newest -> windowStart");
  ok(AeolusRadar.nextIndex(2, 2, 4) === 3, "nextIndex advances toward newest");
  ok(AeolusRadar.nextIndex(0, 0, 0) === -1, "nextIndex handles an empty loop");
  ok(AeolusRadar.frameLayerId(FRAMES[1].layer) === "radar-frame-nowcoast-202607040105",
    "frameLayerId is a pure index -> layer id mapping");

  const { created, maplibregl } = makeMaplibreStub();
  const doc = makeDocument();
  const fetchLog = [];
  const fetchStub = (url) => {
    fetchLog.push(String(url));
    const p = String(url);
    let body = null;
    if (p.indexOf("/v1/radar/frames.json") !== -1) body = FRAMES_BODY;
    else if (p.indexOf("/v1/radar/alerts.geojson") !== -1) body = ALERTS_BODY;
    return Promise.resolve({
      ok: true, status: 200,
      json: () => (body ? Promise.resolve(JSON.parse(JSON.stringify(body)))
        : Promise.reject(new Error("not json"))),
    });
  };
  const win = {
    URL, location: { href: "http://localhost:5002/" },
    setTimeout, clearTimeout, setInterval, clearInterval,
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: (n) => TOKENS[n] || "" }),
  };

  const ctl = AeolusRadar.create({
    window: win, document: doc, maplibregl,
    pmtiles: { Protocol: class { constructor() { this.tile = () => {}; } } },
    fetch: fetchStub,
    now: () => NOW_MS,   // fixture frames must stay inside the loop window
    location: { lat: 35.22, lon: -97.44, name: "Home" },  // injected by the app in the browser
  });

  ctl.enter();
  const map = created.maps[0];
  ok(!!map, "entering the view constructs the map");
  ok(map.opts.center[0] === -97.44 && map.opts.center[1] === 35.22,
    "map centers on the configured location");
  ok(map.styleUrl === undefined && map.opts.style === "/static/vendor/style-light.json",
    "light theme picks the vendored light style");
  ok(created.protocols.indexOf("pmtiles") !== -1, "pmtiles protocol registered");
  ok(created.markers.length === 1 &&
    created.markers[0].lngLat.join(",") === "-97.44,35.22" &&
    created.markers[0].opts.element.className === "loc-marker",
    "location marker (DOM element, always above canvas layers) at 35.22,-97.44");

  await sleep(20);         // frames.json + alerts.geojson resolve
  map.fire("load");        // style ready -> overlays + pending frames
  await sleep(20);

  const d1 = ctl.debug();
  ok(d1.playing === true, "AUTOPLAY: loop is playing right after open");
  ok(d1.cur === FRAMES.length - 1, "loop opens on the newest frame");
  const newestId = AeolusRadar.frameLayerId(FRAMES[3].layer);
  ok(map.paint[newestId] && map.paint[newestId]["raster-opacity"] === 0.7,
    "newest frame layer shown at 0.7 opacity");

  // alert overlay from seeded GeoJSON
  ok(!!map.getLayer("alerts-fill") && !!map.getLayer("alerts-line"),
    "alert fill + outline layers exist");
  ok(map.sources.alerts.data.features.length === 2,
    "alert source carries the 2 seeded polygons");
  const fill = map.layers.find((l) => l.def.id === "alerts-fill").def;
  const expr = JSON.stringify(fill.paint["fill-color"]);
  ok(expr.indexOf('"Severe","#ec835a"') !== -1 &&
    expr.indexOf('"Moderate","#fab219"') !== -1,
    "alert colors come from the app's severity tokens");
  const frameEntry = map.layers.find((l) => l.def.id === newestId);
  ok(frameEntry.before === "alerts-fill",
    "radar frames render beneath the alert overlay");

  // popup on polygon tap
  map.fire("click", {
    features: [{ properties: ALERTS_BODY.features[0].properties }],
    lngLat: { lng: -97.44, lat: 35.22 },
  });
  const pop = created.popups[created.popups.length - 1];
  ok(pop && pop.html.indexOf("Severe Thunderstorm Warning") !== -1 &&
    pop.html.indexOf("60 mph gusts") !== -1,
    "polygon tap opens a popup with event + headline");

  // warming: newest backward, deepening the playable window
  await sleep(600);
  const warmed = [];
  fetchLog.forEach((u) => {
    const m = /(ridge::USCOMP-N0Q-\d{12}|nowcoast-\d{12})/.exec(u);
    if (m && warmed[warmed.length - 1] !== m[1]) warmed.push(m[1]);
  });
  ok(warmed[0] === FRAMES[3].layer && warmed[1] === FRAMES[2].layer &&
    warmed.indexOf(FRAMES[0].layer) === warmed.length - 1,
    "SW warming walks frames newest -> oldest");
  ok(ctl.debug().windowStart === 0, "playable window deepened to the full loop");
  const ncSource = map.sources[AeolusRadar.frameSourceId(FRAMES[1].layer)];
  ok(ncSource && ncSource.def.type === "image" &&
    ncSource.def.coordinates[0].join(",") === "-103.5,39.5",
    "nowCOAST fallback frame is an image source over its bbox");
  const iemSource = map.sources[AeolusRadar.frameSourceId(FRAMES[0].layer)];
  ok(iemSource && iemSource.def.type === "raster" &&
    iemSource.def.tiles[0] ===
    "http://localhost:5002/v1/radar/tiles/ridge::USCOMP-N0Q-202607040100/{z}/{x}/{y}.png",
    "IEM frames are raster sources on the app's tile proxy");

  // SCRUBBER: index -> layer id
  const scrub = doc.getElementById("radarScrub");
  ok(scrub.max === String(FRAMES.length - 1), "scrubber max tracks the frame count");
  const before = ctl.debug().cur;
  scrub.value = "0";
  scrub.oninput();
  const d2 = ctl.debug();
  ok(d2.cur === 0 && d2.playing === false && d2.userPaused === true,
    "scrubbing shows the chosen frame and pauses the loop");
  const oldId = AeolusRadar.frameLayerId(FRAMES[0].layer);
  ok(map.paint[oldId]["raster-opacity"] === 0.7,
    "scrub to index 0 flips layer " + oldId + " to 0.7");
  ok(before !== 0 ? map.paint[AeolusRadar.frameLayerId(FRAMES[before].layer)]["raster-opacity"] === 0
    : true, "previously shown frame layer drops to opacity 0");

  // PAUSE PERSISTENCE while the tab stays open
  ctl.leave();
  ctl.enter();
  await sleep(20);
  ok(ctl.debug().playing === false && ctl.debug().userPaused === true,
    "user pause persists across leave/re-enter");
  doc.getElementById("radarPlay").onclick();
  ok(ctl.debug().playing === true && ctl.debug().userPaused === false,
    "play button resumes the loop");
  ctl.leave();
  ctl.enter();
  await sleep(20);
  ok(ctl.debug().playing === true,
    "without a user pause, re-entering the view autoplays again");
  ctl.leave();
}

// =====================================================================
// 7 - basemap error attribution (static/radar.js)
// =====================================================================

async function testErrorAttribution() {
  console.log("basemap error attribution (static/radar.js):");
  const AeolusRadar = require(path.join(STATIC, "radar.js"));
  const f = AeolusRadar.basemapNoticeFor;

  ok(f({ sourceId: "radar-src-" + FRAMES[0].layer }, true, true) === false,
    "radar source errors never claim the basemap");
  ok(f({ sourceId: "radar-src-" + FRAMES[0].layer }, false, false) === false,
    "radar source errors stay quiet even before the style loads");
  ok(f({ sourceId: "protomaps" }, false, false) === true,
    "a protomaps failure before the source ever loads IS a basemap failure");
  ok(f({ sourceId: "protomaps" }, true, true) === false,
    "a transient protomaps tile error on a rendering basemap stays quiet");
  ok(f({ error: new Error("style fetch died") }, false, false) === true,
    "untagged errors before the initial style load are the basemap's");
  ok(f({ error: new Error("later trouble") }, true, false) === false,
    "untagged errors after style load do not blame the basemap");

  const detail = AeolusRadar.errorDetail;
  ok(detail({ sourceId: "protomaps", error: new Error("boom") }) === "protomaps: boom",
    "errorDetail names sourceId and message");
  ok(detail({ error: new Error("x".repeat(200)) }).length === 80,
    "errorDetail truncates to 80 chars");
  ok(detail({}) === "unknown error", "errorDetail survives an empty event");

  // controller: rendering basemap + radar errors -> no banner, even after
  // the debounce grace elapses
  const a = bootController({ noticeGraceMs: 120 });
  a.ctl.enter();
  await sleep(20);
  const mapA = a.created.maps[0];
  mapA.fire("load");
  mapA.fire("sourcedata", { sourceId: "protomaps", isSourceLoaded: true });
  mapA.fire("error", { sourceId: "radar-src-" + FRAMES[0].layer,
    error: new Error("upstream tile unavailable") });
  mapA.fire("error", { sourceId: "protomaps", error: new Error("one tile hiccup") });
  await sleep(250);
  ok(!a.ctl.debug().log.some((e) => e[0] === "map-notice"),
    "controller: radar 502s and loaded-basemap hiccups never banner");
  a.ctl.leave();

  // controller: transient protomaps error, source loads during the grace
  // period -> pending banner cancelled, but the error was still logged
  const t = bootController({ noticeGraceMs: 120 });
  t.ctl.enter();
  await sleep(20);
  const mapT = t.created.maps[0];
  mapT.fire("error", { sourceId: "protomaps", error: new Error("aborted range read") });
  mapT.fire("sourcedata", { sourceId: "protomaps", isSourceLoaded: true });
  await sleep(250);
  ok(!t.ctl.debug().log.some((e) => e[0] === "map-notice"),
    "a transient error followed by source-loaded never banners");
  ok(t.warns.some((w) => w.indexOf("[aeolus-basemap]") === 0 &&
    w.indexOf("protomaps: aborted range read") !== -1),
    "the transient error still hit console.warn tagged [aeolus-basemap]");
  t.ctl.leave();

  // controller: persistent never-loaded basemap -> banner after the grace
  // period, carrying the causal detail; loading afterwards auto-dismisses
  const b = bootController({ noticeGraceMs: 120 });
  b.ctl.enter();
  await sleep(20);
  const mapB = b.created.maps[0];
  mapB._srcLoaded = (id) => id !== "protomaps";   // basemap never loads
  mapB.fire("error", { error: new Error("style fetch failed") });
  ok(!b.ctl.debug().log.some((e) => e[0] === "map-notice"),
    "no banner immediately on the first qualifying error (debounced)");
  await sleep(250);
  const shown = b.ctl.debug().log.find((e) => e[0] === "map-notice");
  ok(!!shown && shown[1].indexOf("Basemap failed to load") === 0 &&
    shown[1].indexOf(":: style fetch failed") !== -1,
    "persistent failure banners after the grace with the causal detail");
  mapB.fire("sourcedata", { sourceId: "protomaps", isSourceLoaded: true });
  ok(b.ctl.debug().log.some((e) => e[0] === "map-notice-dismissed"),
    "the basemap reaching loaded auto-dismisses the banner");
  b.ctl.leave();
}

// =====================================================================
// 8 - frames refresh keeps the timestamp-keyed position
// =====================================================================

async function testRefreshKeepsPosition() {
  console.log("frames refresh keeps position (static/radar.js):");
  const h = bootController();
  h.ctl.enter();
  await sleep(20);
  h.created.maps[0].fire("load");
  await sleep(650);   // warming walk completes; the window deepens to 0

  const scrub = h.doc.getElementById("radarScrub");
  scrub.value = "2";
  scrub.oninput();    // pause + show the 01:10 frame
  ok(h.ctl.debug().cur === 2 &&
    h.ctl.debug().frames[2].layer === FRAMES[2].layer,
    "scrubbed to the mid-loop 01:10 frame");

  // typical 5-min tick: oldest frame pruned, one new frame appended
  const next = JSON.parse(JSON.stringify(FRAMES_BODY));
  next.frames = FRAMES.slice(1).concat([{
    layer: "ridge::USCOMP-N0Q-202607040120",
    valid: "2026-07-04T01:20:00Z", source: "iem" }]);
  next.count = next.frames.length;
  h.setFramesBody(next);
  h.ctl.leave();
  h.ctl.enter();      // re-entry re-polls frames.json
  await sleep(30);

  const d = h.ctl.debug();
  ok(d.frameCount === 4 &&
    d.frames[3].layer === "ridge::USCOMP-N0Q-202607040120",
    "refresh dropped the pruned frame and appended the new one");
  ok(d.cur === 1 && d.frames[d.cur].valid === "2026-07-04T01:10:00Z",
    "position follows its frame's timestamp through the refresh (no jump)");
  ok(d.playing === false && d.userPaused === true,
    "the user's pause survives the refresh");
  h.ctl.leave();
}

// =====================================================================
// 9 - the loop drops frames inside the prune safety margin
// =====================================================================

async function testPruneBoundarySkip() {
  console.log("prune-boundary frame skipping (static/radar.js):");
  const AeolusRadar = require(path.join(STATIC, "radar.js"));
  const mk = (iso) => ({
    layer: "ridge::USCOMP-N0Q-" + iso.replace(/[^0-9]/g, "").slice(0, 12),
    valid: iso, source: "iem",
  });

  // 6h loop, now=01:16 -> boundary 19:16, safety-margin cutoff 19:21
  const doomed = mk("2026-07-03T19:18:00Z");   // inside the safety margin
  const safe = mk("2026-07-03T19:25:00Z");
  const newest = mk("2026-07-04T01:15:00Z");
  const out = AeolusRadar.playableFrames([doomed, safe, newest], 6, NOW_MS);
  ok(out.length === 2 && out[0] === safe && out[1] === newest,
    "playableFrames drops only the frames inside the prune safety margin");
  ok(AeolusRadar.playableFrames([doomed, newest], 0, NOW_MS).length === 2,
    "no loop_hours (older server payload) means no filtering");

  // offline replay: wall clock ran far past a frozen SW-cached loop; the
  // boundary anchors near the newest frame, not at wall clock, so drift
  // trims at most the boundary-adjacent frames instead of the whole list
  const frozen = [safe, mk("2026-07-03T20:00:00Z"), newest];  // oldest first
  const later = NOW_MS + 11 * 3600 * 1000;
  const kept = AeolusRadar.playableFrames(frozen, 6, later);
  ok(kept.length === 2 && kept[0] === frozen[1] && kept[1] === newest,
    "a frozen offline loop survives wall-clock drift (only boundary frames trim)");

  // live controller: the doomed frame never becomes part of the loop
  const body = JSON.parse(JSON.stringify(FRAMES_BODY));
  body.frames = [doomed].concat(FRAMES);
  body.count = body.frames.length;
  const h = bootController({ framesBody: body });
  h.ctl.enter();
  await sleep(20);
  h.created.maps[0].fire("load");
  await sleep(20);
  const d = h.ctl.debug();
  ok(d.frameCount === FRAMES.length &&
    d.frames[0].layer === FRAMES[0].layer,
    "the controller's loop excludes frames about to be pruned server-side");
  h.ctl.leave();
}

// =====================================================================
// 10 - advance gating: no frame swap before its tiles are loaded
// =====================================================================

async function testAdvanceGating() {
  console.log("advance gating (static/radar.js):");
  const AeolusRadar = require(path.join(STATIC, "radar.js"));

  ok(AeolusRadar.gateAdvance(true, 0, 1000) === true,
    "a loaded source advances immediately");
  ok(AeolusRadar.gateAdvance(false, 400, 1000) === false,
    "an unloaded source holds the current frame");
  ok(AeolusRadar.gateAdvance(false, 1000, 1000) === true,
    "the timeout advances anyway: one dead tile cannot stall the loop");

  const h = bootController();
  h.ctl.enter();
  await sleep(20);
  const map = h.created.maps[0];
  let loaded = false;
  map._srcLoaded = () => loaded;
  map.fire("load");
  // Past the newest-frame dwell (1400ms): the loop now wants to wrap to
  // frame 0, whose source reports unloaded -> it must gate, not blank-step.
  await sleep(1800);
  const d1 = h.ctl.debug();
  ok(d1.playing === true && d1.cur === FRAMES.length - 1 &&
    d1.log.some((e) => e[0] === "gate" && e[1] === FRAMES[0].layer),
    "unloaded next frame: loop holds the newest frame and logs the gate");
  loaded = true;
  await sleep(300);
  ok(h.ctl.debug().cur !== FRAMES.length - 1,
    "tiles loaded: the loop advances");
  const at = h.ctl.debug().cur;
  loaded = false;
  await sleep(1500);   // > GATE_TIMEOUT_MS + a step
  ok(h.ctl.debug().cur !== at,
    "a source stuck unloaded cannot stall the loop past the gate timeout");
  h.ctl.leave();
}

// =====================================================================
// 2 - the hash router registers the radar view (static/app.js)
// =====================================================================

async function testRouter() {
  console.log("router (static/app.js):");
  const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

  const doc = makeDocument();
  const radarCalls = { enter: 0, leave: 0 };
  const winHandlers = {};
  const chartsStub = {
    esc: (s) => String(s), hourLabel: () => "1p", quarterLabel: () => "1:15p",
    dayLabel: () => "Sat", nowcastStrip: () => "", hourlyChart: () => "",
    dailyRangeBar: () => "", PAD_L: 10, STEP: 34,
  };
  const sandbox = {
    window: {
      AeolusCharts: chartsStub,
      AeolusRadar: { enter: () => radarCalls.enter++, leave: () => radarCalls.leave++ },
      addEventListener: (t, fn) => { winHandlers[t] = fn; },
    },
    document: doc,
    location: { hash: "" },
    localStorage: { getItem: () => null, setItem: () => {} },
    navigator: {},
    fetch: () => Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve(null) }),
    setInterval: () => 0,
    clearInterval: () => {},
    console,
    Date,
  };
  vm.runInNewContext(src, sandbox, { filename: "app.js" });
  await sleep(10);

  ok(typeof winHandlers.hashchange === "function", "app binds hashchange routing");
  ok(doc._els["view-radar"] !== undefined && doc._els["view-radar"].hidden === true,
    "radar view exists in the router and starts hidden");
  ok(radarCalls.leave >= 1 && radarCalls.enter === 0,
    "landing on #/now leaves the radar controller dormant");

  sandbox.location.hash = "#/radar";
  winHandlers.hashchange();
  ok(doc._els["view-radar"].hidden === false, "hash #/radar unhides the radar view");
  ok(doc._els["view-now"].hidden === true, "other views hide");
  ok(doc._els["tab-radar"].getAttribute("aria-current") === "page",
    "radar tab gets aria-current");
  ok(radarCalls.enter === 1, "router enter()s the radar controller");

  sandbox.location.hash = "#/alerts";
  winHandlers.hashchange();
  ok(doc._els["view-radar"].hidden === true && radarCalls.leave >= 2,
    "leaving #/radar hides the view and leave()s the controller");
}

// =====================================================================
// 2b - DAILY view rainfall totals (static/app.js renderDaily)
// =====================================================================

async function testDailyRainfall() {
  console.log("daily rainfall (static/app.js):");
  const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

  const DAILY_BODY = {
    source: "open_meteo", fetched_at: "2026-07-04T01:16:00+00:00",
    stale: false, timezone: "America/Chicago",
    daily: [
      { ts: "2026-07-03", temp_min_f: 68, temp_max_f: 91,
        precip_prob_max_pct: 70, precip_sum_in: 0.32,
        condition: "Rain", condition_code: "rain" },
      { ts: "2026-07-04", temp_min_f: 66, temp_max_f: 88,
        precip_prob_max_pct: 10, precip_sum_in: 0.004,   // rounds to 0.00
        condition: "Partly cloudy", condition_code: "partly_cloudy" },
      { ts: "2026-07-05", temp_min_f: 64, temp_max_f: 86,
        precip_prob_max_pct: 0,                          // field absent
        condition: "Clear", condition_code: "clear" },
    ],
  };
  const LOCATIONS_BODY = {
    source: "local", fetched_at: "2026-07-04T01:16:00+00:00", stale: false,
    locations: [{ id: 1, name: "Home", lat: 35.22, lon: -97.44, is_default: true }],
  };

  const doc = makeDocument();
  const chartsStub = {
    esc: (s) => String(s), hourLabel: () => "1p", quarterLabel: () => "1:15p",
    dayLabel: () => "Sat", nowcastStrip: () => "", hourlyChart: () => "",
    dailyRangeBar: () => "", PAD_L: 10, STEP: 34,
  };
  const sandbox = {
    window: {
      AeolusCharts: chartsStub,
      AeolusRadar: { enter() {}, leave() {} },
      addEventListener() {},
    },
    document: doc,
    location: { hash: "#/daily" },
    localStorage: { getItem: () => null, setItem: () => {} },
    navigator: {},
    fetch: (u) => {
      const p = String(u);
      let body = null;
      if (p.indexOf("/v1/daily") === 0) body = DAILY_BODY;
      else if (p.indexOf("/v1/locations") === 0) body = LOCATIONS_BODY;
      return Promise.resolve({
        ok: !!body, status: body ? 200 : 503,
        json: () => Promise.resolve(body ? JSON.parse(JSON.stringify(body)) : null),
      });
    },
    setInterval: () => 0,
    clearInterval: () => {},
    console,
    Date,
  };
  vm.runInNewContext(src, sandbox, { filename: "app.js" });
  await sleep(20);

  const html = doc._els.dailyList.innerHTML;
  ok(html.indexOf('<span class="day-rain">0.32 in</span>') !== -1,
    "a wet day's row carries its rainfall total in the rain-blue element");
  ok((html.match(/day-rain/g) || []).length === 1,
    "rainfall renders on exactly the one day with a measurable total");
  ok(html.indexOf("0.00") === -1,
    "totals that round to 0.00 render nothing (dry weeks stay quiet)");
  ok(html.indexOf("0.32 in</span></div>") !== -1 &&
    html.indexOf('"day-precip">70%<span class="day-rain">') !== -1,
    "the total sits inside the day-precip cell, under the 70% chance");

  const css = fs.readFileSync(path.join(STATIC, "app.css"), "utf8");
  ok(/\.day-precip \.day-rain\s*{[^}]*var\(--rain\)/.test(css),
    "day-rain styles with the app's rain blue token (theme-aware)");
}

// =====================================================================
// 3 + 4 - service worker: routing + radar bucket cap (static/sw.js)
// =====================================================================

async function testServiceWorker() {
  console.log("service worker (static/sw.js):");
  const src = fs.readFileSync(path.join(STATIC, "sw.js"), "utf8");

  const swHandlers = {};
  const opened = [];
  const fakeCacheFactory = () => ({
    match: () => Promise.resolve(undefined),
    put: () => Promise.resolve(),
    keys: () => Promise.resolve([]),
    delete: () => Promise.resolve(true),
    addAll: () => Promise.resolve(),
  });
  const sandbox = {
    self: {
      addEventListener: (t, fn) => { swHandlers[t] = fn; },
      location: { origin: "http://localhost:5002" },
      skipWaiting: () => Promise.resolve(),
      clients: { claim: () => Promise.resolve() },
    },
    caches: {
      open: (name) => { opened.push(name); return Promise.resolve(fakeCacheFactory()); },
      match: () => Promise.resolve(undefined),
      keys: () => Promise.resolve([]),
      delete: () => Promise.resolve(true),
    },
    fetch: () => Promise.resolve({
      ok: true, redirected: false, url: "http://localhost:5002/x",
      headers: { get: () => "image/png" }, clone() { return this; },
    }),
    URL, Response: class { constructor(b, i) { this.body = b; this.init = i; } },
    console,
  };
  vm.runInNewContext(src, sandbox, { filename: "sw.js" });

  // cap logic: pure eviction plan
  const plan = sandbox.radarEvictionPlan;
  ok(typeof plan === "function", "radarEvictionPlan exists");
  const T = (stamp, n, size) =>
    ({ url: `http://x/v1/radar/tiles/ridge::USCOMP-N0Q-${stamp}/7/30/${n}.png`, size });
  ok(plan([T("202607040100", 1, 10), T("202607040105", 2, 10)], 100).length === 0,
    "under the cap: nothing evicted");
  const entries = [
    T("202607040110", 1, 40 * 1024 * 1024),   // newest, inserted first
    T("202607040100", 2, 40 * 1024 * 1024),   // oldest
    T("202607040105", 3, 40 * 1024 * 1024),   // middle
  ];
  const out = plan(entries, 100 * 1024 * 1024);
  ok(out.length === 1 && out[0].indexOf("202607040100") !== -1,
    "over the cap: evicts the OLDEST frame's tiles first, regardless of insertion order");
  const out2 = plan(entries, 30 * 1024 * 1024);
  ok(out2.length === 3 - 0 && out2[1].indexOf("202607040105") !== -1,
    "keeps evicting oldest-first until under budget");
  ok(plan([{ url: "http://x/v1/radar/frames/nowcoast-202607040050.png", size: 60 * 1024 * 1024 },
    T("202607040100", 1, 60 * 1024 * 1024)], 100 * 1024 * 1024)[0]
    .indexOf("nowcoast-202607040050") !== -1,
    "nowCOAST frame stamps participate in frame-age ordering");
  ok(sandbox.RADAR_MAX_BYTES === 100 * 1024 * 1024, "radar bucket budget is 100 MB");
  ok(sandbox.RADAR_CACHE === "aeolus-radar-v1" &&
    sandbox.RADAR_CACHE.indexOf("__BUILD_REV__") === -1,
    "radar bucket survives deploys (not version-stamped)");

  // trim wiring: sizes from headers, delete called for the oldest frame
  const stored = [
    { url: "http://localhost:5002/v1/radar/tiles/ridge::USCOMP-N0Q-202607040100/7/30/48.png", size: 60 * 1024 * 1024 },
    { url: "http://localhost:5002/v1/radar/tiles/ridge::USCOMP-N0Q-202607040105/7/30/48.png", size: 60 * 1024 * 1024 },
  ];
  const deleted = [];
  const fakeCache = {
    keys: () => Promise.resolve(stored.map((s) => ({ url: s.url }))),
    match: (req) => Promise.resolve({
      headers: { get: (h) => h === "Content-Length"
        ? String(stored.find((s) => s.url === req.url).size) : null },
    }),
    delete: (u) => { deleted.push(u); return Promise.resolve(true); },
  };
  await sandbox.trimRadarCache(fakeCache);
  ok(deleted.length === 1 && deleted[0].indexOf("202607040100") !== -1,
    "trimRadarCache deletes the oldest frame's entries to fit 100 MB");

  // fetch routing: tiles -> radar bucket, frames.json -> data cache,
  // basemap -> untouched passthrough
  function dispatch(pathname) {
    opened.length = 0;
    let responded = false;
    swHandlers.fetch({
      request: { method: "GET", url: "http://localhost:5002" + pathname, mode: "cors" },
      respondWith: (p) => { responded = true; return p; },
    });
    return sleep(5).then(() => ({ responded, opened: opened.slice() }));
  }
  let r = await dispatch("/v1/radar/tiles/ridge::USCOMP-N0Q-202607040100/7/30/48.png");
  ok(r.responded && r.opened.indexOf("aeolus-radar-v1") !== -1,
    "tile requests are served cache-first from the radar bucket");
  r = await dispatch("/v1/radar/frames/nowcoast-202607040100.png");
  ok(r.responded && r.opened.indexOf("aeolus-radar-v1") !== -1,
    "nowCOAST frame images also land in the radar bucket");
  r = await dispatch("/v1/radar/frames.json");
  ok(r.responded && r.opened.length && r.opened[0].indexOf("-data") !== -1,
    "frames.json stays network-first in the data cache");
  r = await dispatch("/v1/radar/alerts.geojson");
  ok(r.responded && r.opened[0].indexOf("-data") !== -1,
    "alerts.geojson stays network-first in the data cache");
  r = await dispatch("/basemap.pmtiles");
  ok(!r.responded, "basemap Range reads bypass the service worker");
}

// =====================================================================
// 6 - the precache list points at real files
// =====================================================================

function testShellFiles() {
  console.log("shell precache list:");
  const src = fs.readFileSync(path.join(STATIC, "sw.js"), "utf8");
  const m = /var SHELL = \[([\s\S]*?)\];/.exec(src);
  assert.ok(m, "SHELL array found");
  const entries = [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
  ok(entries.length > 20, "SHELL lists the vendored radar assets (" + entries.length + " entries)");
  for (const e of entries) {
    let p;
    if (e === "/") p = path.join(STATIC, "index.html");
    else if (e === "/manifest.json") p = path.join(STATIC, "manifest.json");
    else p = path.join(ROOT, decodeURIComponent(e).replace(/^\//, ""));
    assert.ok(fs.existsSync(p), "missing shell file: " + e + " -> " + p);
  }
  ok(true, "every SHELL entry exists on disk (incl. %20-encoded font paths)");

  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  for (const needle of [
    "/static/vendor/maplibre-gl.js", "/static/vendor/maplibre-gl.css",
    "/static/vendor/pmtiles.js", "/static/radar.js",
    'id="view-radar"', 'id="tab-radar"', 'id="radarMap"', 'id="radarScrub"',
    "Iowa Environmental Mesonet",
  ]) {
    assert.ok(html.indexOf(needle) !== -1, "index.html missing: " + needle);
  }
  ok(true, "index.html wires the radar view, tab, vendor scripts and attribution");

  for (const styleName of ["style-light.json", "style-dark.json"]) {
    const style = JSON.parse(fs.readFileSync(path.join(STATIC, "vendor", styleName), "utf8"));
    assert.ok(style.sources.protomaps.url === "pmtiles:///basemap.pmtiles",
      styleName + " must read the app's own basemap");
    assert.ok(style.glyphs.indexOf("/static/vendor/fonts/") === 0 &&
      style.sprite.indexOf("/static/vendor/sprites/") === 0,
      styleName + " must use vendored glyphs/sprites");
    assert.ok(style.layers.length > 20, styleName + " has real layers");
  }
  ok(true, "both basemap styles are fully self-hosted (no CDN URLs)");
}

// ---- run ----

(async () => {
  try {
    await testRadarController();
    await testErrorAttribution();
    await testRefreshKeepsPosition();
    await testPruneBoundarySkip();
    await testAdvanceGating();
    await testRouter();
    await testDailyRainfall();
    await testServiceWorker();
    testShellFiles();
    console.log("\nALL GREEN: " + checks + " checks passed");
    process.exit(0);
  } catch (err) {
    console.error("\nFAILED:", err.message);
    console.error(err.stack);
    process.exit(1);
  }
})();
