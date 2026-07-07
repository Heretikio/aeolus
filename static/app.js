/* Aeolus PWA frontend. Vanilla JS, hash-routed views, honest staleness.
   Every view is rendered from /v1 payloads that carry fetched_at and stale;
   the service worker keeps the last payloads available offline. */

(function () {
  "use strict";

  var C = window.AeolusCharts;

  // ---- condition mapping (canonical Aeolus schema) ----
  // The API serves condition (human string) and condition_code (normalized
  // enum) for every source; this map only picks the glyph.

  var COND_ICON = {
    clear: "clear", partly_cloudy: "partly", cloudy: "cloud", fog: "fog",
    drizzle: "drizzle", rain: "rain", freezing_rain: "sleet", sleet: "sleet",
    snow: "snow", hail: "storm", thunderstorm: "storm", windy: "cloud",
    tornado: "storm", unknown: "cloud"
  };

  function condition(row, isDay) {
    var icon = COND_ICON[row && row.condition_code] || "cloud";
    if (icon === "clear") icon = isDay ? "sun" : "moon";
    if (icon === "partly") icon = isDay ? "sun-cloud" : "moon-cloud";
    return { label: (row && row.condition) || "Unknown", icon: icon };
  }

  // ---- inline SVG condition icons (stroke glyphs, no assets) ----

  var CLOUD = 'M17.5 18.5h-11a3.6 3.6 0 0 1-.55-7.16 5.5 5.5 0 0 1 10.85-1.4A4.1 4.1 0 0 1 17.5 18.5z';
  var SUN_CORE = '<circle class="ic-sun" cx="12" cy="12" r="4.2"/>' +
    '<path class="ic-sun" d="M12 3.2v2M12 18.8v2M3.2 12h2M18.8 12h2M5.9 5.9l1.4 1.4M16.7 16.7l1.4 1.4M18.1 5.9l-1.4 1.4M7.3 16.7l-1.4 1.4"/>';
  var SMALL_SUN = '<circle class="ic-sun" cx="16.5" cy="7" r="2.6"/>' +
    '<path class="ic-sun" d="M16.5 2.6v1.4M16.5 10v1.2M12.1 7h1.4M20.9 7h-1.4M13.4 3.9l1 1M19.6 10.1l-.8-.8M19.6 3.9l-1 1"/>';
  var SMALL_MOON = '<path d="M20 8.6A4.5 4.5 0 1 1 14.6 3 3.6 3.6 0 0 0 20 8.6z"/>';
  var LOW_CLOUD = 'M16.5 20h-9a3 3 0 0 1-.46-5.96 4.6 4.6 0 0 1 9.07-1.17A3.4 3.4 0 0 1 16.5 20z';

  var ICONS = {
    sun: SUN_CORE,
    moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.6 6.6 0 0 0 10.5 10.5z"/>',
    "sun-cloud": SMALL_SUN + '<path d="' + LOW_CLOUD + '"/>',
    "moon-cloud": SMALL_MOON + '<path d="' + LOW_CLOUD + '"/>',
    cloud: '<path d="' + CLOUD + '"/>',
    fog: '<path d="M17.5 13.5h-11a3.6 3.6 0 0 1-.55-7.16 5.5 5.5 0 0 1 10.85-1.4A4.1 4.1 0 0 1 17.5 13.5z"/>' +
      '<path d="M5.5 17h13M7.5 20.5h9"/>',
    drizzle: '<path d="' + CLOUD + '"/><path class="ic-rain" d="M8.5 21v.2M12 21.5v.2M15.5 21v.2"/>',
    rain: '<path d="' + CLOUD + '"/><path class="ic-rain" d="M9 20.5l-.7 1.7M12.5 20.5l-.7 1.7M16 20.5l-.7 1.7"/>',
    sleet: '<path d="' + CLOUD + '"/><path class="ic-rain" d="M9 20.5l-.6 1.5M15.8 20.5l-.6 1.5"/>' +
      '<path class="ic-rain" d="M12.4 21.2h.2"/>',
    snow: '<path d="' + CLOUD + '"/><path class="ic-rain" d="M8.7 20.9h.2M12.2 22h.2M15.7 20.9h.2"/>',
    storm: '<path d="' + CLOUD + '"/><path class="ic-sun" d="M12.5 19.5l-1.8 2.6h2.6l-1.8 2.6"/>'
  };

  function iconSvg(name, cls) {
    var body = ICONS[name] || ICONS.cloud;
    return '<svg class="' + cls + '" viewBox="0 0 24 26" fill="none" stroke="currentColor"' +
      ' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      body + "</svg>";
  }

  // ---- small formatters ----

  var COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  function compass(deg) {
    if (typeof deg !== "number" || !isFinite(deg)) return "";
    return COMPASS[Math.round(deg / 22.5) % 16];
  }

  function uvWord(uv) {
    if (uv == null) return "";
    if (uv < 3) return "low";
    if (uv < 6) return "moderate";
    if (uv < 8) return "high";
    if (uv < 11) return "very high";
    return "extreme";
  }

  function rnd(v) { return (typeof v === "number" && isFinite(v)) ? Math.round(v) : null; }

  /* Day's rainfall total for the DAILY rows ("0.32 in"). Empty when the
     total rounds to 0.00 so dry weeks stay quiet: no zeros, no clutter. */
  function fmtRainIn(v) {
    if (typeof v !== "number" || !isFinite(v) || v <= 0) return "";
    var s = v.toFixed(2);
    return s === "0.00" ? "" : s + " in";
  }
  function esc(s) { return C.esc(s == null ? "" : s); }

  function fmtClock(iso) {
    if (!iso) return "?";
    var d = new Date(iso);
    if (isNaN(d)) return "?";
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  function relTime(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var mins = Math.round((Date.now() - t) / 60000);
    if (mins < 1) return "just now";
    if (mins < 90) return mins + " min ago";
    var hrs = Math.round(mins / 60);
    if (hrs < 36) return hrs + " h ago";
    return Math.round(hrs / 24) + " d ago";
  }

  // ---- state ----

  var state = {
    loc: null,
    defaultLoc: null,
    locations: [],
    data: {},          // key -> {ok, status, body} per endpoint
    lastLoad: 0
  };

  var $ = function (id) { return document.getElementById(id); };

  function fetchJSON(path) {
    return fetch(path, { headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (body) {
          return { ok: r.ok, status: r.status, body: body };
        });
      })
      .catch(function () { return { ok: false, status: 0, body: null }; });
  }

  // ---- shared render helpers ----

  function updatedLine(el, payloads) {
    // payloads: list of envelope payloads backing the view; show the oldest,
    // go amber when any is stale.
    var list = payloads.filter(function (p) { return p && p.fetched_at; });
    if (!list.length) { el.textContent = ""; el.classList.remove("stale"); return; }
    var oldest = list.reduce(function (a, b) {
      return Date.parse(a.fetched_at) <= Date.parse(b.fetched_at) ? a : b;
    });
    var stale = list.some(function (p) { return p.stale; });
    el.textContent = "Updated " + relTime(oldest.fetched_at) +
      (stale ? " · showing last known data" : "");
    el.classList.toggle("stale", stale);
  }

  function emptyState(res) {
    if (res && res.status === 503) {
      var msg = res.body && res.body.error && res.body.error.indexOf("first successful poll") >= 0
        ? "First poll pending. Data lands within a minute of startup."
        : "No data yet for this location.";
      if (state.loc !== state.defaultLoc) {
        msg = "No data yet for this location. Aeolus polls the default " +
          "location; other saved places fill in when their polling starts.";
      }
      return '<p class="empty-state"><strong>' + esc(msg) + "</strong></p>";
    }
    return '<p class="empty-state"><strong>Aeolus is unreachable.</strong> ' +
      "No saved data for this view yet; it will fill in when a connection returns.</p>";
  }

  var SEV_RANK = { Extreme: 4, Severe: 3, Moderate: 2, Minor: 1 };
  function sevClass(sev) {
    if (sev === "Extreme") return "sev-extreme";
    if (sev === "Severe") return "sev-severe";
    if (sev === "Moderate") return "sev-moderate";
    return "sev-minor";
  }
  function sortedAlerts(items) {
    return items.slice().sort(function (a, b) {
      return (SEV_RANK[b.severity] || 0) - (SEV_RANK[a.severity] || 0);
    });
  }

  // ---- burn window chip (NOW) ----

  var BURN_LABEL = { go: "GO", caution: "CAUTION", no_burn: "NO BURN" };
  var BURN_RANK = { go: 0, caution: 1, no_burn: 2 };
  var burnOpen = false; // expanded state survives re-renders

  function burnHourWord(ts) {
    var d = new Date(ts);
    if (isNaN(d)) return "";
    return C.hourLabel(d).replace("a", " AM").replace("p", " PM");
  }

  function burnHint(b) {
    // "winds rise to 18 mph at 2 PM" from changes_at + the flip's first
    // reason; generic phrasing when the reason is not a single factor.
    if (!b.changes_at || !b.next_verdict) return "";
    var t = burnHourWord(b.changes_at);
    var worse = (BURN_RANK[b.next_verdict] || 0) > (BURN_RANK[b.verdict] || 0);
    var r = (b.next_reasons && b.next_reasons[0]) || "";
    var m;
    if (worse && r) {
      if ((m = r.match(/^wind (\d+) mph$/))) return "Winds rise to " + m[1] + " mph at " + t + ".";
      if ((m = r.match(/^gusts (\d+) mph$/))) return "Gusts reach " + m[1] + " mph at " + t + ".";
      if ((m = r.match(/^humidity (\d+)%$/))) return "Humidity drops to " + m[1] + "% at " + t + ".";
      return (BURN_LABEL[b.next_verdict] || b.next_verdict) + " at " + t + ": " + r + ".";
    }
    return "Improves to " + (BURN_LABEL[b.next_verdict] || b.next_verdict) + " at " + t + ".";
  }

  function renderBurn() {
    var el = $("nowBurn");
    var res = state.data.burn;
    var b = res && res.ok && res.body;
    // default-location-only feature: the burn window does not follow the picker
    if (!b || !b.verdict || state.loc !== state.defaultLoc) {
      el.innerHTML = "";
      return;
    }
    var reasons = (b.reasons && b.reasons.length ? b.reasons : null);
    var items = (reasons || ["Wind, gusts and humidity are all in the safe range."])
      .map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("");
    var hint = burnHint(b);
    el.innerHTML =
      '<button type="button" id="burnChip" class="burn-chip burn-' + esc(b.verdict) + '"' +
      ' aria-expanded="' + (burnOpen ? "true" : "false") + '" aria-controls="burnDetail">' +
      '<span class="burn-dot" aria-hidden="true"></span>' +
      "BURN: " + esc(BURN_LABEL[b.verdict] || b.verdict) +
      (b.stale ? '<span class="burn-stale-tag">stale</span>' : "") +
      "</button>" +
      '<div id="burnDetail" class="burn-detail"' + (burnOpen ? "" : " hidden") + ">" +
      '<ul class="burn-reasons">' + items + "</ul>" +
      (hint ? '<p class="burn-hint">' + esc(hint) + "</p>" : "") +
      '<p class="burn-disclaimer">Guidance only. Check county burn bans.</p>' +
      "</div>";
    $("burnChip").onclick = function () {
      burnOpen = !burnOpen;
      this.setAttribute("aria-expanded", burnOpen ? "true" : "false");
      $("burnDetail").hidden = !burnOpen;
    };
  }

  // ---- NOW ----

  function renderNow() {
    var cur = state.data.current, nc = state.data.nowcast, al = state.data.alerts;

    // alert banner: highest severity active alert, tap through to details
    var bannerEl = $("nowAlertBanner");
    var items = (al && al.ok && al.body && al.body.alerts) || [];
    if (items.length) {
      var top = sortedAlerts(items)[0];
      var where = top.affects_point ? "Our area" : (top.area_desc || "Nearby");
      var until = top.ends || top.expires;
      bannerEl.innerHTML = '<a class="alert-banner ' + sevClass(top.severity) + '" href="#/alerts">' +
        '<div class="ab-event">' + esc(top.event) + "</div>" +
        '<div class="ab-meta">' + esc(where) + (until ? " · until " + esc(fmtClock(until)) : "") +
        (items.length > 1 ? " · " + (items.length - 1) + " more" : "") + "</div></a>";
    } else {
      bannerEl.innerHTML = "";
    }
    $("alertDot").hidden = !items.length;

    renderBurn();

    // hero tile
    var heroEl = $("nowHero");
    if (!cur || !cur.ok || !cur.body || !cur.body.current) {
      heroEl.innerHTML = emptyState(cur);
    } else {
      var c = cur.body.current;
      var cond = condition(c, c.is_day !== 0);
      var t = rnd(c.temp_f != null ? c.temp_f : c.temp); // c.temp: raw station obs
      var feels = rnd(c.feels_like_f);
      var gust = rnd(c.gusts_mph);
      var windTxt = c.wind_mph != null
        ? compass(c.wind_dir_deg) + " " + rnd(c.wind_mph) + " mph" : "n/a";
      heroEl.innerHTML =
        '<div class="hero-top"><div>' +
        '<div class="hero-temp">' + (t == null ? "--" : t) + "<sup>&#176;</sup></div>" +
        '<div class="hero-cond">' + esc(cond.label) + "</div>" +
        (feels != null ? '<div class="hero-feels">Feels like ' + feels + "&#176;</div>" : "") +
        "</div>" + iconSvg(cond.icon, "hero-icon") + "</div>" +
        '<div class="hero-stats">' +
        '<div class="stat"><div class="stat-label">Wind</div>' +
        '<div class="stat-value">' + esc(windTxt) + "</div>" +
        (gust != null ? '<div class="stat-sub">gusts ' + gust + " mph</div>" : "") + "</div>" +
        '<div class="stat"><div class="stat-label">Humidity</div>' +
        '<div class="stat-value">' + (rnd(c.humidity_pct) != null ? rnd(c.humidity_pct) + "%" : "n/a") + "</div>" +
        (c.dew_point_f != null ? '<div class="stat-sub">dew point ' + rnd(c.dew_point_f) + "&#176;</div>" : "") + "</div>" +
        '<div class="stat"><div class="stat-label">UV index</div>' +
        '<div class="stat-value">' + (c.uv_index != null ? Math.round(c.uv_index * 10) / 10 : "n/a") + "</div>" +
        '<div class="stat-sub">' + esc(uvWord(c.uv_index)) + "</div></div>" +
        "</div>";
    }

    // nowcast strip: the next hour, 15-minute buckets
    var stripEl = $("nowcastStrip"), sumEl = $("nowcastSummary");
    if (!nc || !nc.ok || !nc.body || !nc.body.nowcast || !nc.body.nowcast.length) {
      stripEl.innerHTML = emptyState(nc);
      sumEl.textContent = "";
    } else {
      var rows = nc.body.nowcast.slice(0, 4);
      stripEl.innerHTML = C.nowcastStrip(rows);
      var firstWet = -1, snow = false;
      rows.forEach(function (r, i) {
        if (firstWet < 0 && typeof r.precip_in === "number" && r.precip_in > 0) firstWet = i;
        if (typeof r.snow_in === "number" && r.snow_in > 0) snow = true;
      });
      var word = snow ? "Snow" : "Rain";
      if (firstWet < 0) sumEl.textContent = "No precipitation expected in the next hour.";
      else if (firstWet === 0) sumEl.textContent = word + " now.";
      else sumEl.textContent = word + " around " +
        C.quarterLabel(new Date(rows[firstWet].ts)).replace("a", " AM").replace("p", " PM") + ".";
    }

    var br = state.data.burn;
    updatedLine($("updated-now"), [cur && cur.body, nc && nc.body, br && br.body]);
  }

  // ---- HOURLY ----

  function renderHourly() {
    var res = state.data.hourly;
    var wrap = $("hourlyChartWrap"), tableEl = $("hourlyTable");
    if (!res || !res.ok || !res.body || !res.body.hourly || !res.body.hourly.length) {
      wrap.innerHTML = emptyState(res);
      tableEl.innerHTML = "";
    } else {
      var rows = res.body.hourly;
      wrap.innerHTML = C.hourlyChart(rows) +
        '<div class="chart-cursor" id="hourlyCursor"></div>' +
        '<div class="chart-tip" id="hourlyTip"></div>';
      bindHourlyTooltip(wrap, rows);
      // open by default; a re-render keeps whatever the user last chose
      var prev = tableEl.querySelector("details");
      tableEl.innerHTML = hourlyTable(rows, prev ? prev.open : true);
    }
    updatedLine($("updated-hourly"), [res && res.body]);
  }

  function hourlyTable(rows, open) {
    var out = ['<details class="table-view"' + (open ? " open" : "") + ">" +
      "<summary>View as table</summary><table>",
      "<tr><th>Time</th><th>Temp</th><th>Feels</th><th>Chance</th><th>Precip</th><th>Wind</th></tr>"];
    rows.forEach(function (r) {
      var d = new Date(r.ts);
      // temp, chance and wind cells wear quiet data bars: fill width = the
      // value on the chart's scales (temp 0..110, chance 0..100, wind
      // 0..40 mph); the temp tint picks the line gradient's nearest anchor
      // (bucket cuts are the midpoints between anchors)
      var p = r.precip_prob_pct;
      var bar = typeof p === "number" && p > 0
        ? ' style="background-size:' + Math.min(p, 100) + '% 100%"' : "";
      var w = r.wind_mph;
      var wbar = typeof w === "number" && w > 0
        ? ' style="background-size:' + Math.min(100, Math.round(w / 40 * 100)) + '% 100%"' : "";
      var t = r.temp_f, tcell = ' class="temp-cell"';
      if (typeof t === "number") {
        var tw = Math.max(0, Math.min(100, Math.round(t / 110 * 100)));
        tcell = ' class="temp-cell ' + (t >= 102.5 ? "t-hot2" : t >= 87.5 ? "t-hot" :
          t >= 70 ? "t-warm" : t >= 50 ? "t-mild" : t >= 15 ? "t-cool" : "t-cold") + '"' +
          (tw > 0 ? ' style="background-size:' + tw + '% 100%"' : "");
      }
      out.push("<tr><td>" + esc(C.dayLabel(d) + " " + C.hourLabel(d)) + "</td>" +
        "<td" + tcell + ">" + (rnd(r.temp_f) != null ? rnd(r.temp_f) + "°" : "") + "</td>" +
        "<td>" + (rnd(r.feels_like_f) != null ? rnd(r.feels_like_f) + "°" : "") + "</td>" +
        '<td class="precip-cell"' + bar + ">" + (p != null ? p + "%" : "") + "</td>" +
        "<td>" + (typeof r.precip_in === "number" && r.precip_in > 0 ? r.precip_in.toFixed(2) + " in" : "") + "</td>" +
        '<td class="wind-cell"' + wbar + ">" + (rnd(w) != null ? rnd(w) + " mph" : "") + "</td></tr>");
    });
    out.push("</table></details>");
    return out.join("");
  }

  function bindHourlyTooltip(wrap, rows) {
    var tip = $("hourlyTip"), cursor = $("hourlyCursor");
    var svg = wrap.querySelector("svg");
    if (!svg) return;
    cursor.style.height = "182px";

    function show(clientX) {
      var rect = svg.getBoundingClientRect();
      var x = clientX - rect.left;
      var i = Math.floor((x - C.PAD_L) / C.STEP);
      if (i < 0) i = 0;
      if (i >= rows.length) i = rows.length - 1;
      var r = rows[i], d = new Date(r.ts);
      var cx = C.PAD_L + i * C.STEP + C.STEP / 2;
      cursor.style.left = cx + "px";
      cursor.style.display = "block";
      tip.innerHTML = '<span class="tt-t">' + esc(C.dayLabel(d) + " " + C.hourLabel(d)) + "</span> · " +
        (rnd(r.temp_f) != null ? rnd(r.temp_f) + "°" : "") +
        (rnd(r.feels_like_f) != null ? " feels " + rnd(r.feels_like_f) + "°" : "") +
        (r.precip_prob_pct != null ? " · " + r.precip_prob_pct + "%" : "") +
        (rnd(r.wind_mph) != null ? " · " + compass(r.wind_dir_deg) + " " + rnd(r.wind_mph) + " mph" : "") +
        (rnd(r.gusts_mph) != null ? ", gusts " + rnd(r.gusts_mph) : "");
      tip.style.display = "block";
      var tw = tip.offsetWidth;
      var visX = cx - wrap.scrollLeft;
      var clamped = Math.max(6, Math.min(visX - tw / 2, wrap.clientWidth - tw - 6));
      tip.style.left = (wrap.scrollLeft + clamped) + "px";
      tip.style.top = "10px";
    }

    wrap.addEventListener("pointermove", function (e) { show(e.clientX); });
    wrap.addEventListener("pointerdown", function (e) { show(e.clientX); });
    wrap.addEventListener("pointerleave", function () {
      tip.style.display = "none";
      cursor.style.display = "none";
    });
  }

  // ---- DAILY ----

  var burnDayOpen = null; // date string of the expanded outlook chip

  function burnDayDetail(bd, open, stale) {
    var items = (bd.reasons && bd.reasons.length ? bd.reasons
      : ["Wind, gusts and humidity stay in the safe range through the burn window."])
      .map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("");
    return '<div class="burn-day-detail"' + (open ? "" : " hidden") + ">" +
      '<ul class="burn-reasons">' + items + "</ul>" +
      (bd.partial ? '<p class="burn-note">Partial forecast coverage for this day\'s burn window.</p>' : "") +
      (bd.low_confidence ? '<p class="burn-note">Lower confidence this far out.</p>' : "") +
      (stale ? '<p class="burn-note stale-note">Showing last known data.</p>' : "") +
      '<p class="burn-disclaimer">Guidance only. Check county burn bans.</p>' +
      "</div>";
  }

  function dailyPayloads() {
    // payloads backing the daily view; the outlook only backs the default location
    var arr = [state.data.daily && state.data.daily.body];
    if (state.loc === state.defaultLoc && state.data.burnOutlook) {
      arr.push(state.data.burnOutlook.body);
    }
    return arr;
  }

  function renderDaily() {
    var res = state.data.daily;
    var listEl = $("dailyList");
    if (!res || !res.ok || !res.body || !res.body.daily || !res.body.daily.length) {
      listEl.innerHTML = emptyState(res);
    } else {
      var rows = res.body.daily;
      var lows = [], highs = [];
      rows.forEach(function (r) {
        if (typeof r.temp_min_f === "number") lows.push(r.temp_min_f);
        if (typeof r.temp_max_f === "number") highs.push(r.temp_max_f);
      });
      var min = Math.min.apply(null, lows), max = Math.max.apply(null, highs);

      // burn outlook chips: default location only, keyed by date (r.ts "YYYY-MM-DD")
      var ol = state.data.burnOutlook;
      var outlook = {};
      var showBurn = state.loc === state.defaultLoc &&
        ol && ol.ok && ol.body && ol.body.days && ol.body.days.length;
      if (showBurn) {
        ol.body.days.forEach(function (bd) { outlook[bd.date] = bd; });
      }
      listEl.classList.toggle("has-burn", !!showBurn);

      var out = [];
      rows.forEach(function (r, i) {
        if (i === 7) {
          out.push('<div class="confidence-divider" role="note">Lower confidence</div>');
        }
        var d = new Date(r.ts + "T12:00");
        var name = i === 0 ? "Today" : C.dayLabel(d);
        var date = d.toLocaleString([], { month: "short", day: "numeric" });
        var cond = condition(r, true);
        var p = r.precip_prob_max_pct;
        var rain = fmtRainIn(r.precip_sum_in);
        var lo = rnd(r.temp_min_f), hi = rnd(r.temp_max_f);
        var bd = showBurn ? outlook[r.ts] : null;
        var open = bd && burnDayOpen === bd.date;
        out.push('<div class="day-row' + (i >= 7 ? " low-confidence" : "") + '"' +
          (i >= 7 ? ' title="Days 8-10: forecast skill drops; treat as a trend."' : "") + ">" +
          '<div class="day-name">' + esc(name) + '<span class="day-date">' + esc(date) + "</span></div>" +
          iconSvg(cond.icon, "day-icon") +
          '<div class="day-precip' + (p == null || p < 15 ? " dry" : "") + '">' +
          (p != null ? p + "%" : "") +
          (rain ? '<span class="day-rain">' + rain + "</span>" : "") + "</div>" +
          '<div class="day-lo">' + (lo != null ? lo + "°" : "") + "</div>" +
          C.dailyRangeBar(r.temp_min_f, r.temp_max_f, min, max) +
          '<div class="day-hi">' + (hi != null ? hi + "°" : "") + "</div>" +
          (bd ? '<button type="button" class="burn-day-chip burn-' + esc(bd.verdict) +
            (bd.low_confidence ? " low-conf" : "") + '" data-date="' + esc(bd.date) + '"' +
            ' aria-expanded="' + (open ? "true" : "false") + '">' +
            esc(BURN_LABEL[bd.verdict] || bd.verdict) + (bd.partial ? "*" : "") +
            "</button>" : (showBurn ? "<span></span>" : "")) +
          "</div>");
        if (bd) out.push(burnDayDetail(bd, open, ol.body.stale));
      });
      listEl.innerHTML = out.join("");
      var chips = listEl.querySelectorAll(".burn-day-chip");
      for (var ci = 0; ci < chips.length; ci++) {
        chips[ci].onclick = function () {
          var dd = this.getAttribute("data-date");
          burnDayOpen = burnDayOpen === dd ? null : dd;
          renderDaily();
        };
      }
    }
    updatedLine($("updated-daily"), dailyPayloads());
  }

  // ---- ALERTS ----

  function renderAlerts() {
    var res = state.data.alerts;
    var listEl = $("alertsList");
    if (!res || !res.body) {
      listEl.innerHTML = emptyState(res);
    } else if (!res.body.alerts || !res.body.alerts.length) {
      listEl.innerHTML = '<p class="empty-state"><strong>No active alerts.</strong> ' +
        "Watches, warnings and advisories for our county appear here.</p>";
    } else {
      listEl.innerHTML = sortedAlerts(res.body.alerts).map(function (a) {
        var where = a.affects_point ? "Our area" : (a.area_desc || "Nearby");
        var headline = a.nws_headline || a.headline || "";
        return '<article class="alert-card ' + sevClass(a.severity) + '">' +
          '<div class="alert-head"><h3 class="alert-event">' + esc(a.event) + "</h3>" +
          '<span class="area-chip' + (a.affects_point ? " here" : "") + '">' + esc(where) + "</span></div>" +
          (headline ? '<p class="alert-headline">' + esc(headline) + "</p>" : "") +
          '<p class="alert-window">' + esc(fmtClock(a.onset)) + " → " +
          esc(fmtClock(a.ends || a.expires)) + "</p>" +
          (a.instruction ? '<p class="alert-instruction">' + esc(a.instruction) + "</p>" : "") +
          "</article>";
      }).join("");
    }
    $("alertDot").hidden = !(res && res.body && res.body.alerts && res.body.alerts.length);
    updatedLine($("updated-alerts"), [res && res.body]);
  }

  // ---- routing ----

  var VIEWS = ["now", "hourly", "daily", "radar", "alerts"];

  function activeView() {
    var m = (location.hash || "").match(/^#\/(\w+)/);
    return m && VIEWS.indexOf(m[1]) >= 0 ? m[1] : "now";
  }

  function showView() {
    var v = activeView();
    VIEWS.forEach(function (name) {
      $("view-" + name).hidden = name !== v;
      var tab = $("tab-" + name);
      if (name === v) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    });
    // The radar view manages its own map, loop and refresh cadence; tell it
    // when it gains or loses the screen (lazy: no map until first open).
    var R = window.AeolusRadar;
    if (R) { if (v === "radar") R.enter(); else R.leave(); }
  }

  function renderAll() {
    renderNow();
    renderHourly();
    renderDaily();
    renderAlerts();
  }

  // ---- data loading ----

  function loadAll() {
    state.lastLoad = Date.now();
    var loc = state.loc != null ? "?loc=" + state.loc : "";
    fetchJSON("/v1/current" + loc).then(function (r) { state.data.current = r; renderNow(); });
    fetchJSON("/v1/burn").then(function (r) { state.data.burn = r; renderNow(); });
    fetchJSON("/v1/burn/outlook").then(function (r) { state.data.burnOutlook = r; renderDaily(); });
    fetchJSON("/v1/nowcast" + loc).then(function (r) { state.data.nowcast = r; renderNow(); });
    fetchJSON("/v1/hourly" + loc).then(function (r) { state.data.hourly = r; renderHourly(); });
    fetchJSON("/v1/daily" + loc).then(function (r) { state.data.daily = r; renderDaily(); });
    fetchJSON("/v1/alerts").then(function (r) {
      state.data.alerts = r;
      renderAlerts();
      renderNow(); // banner + dot
    });
  }

  function loadLocations() {
    return fetchJSON("/v1/locations").then(function (r) {
      var locs = (r.ok && r.body && r.body.locations) || [];
      state.locations = locs;
      var def = locs.filter(function (l) { return l.is_default; })[0] || locs[0];
      state.defaultLoc = def ? def.id : null;
      // Center the radar map on the configured default location (the map is
      // lazy, so this runs well before the radar tab is first opened).
      if (def && window.AeolusRadar && window.AeolusRadar.setLocation) {
        window.AeolusRadar.setLocation(def.lat, def.lon, def.name);
      }
      var saved = parseInt(localStorage.getItem("aeolus.loc"), 10);
      var savedOk = locs.some(function (l) { return l.id === saved; });
      state.loc = savedOk ? saved : state.defaultLoc;

      var sel = $("locSelect");
      sel.innerHTML = locs.map(function (l) {
        return '<option value="' + l.id + '"' + (l.id === state.loc ? " selected" : "") + ">" +
          esc(l.name) + "</option>";
      }).join("") || "<option>Home</option>";
      sel.onchange = function () {
        state.loc = parseInt(sel.value, 10);
        localStorage.setItem("aeolus.loc", String(state.loc));
        state.data.current = state.data.hourly = state.data.daily = state.data.nowcast = null;
        renderAll();
        loadAll();
      };
    });
  }

  // ---- boot ----

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () { /* offline-first is best-effort */ });
  }

  window.addEventListener("hashchange", showView);
  showView();

  loadLocations().then(loadAll);

  // keep "updated N min ago" honest without refetching
  setInterval(function () {
    updatedLine($("updated-now"), [state.data.current && state.data.current.body,
      state.data.nowcast && state.data.nowcast.body,
      state.data.burn && state.data.burn.body]);
    updatedLine($("updated-hourly"), [state.data.hourly && state.data.hourly.body]);
    updatedLine($("updated-daily"), dailyPayloads());
    updatedLine($("updated-alerts"), [state.data.alerts && state.data.alerts.body]);
  }, 30000);

  // refetch on a cadence and when the app returns to the foreground
  setInterval(loadAll, 5 * 60 * 1000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && Date.now() - state.lastLoad > 60000) loadAll();
  });
})();
