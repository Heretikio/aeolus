#!/usr/bin/env bash
# make-basemap.sh: generate the Aeolus radar basemap (basemap.pmtiles) from the
# latest Protomaps daily planet build and install it into Aeolus's data volume.
#
# The basemap is OPTIONAL: without it the radar map shows a "basemap not
# installed" notice but radar frames and alert polygons still work. Install it
# to get a real street/terrain map under the radar.
#
# Run this on the Docker host (any linux-amd64 box with Docker + network). The
# extract range-reads only your region's slice of the ~136 GB planet build
# (a few hundred MB transferred, minutes not hours). Re-run quarterly.
#
# Usage:
#   ./make-basemap.sh                 # latest available daily build
#   ./make-basemap.sh 20260703        # a specific YYYYMMDD build
#
# Configuration (env):
#   RADAR_BBOX        region to extract, "W,S,E,N". MUST match the server's
#                     RADAR_BBOX so the map covers the same area as the radar.
#   AEOLUS_VOLUME     named Docker volume to install into (default aeolus-data,
#                     matching docker-compose.yml).
#   AEOLUS_DATA_DIR   install into this host directory instead of a volume
#                     (for a bind-mount deployment).
#
# Notes:
# - build.protomaps.com has no machine-readable index; we probe today's date
#   (UTC) and walk backwards until a build responds to a range request.
# - The extract source must be clustered; daily builds are.
# - Output is NOT committed to git; it lives only in the data volume/dir.

set -euo pipefail

BBOX="${RADAR_BBOX:--103.5,30.5,-91.5,39.5}"   # W,S,E,N; must match server RADAR_BBOX
MAXZOOM=12
PMTILES_VERSION="1.30.3"
AEOLUS_VOLUME="${AEOLUS_VOLUME:-aeolus-data}"
STAGE_DIR="${TMPDIR:-$HOME/tmp}"
AEOLUS_UID=10001

mkdir -p "$HOME/bin" "$STAGE_DIR"

# 1. Ensure pmtiles CLI (static linux-amd64 binary from go-pmtiles releases).
PMTILES="$HOME/bin/pmtiles"
if ! "$PMTILES" version >/dev/null 2>&1; then
    echo "==> installing go-pmtiles v$PMTILES_VERSION to $PMTILES"
    curl -fsSL -o /tmp/go-pmtiles.tar.gz \
        "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_x86_64.tar.gz"
    tar -xzf /tmp/go-pmtiles.tar.gz -C /tmp pmtiles
    mv /tmp/pmtiles "$PMTILES"
    chmod +x "$PMTILES"
    rm -f /tmp/go-pmtiles.tar.gz
fi
"$PMTILES" version

# 2. Pick the build: argument wins, else probe back from today (UTC).
BUILD="${1:-}"
if [ -z "$BUILD" ]; then
    for i in 0 1 2 3 4 5 6 7; do
        d=$(date -u -d "$i days ago" +%Y%m%d 2>/dev/null || date -u -v-"${i}"d +%Y%m%d)
        code=$(curl -s -o /dev/null -w '%{http_code}' -r 0-13 \
            "https://build.protomaps.com/$d.pmtiles")
        if [ "$code" = "206" ] || [ "$code" = "200" ]; then
            BUILD="$d"
            break
        fi
    done
fi
[ -n "$BUILD" ] || { echo "ERROR: no protomaps daily build found in the last week" >&2; exit 1; }
SRC="https://build.protomaps.com/${BUILD}.pmtiles"
echo "==> extracting region from $SRC (bbox $BBOX, maxzoom $MAXZOOM)"

# 3. Extract to a staging file.
STAGE="$STAGE_DIR/basemap.pmtiles.new"
rm -f "$STAGE"
"$PMTILES" extract "$SRC" "$STAGE" \
    --bbox="$BBOX" --maxzoom="$MAXZOOM" \
    --download-threads=4 --overfetch=0.05

# 4. Sanity-check the result before installing it.
"$PMTILES" show "$STAGE" | sed -n '1,20p'
sz=$(stat -c %s "$STAGE" 2>/dev/null || stat -f %z "$STAGE")
[ "$sz" -gt 5000000 ] || { echo "ERROR: extract suspiciously small ($sz bytes), not installing" >&2; exit 1; }

# 5. Install into the data volume/dir as uid 10001 via a one-shot container
#    (atomic-ish: copy under a temp name, chown, then rename inside the mount so
#    the running container never sees a partial file).
if [ -n "${AEOLUS_DATA_DIR:-}" ]; then
    TARGET_DESC="host dir $AEOLUS_DATA_DIR"
    MOUNT_ARG=(-v "$AEOLUS_DATA_DIR":/data)
else
    TARGET_DESC="volume $AEOLUS_VOLUME"
    MOUNT_ARG=(-v "$AEOLUS_VOLUME":/data)
fi
echo "==> installing basemap.pmtiles into $TARGET_DESC (uid $AEOLUS_UID)"
docker run --rm \
    "${MOUNT_ARG[@]}" \
    -v "$STAGE_DIR":/stage \
    alpine sh -c "cp /stage/$(basename "$STAGE") /data/basemap.pmtiles.new \
        && chown $AEOLUS_UID:$AEOLUS_UID /data/basemap.pmtiles.new \
        && chmod 644 /data/basemap.pmtiles.new \
        && mv /data/basemap.pmtiles.new /data/basemap.pmtiles"
rm -f "$STAGE"

echo "==> done: basemap installed into $TARGET_DESC ($(numfmt --to=iec "$sz" 2>/dev/null || echo "$sz bytes")), build $BUILD"
echo "    restart is not required; the /basemap.pmtiles endpoint serves it immediately."
