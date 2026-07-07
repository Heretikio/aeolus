/* Generate the vendored Protomaps basemap styles (light + dark) for the
   radar map. Build-time only; the browser loads the emitted JSON, never
   this script or the basemaps library.

   Usage: node scripts/gen_basemap_styles.js
   Emits: static/vendor/style-light.json, static/vendor/style-dark.json

   The styles are fully self-contained per the PWA house rule: the vector
   source is the app's own /basemap.pmtiles (via the pmtiles:// protocol),
   glyphs and sprites point at static/vendor paths. Re-run only when the
   vendored @protomaps/basemaps version changes (scripts/vendor/basemaps.js,
   v5.7.2 per the v2 recon). */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

// The dist bundle is a browser IIFE that assigns a module-scoped
// `var basemaps`; evaluate it and pull the global out of the sandbox.
const bundle = fs.readFileSync(path.join(__dirname, "vendor", "basemaps.js"), "utf8");
const sandbox = {};
vm.runInNewContext(bundle + "\nthis.basemaps = basemaps;", sandbox);
const basemaps = sandbox.basemaps;

const OUT_DIR = path.join(__dirname, "..", "static", "vendor");

// Placeholder center/zoom; the radar view overrides them at runtime from the
// configured location, so these values are never actually shown.
function styleFor(flavorName) {
  const flavor = basemaps.namedFlavor(flavorName);
  return {
    version: 8,
    name: "aeolus-" + flavorName,
    glyphs: "/static/vendor/fonts/{fontstack}/{range}.pbf",
    sprite: "/static/vendor/sprites/" + flavorName,
    sources: {
      protomaps: {
        type: "vector",
        url: "pmtiles:///basemap.pmtiles",
        attribution: "",
      },
    },
    layers: basemaps.layers("protomaps", flavor, { lang: "en" }),
  };
}

for (const flavorName of ["light", "dark"]) {
  const style = styleFor(flavorName);
  const out = path.join(OUT_DIR, "style-" + flavorName + ".json");
  fs.writeFileSync(out, JSON.stringify(style));
  const fonts = new Set();
  for (const layer of style.layers) {
    const tf = layer.layout && layer.layout["text-font"];
    (tf || []).forEach((f) => fonts.add(f));
  }
  console.log(out, style.layers.length + " layers, fonts: " +
    [...fonts].sort().join(", "));
}
