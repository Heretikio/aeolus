/* Aeolus chart builders. Pure functions: rows in, SVG markup string out.
   No DOM access, so the same file runs in the browser (window.AeolusCharts)
   and under node for verification. Marks carry CSS classes and take their
   colors from app.css tokens, so themes restyle charts without a re-render. */

(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.AeolusCharts = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Hourly chart geometry, shared with the tooltip math in app.js
  var STEP = 34;        // px per hour
  var PAD_L = 10;
  var PAD_R = 16;
  var PLOT_TOP = 20;    // one shared plot: bars layered under the temp line
  var PLOT_H = 170;
  var PLOT_BASE = PLOT_TOP + PLOT_H;
  var TEMP_LO = 0;      // fixed axis (F): the baseline sits at 0 and never
  var TEMP_HI = 110;    //   rescales; subzero hours dip below the baseline
  // temperature gradient anchors: degrees F -> stop class; the colors live
  // in app.css tokens so a theme flip restyles the line without a re-render
  var GRAD_STOPS = [[110, "g-hot2"], [95, "g-hot"], [80, "g-warm"],
    [60, "g-mild"], [40, "g-cool"], [-10, "g-cold"]];
  var WIND_HI = 40;     // fixed wind scale (mph): 0 at baseline, clamped at 40
  // wind gradient anchors: mph -> stop class, burn-safety colors; green is
  // calm enough to burn, yellow is the open-burn no-go line, red is red-flag
  var WIND_STOPS = [[40, "w-danger2"], [25, "w-danger"], [20, "w-warn"],
    [14, "w-caution"], [8, "w-safe"]];
  var CHART_H = 230;
  var BAR_W = 22;       // thin marks; the band's leftover stays air

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function num(v) { return typeof v === "number" && isFinite(v); }

  // "2026-07-03T14:00" parses as local time, which is the service timezone
  function parseTs(ts) { return new Date(ts); }

  function hourLabel(d) {
    var h = d.getHours();
    var ap = h < 12 ? "a" : "p";
    var h12 = h % 12; if (h12 === 0) h12 = 12;
    return h12 + ap;
  }

  function quarterLabel(d) {
    var h = d.getHours();
    var ap = h < 12 ? "a" : "p";
    var h12 = h % 12; if (h12 === 0) h12 = 12;
    var m = d.getMinutes();
    return h12 + ":" + (m < 10 ? "0" : "") + m + ap;
  }

  var DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function dayLabel(d) { return DAYS[d.getDay()]; }

  // Bar with a 3px rounded data-end and a square baseline
  function topRoundedBar(x, yTop, w, h, base, cls) {
    if (h <= 0.5) return "";
    var r = Math.min(3, h / 2, w / 2);
    var d = "M" + x + " " + base +
      "V" + (yTop + r) +
      "Q" + x + " " + yTop + " " + (x + r) + " " + yTop +
      "H" + (x + w - r) +
      "Q" + (x + w) + " " + yTop + " " + (x + w) + " " + (yTop + r) +
      "V" + base + "Z";
    return '<path class="' + cls + '" d="' + d + '"/>';
  }

  // 48h chart: one plot, precip-probability bars (own 0..100 scale, 100%
  // fills the plot) and the wind layer (own 0..40 mph scale) layered under
  // the temperature line; day separators, sparse labels.
  function hourlyChart(rows) {
    if (!rows || !rows.length) return "";
    var n = rows.length;
    var W = PAD_L + PAD_R + n * STEP;

    if (!rows.some(function (r) { return num(r.temp_f); })) return "";
    var yT = function (t) { return PLOT_TOP + (TEMP_HI - t) / (TEMP_HI - TEMP_LO) * PLOT_H; };
    var yW = function (w) { return PLOT_BASE - Math.min(w, WIND_HI) / WIND_HI * PLOT_H; };
    var xC = function (i) { return PAD_L + i * STEP + STEP / 2; };

    var parts = [];
    parts.push('<svg xmlns="http://www.w3.org/2000/svg" class="chart" width="' + W +
      '" height="' + CHART_H + '" viewBox="0 0 ' + W + " " + CHART_H +
      '" role="img" aria-label="48 hour forecast: temperature line, chance of' +
      ' precipitation bars, wind line with gust band">');

    // line color encodes temperature: userSpaceOnUse pins the gradient to
    // the fixed axis, so the stroke at any height wears that height's color;
    // it runs past the baseline to the coldest anchor so a subzero dip
    // keeps its true color
    var gCold = GRAD_STOPS[GRAD_STOPS.length - 1][0];
    var stops = GRAD_STOPS.map(function (s) {
      var off = (TEMP_HI - s[0]) / (TEMP_HI - gCold) * 100;
      return '<stop class="' + s[1] + '" offset="' + off.toFixed(1) + '%"/>';
    }).join("");
    // the wind line's gradient works the same way on its own 0..40 scale:
    // the color at any height says whether that wind is burn-safe
    var wstops = WIND_STOPS.map(function (s) {
      var off = (WIND_HI - s[0]) / WIND_HI * 100;
      return '<stop class="' + s[1] + '" offset="' + off.toFixed(1) + '%"/>';
    }).join("");
    parts.push('<defs><linearGradient id="temp-grad" gradientUnits="userSpaceOnUse"' +
      ' x1="0" y1="' + PLOT_TOP + '" x2="0" y2="' + yT(gCold).toFixed(1) + '">' +
      stops + "</linearGradient>" +
      '<linearGradient id="wind-grad" gradientUnits="userSpaceOnUse"' +
      ' x1="0" y1="' + PLOT_TOP + '" x2="0" y2="' + PLOT_BASE + '">' +
      wstops + "</linearGradient></defs>");

    // lane titles, quiet and small, sharing the top line
    parts.push('<text class="c-lane" x="' + PAD_L + '" y="12">TEMP &#176;F</text>');
    parts.push('<text class="c-lane" x="' + (PAD_L + 62) +
      '" y="12">&#183; CHANCE OF PRECIP &#183; WIND</text>');

    // fixed-step gridlines, hairline, labeled once at the left
    for (var g = 0; g <= 100; g += 20) {
      var gy = yT(g);
      parts.push('<line class="c-grid" x1="' + PAD_L + '" y1="' + gy.toFixed(1) +
        '" x2="' + (W - PAD_R) + '" y2="' + gy.toFixed(1) + '"/>');
      parts.push('<text x="' + (PAD_L + 2) + '" y="' + (gy - 3).toFixed(1) + '">' + g + "&#176;</text>");
    }

    // day separators + labels, precip baseline
    var prevDay = null;
    rows.forEach(function (r, i) {
      var d = parseTs(r.ts);
      var key = d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate();
      if (prevDay !== null && key !== prevDay) {
        var xb = PAD_L + i * STEP;
        parts.push('<line class="c-day" x1="' + xb + '" y1="' + PLOT_TOP + '" x2="' + xb +
          '" y2="' + PLOT_BASE + '"/>');
        parts.push('<text class="c-daylbl" x="' + (xb + 5) + '" y="' + (PLOT_BASE + 34) + '">' +
          dayLabel(d) + "</text>");
      }
      prevDay = key;
    });
    parts.push('<line class="c-base" x1="' + PAD_L + '" y1="' + PLOT_BASE + '" x2="' +
      (W - PAD_R) + '" y2="' + PLOT_BASE + '"/>');

    // precip-probability bars, drawn under the temp line: height is the
    // probability (100% fills the plot), same quiet tint as the table's bars
    rows.forEach(function (r, i) {
      var p = r.precip_prob_pct;
      if (!num(p) || p <= 0) return;
      var h = p / 100 * PLOT_H;
      parts.push(topRoundedBar(xC(i) - BAR_W / 2, PLOT_BASE - h, BAR_W, h, PLOT_BASE, "c-rainbar"));
    });

    // gust envelope: a faint band from sustained wind up to the gusts (same
    // 0..40 clamp), quieter than the precip bars; gusts carry embers
    var band = "", run = [];
    function flushBand() {
      if (run.length > 1) {
        var bd = "";
        run.forEach(function (pt, j) { bd += (j ? "L" : "M") + pt[0] + " " + pt[2]; });
        for (var j = run.length - 1; j >= 0; j--) bd += "L" + run[j][0] + " " + run[j][1];
        band += '<path class="c-gustband" d="' + bd + 'Z"/>';
      }
      run = [];
    }
    rows.forEach(function (r, i) {
      if (!num(r.wind_mph) || !num(r.gusts_mph)) { flushBand(); return; }
      run.push([xC(i).toFixed(1), yW(r.wind_mph).toFixed(1), yW(r.gusts_mph).toFixed(1)]);
    });
    flushBand();
    if (band) parts.push(band);

    // sustained wind: a thin line, broken at gaps; height and color are the
    // burn-safety read, exact numbers live in the table below
    var wd = "", wpen = false;
    rows.forEach(function (r, i) {
      if (!num(r.wind_mph)) { wpen = false; return; }
      wd += (wpen ? "L" : "M") + xC(i).toFixed(1) + " " + yW(r.wind_mph).toFixed(1);
      wpen = true;
    });
    if (wd) parts.push('<path class="c-wind" stroke="url(#wind-grad)" d="' + wd + '"/>');

    // temperature line, broken at gaps
    var d = "", pen = false;
    rows.forEach(function (r, i) {
      if (!num(r.temp_f)) { pen = false; return; }
      var pt = xC(i).toFixed(1) + " " + yT(r.temp_f).toFixed(1);
      d += (pen ? "L" : "M") + pt;
      pen = true;
    });
    parts.push('<path class="c-temp" stroke="url(#temp-grad)" d="' + d + '"/>');

    // sparse x labels every 3 hours
    rows.forEach(function (r, i) {
      if (i % 3 !== 0) return;
      var dd = parseTs(r.ts);
      parts.push('<text x="' + xC(i) + '" y="' + (PLOT_BASE + 18) +
        '" text-anchor="middle">' + hourLabel(dd) + "</text>");
    });

    // selective direct labels: now (dot + bold value, set right of the dot so
    // it clears the left-edge gridline labels), then every 6th hour
    rows.forEach(function (r, i) {
      if (!num(r.temp_f)) return;
      if (i !== 0 && i % 6 !== 0) return;
      var cx = xC(i), cy = yT(r.temp_f);
      var label = Math.round(r.temp_f) + "&#176;";
      if (i === 0) {
        parts.push('<text class="c-vlabel-now" x="' + (cx + 9) + '" y="' + (cy + 4).toFixed(1) +
          '">' + label + "</text>");
        return;
      }
      var ly = cy < 34 ? cy + 18 : cy - 9;
      parts.push('<text class="c-vlabel" x="' + cx + '" y="' + ly.toFixed(1) +
        '" text-anchor="middle">' + label + "</text>");
    });
    var nowRow = rows[0];
    if (num(nowRow.temp_f)) {
      parts.push('<circle class="c-temp-dot" cx="' + xC(0) + '" cy="' +
        yT(nowRow.temp_f).toFixed(1) + '" r="4.5"/>');
    }

    parts.push("</svg>");
    return parts.join("");
  }

  // Next-hour nowcast strip: four 15-minute buckets, single blue
  function nowcastStrip(rows) {
    if (!rows) rows = [];
    var buckets = rows.slice(0, 4);
    var W = 340, H = 96, BASE = 64, TOPMAX = 40, SLOT = W / 4, BW = 24;
    var maxP = 0.1;
    buckets.forEach(function (r) {
      if (num(r.precip_in)) maxP = Math.max(maxP, r.precip_in);
    });

    var parts = [];
    parts.push('<svg xmlns="http://www.w3.org/2000/svg" class="chart" viewBox="0 0 ' + W + " " + H +
      '" role="img" aria-label="Precipitation for the next hour in 15 minute steps">');
    parts.push('<line class="c-base" x1="6" y1="' + BASE + '" x2="' + (W - 6) + '" y2="' + BASE + '"/>');

    buckets.forEach(function (r, i) {
      var cx = SLOT * i + SLOT / 2;
      var p = num(r.precip_in) ? r.precip_in : 0;
      if (p > 0) {
        var h = Math.max(3, p / maxP * TOPMAX);
        parts.push(topRoundedBar(cx - BW / 2, BASE - h, BW, h, BASE, "c-rain"));
        parts.push('<text class="c-vlabel" x="' + cx + '" y="' + (BASE - h - 6).toFixed(1) +
          '" text-anchor="middle">' + p.toFixed(2) + "&quot;</text>");
      } else {
        parts.push('<rect class="c-stub" x="' + (cx - BW / 2) + '" y="' + (BASE - 2.5) +
          '" width="' + BW + '" height="2.5" rx="1.25"/>');
      }
      parts.push('<text x="' + cx + '" y="82" text-anchor="middle">' +
        esc(quarterLabel(parseTs(r.ts))) + "</text>");
    });

    parts.push("</svg>");
    return parts.join("");
  }

  // Daily hi/lo range bar on the shared 10-day scale (one hue on a quiet track)
  function dailyRangeBar(lo, hi, min, max) {
    if (!num(lo) || !num(hi)) return "";
    var span = Math.max(1, max - min);
    var x1 = (lo - min) / span * 100;
    var x2 = (hi - min) / span * 100;
    var w = Math.max(4, x2 - x1);
    return '<svg xmlns="http://www.w3.org/2000/svg" class="day-range" viewBox="0 0 100 10"' +
      ' preserveAspectRatio="none" role="img" aria-label="Low ' + Math.round(lo) +
      ', high ' + Math.round(hi) + ' degrees">' +
      '<rect class="r-track" x="0" y="3.4" width="100" height="3.2" rx="1.6"/>' +
      '<rect class="r-fill" x="' + x1.toFixed(1) + '" y="3.4" width="' + w.toFixed(1) +
      '" height="3.2" rx="1.6"/></svg>';
  }

  return {
    STEP: STEP,
    PAD_L: PAD_L,
    hourlyChart: hourlyChart,
    nowcastStrip: nowcastStrip,
    dailyRangeBar: dailyRangeBar,
    hourLabel: hourLabel,
    quarterLabel: quarterLabel,
    dayLabel: dayLabel,
    esc: esc
  };
});
