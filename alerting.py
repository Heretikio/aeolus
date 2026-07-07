"""Alert pipeline: VTEC event keying, point-in-alert annotation, Slack tiers.

Verified gotchas encoded here (from the live research pass in DESIGN.md):
- properties.id identifies a MESSAGE, not an event; every update gets a new id.
- A live VTEC continuation can arrive as messageType=Alert with EMPTY
  references, so the VTEC-derived key always wins; reference walking is only
  the fallback for non-VTEC products.
- Non-VTEC products (Special Weather Statements) dedupe via the chain
  references -> expiredReferences -> bare id, or they fragment per update.
- One CAP message can carry SEVERAL VTEC lines (a watch upgraded to a
  warning arrives as UPG + NEW in one message, UPG first); each line is its
  own work item or the replacement warning is silently dropped.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("aeolus.alerting")

TIER1_PHENSIG = {("TO", "W"), ("SV", "W"), ("FF", "W")}
HOT_WATCH_PHENSIG = {("TO", "A"), ("SV", "A")}  # tightens the alert poll cadence
SEVERITY_RANK = {"unknown": 0, "minor": 1, "moderate": 2, "severe": 3, "extreme": 4}
DAMAGE_RANK = {"": 0, "base": 0, "considerable": 1, "destructive": 2, "catastrophic": 3}
EXTENSION_THRESHOLD = timedelta(minutes=30)
TERMINAL_ACTIONS = ("CAN", "EXP", "UPG")  # VTEC actions that end an event


# ---- time helpers ----

def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def in_quiet_hours(cfg, now_utc: datetime) -> bool:
    hour = now_utc.astimezone(cfg.tzinfo).hour
    start, end = cfg.quiet_hours_start, cfg.quiet_hours_end
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# ---- VTEC / event keying ----

def parse_vtec(raw: str, fallback_year: int | None = None):
    """Parse a P-VTEC string like /O.NEW.KOUN.TO.W.0032.260703T0130Z-260703T0200Z/.
    Returns None for anything unparseable; one garbage line must never sink a poll.

    Year comes from the event BEGIN time (VTEC ETNs are assigned per issuance
    year, and the IEM watchdog keys on issue year too), falling back to the
    message sent year, then the end time. Begin is 000000T0000Z on
    continuations of an already-running event.
    """
    parts = raw.strip().strip("/").split(".")
    if len(parts) != 7:
        return None
    _pclass, action, office, phen, sig, etn, times = parts
    begin, _, end = times.partition("-")
    year = None
    if begin and not begin.startswith("000000"):
        try:
            year = 2000 + int(begin[:2])
        except ValueError:
            return None
    if year is None:
        year = fallback_year
    if year is None and end and not end.startswith("000000"):
        try:
            year = 2000 + int(end[:2])
        except ValueError:
            return None
    return {"action": action, "office": office, "phen": phen, "sig": sig,
            "etn": etn, "year": year, "raw": raw}


def vtec_event_key(v: dict) -> str:
    return f"{v['office']}.{v['phen']}.{v['sig']}.{v['etn']}.{v['year']}"


def _sent_year(props: dict) -> int | None:
    try:
        return int((props.get("sent") or "")[:4])
    except (TypeError, ValueError):
        return None


def parse_vtec_lines(props: dict) -> list:
    """All parseable P-VTEC lines on a message. One CAP message can carry
    several: canonically the UPG/CAN of an old event followed by the NEW of
    its replacement (watch upgraded to warning), per NWS 10-1703 ordering."""
    params = props.get("parameters") or {}
    year = _sent_year(props)
    lines = []
    for raw in params.get("VTEC") or []:
        v = parse_vtec(raw, fallback_year=year)
        if v:
            lines.append(v)
    return lines


def resolve_event_key(props: dict, lookup_message):
    """Return (event_key, vtec_dict_or_None) for one alert message.

    Chain: VTEC first (always, regardless of messageType/references), then
    references, then expiredReferences, then the bare message id. Messages
    with several VTEC lines are expanded per line by process_alerts; this
    helper mainly serves the non-VTEC fallback chain.
    """
    vtecs = parse_vtec_lines(props)
    if vtecs:
        return vtec_event_key(vtecs[0]), vtecs[0]
    params = props.get("parameters") or {}
    for ref in props.get("references") or []:
        key = lookup_message(ref.get("identifier") or ref.get("@id") or "")
        if key:
            return key, None
    for blob in params.get("expiredReferences") or []:
        # \w, not \d: real NWS identifiers carry a hex hash segment.
        for ident in re.findall(r"urn:oid:[\w.]+", str(blob)):
            key = lookup_message(ident)
            if key:
                return key, None
    return props["id"], None


# ---- geometry / affects_point ----

def _point_in_ring(lon, lat, ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lon, lat, rings) -> bool:
    if not rings or not _point_in_ring(lon, lat, rings[0]):
        return False
    return not any(_point_in_ring(lon, lat, hole) for hole in rings[1:])


def point_in_geometry(lon, lat, geom) -> bool:
    if not geom:
        return False
    coords = geom.get("coordinates") or []
    if geom.get("type") == "Polygon":
        return _point_in_polygon(lon, lat, coords)
    if geom.get("type") == "MultiPolygon":
        return any(_point_in_polygon(lon, lat, p) for p in coords)
    return False


def affects_point(feature: dict, lat, lon, ugc_codes) -> bool:
    """Point-in-polygon when the alert has geometry, else UGC containment.
    Annotation only; alerts are never filtered on this."""
    geom = feature.get("geometry")
    if geom:
        return point_in_geometry(lon, lat, geom)
    props = feature.get("properties") or {}
    ugc = (props.get("geocode") or {}).get("UGC") or []
    return any(u in ugc_codes for u in ugc)


# ---- classification ----

def tier_for(props: dict, vtec) -> int:
    if vtec and (vtec["phen"], vtec["sig"]) in TIER1_PHENSIG:
        return 1
    if (props.get("severity") or "").lower() == "extreme":
        return 1
    return 2


def damage_threat(props: dict) -> str:
    params = props.get("parameters") or {}
    for key in ("tornadoDamageThreat", "thunderstormDamageThreat", "flashFloodDamageThreat"):
        vals = params.get(key) or []
        if vals:
            return str(vals[0]).lower()
    return ""


def alert_is_active(alert_row: dict, now_utc: datetime) -> bool:
    state = alert_row.get("state") or {}
    if state.get("canceled") or state.get("cleared"):
        return False
    end = alert_row.get("ends") or alert_row.get("expires")
    if not end:
        return True
    try:
        return parse_iso(end) > now_utc
    except ValueError:
        return True


def any_hot_watch(store, now_utc: datetime | None = None) -> bool:
    """True while a convective/tornado watch is active in the cache."""
    now_utc = now_utc or datetime.now(timezone.utc)
    for row in store.list_alerts():
        v = parse_vtec(row["vtec"]) if row.get("vtec") else None
        if v and (v["phen"], v["sig"]) in HOT_WATCH_PHENSIG and alert_is_active(row, now_utc):
            return True
    return False


# ---- Slack ----

def send_slack(cfg, text: str, at_channel: bool = False) -> bool:
    body = ("<!channel> " if at_channel else "") + text
    if not cfg.slack_webhook_url:
        log.info("DRY-RUN Slack send: %s", body)
        return True
    try:
        resp = requests.post(cfg.slack_webhook_url, json={"text": body}, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        # Never log the exception itself: the webhook URL (a secret) rides in
        # HTTPError/ConnectionError messages and tracebacks.
        status = getattr(getattr(e, "response", None), "status_code", None)
        log.error("Slack send failed: %s status=%s", type(e).__name__, status)
        return False


def _default_sender(cfg):
    return lambda text, at_channel=False: send_slack(cfg, text, at_channel)


def format_alert(props: dict, affects: bool, prefix: str = "") -> str:
    onset = props.get("onset") or props.get("effective") or "?"
    ends = props.get("ends") or props.get("expires") or "?"
    where = "covers your location" if affects else "in the area, not over your location"
    lines = [f"{prefix}{props.get('event', 'Alert')}: {props.get('headline') or ''}".strip(),
             f"{onset} -> {ends} ({where})"]
    instruction = (props.get("instruction") or "").strip()
    if instruction:
        lines.append(instruction[:400])
    return "\n".join(lines)


# ---- ingestion / notification ----

def process_alerts(cfg, store, features, now=None, send=None):
    """Ingest one poll of the NWS active-alerts feed and route notifications.
    One malformed feature costs only itself; the rest of the feed and the
    expiry sweep still run."""
    now = now or datetime.now(timezone.utc)
    send = send or _default_sender(cfg)
    active_keys = set()
    for feature in features or []:
        try:
            _process_feature(cfg, store, feature, active_keys, now, send)
        except Exception:
            log.exception("alert feature %s failed; continuing",
                          (feature.get("properties") or {}).get("id"))
    _expiry_sweep(store, active_keys, now, send)


def _process_feature(cfg, store, feature, active_keys, now, send):
    props = feature.get("properties") or {}
    if not props.get("id"):
        return
    affects = affects_point(feature, cfg.lat, cfg.lon, cfg.ugc_codes)
    vtecs = parse_vtec_lines(props)
    if vtecs:
        # One work item per VTEC line: a single message can UPG/CAN the old
        # event AND carry the NEW replacement (watch upgraded to warning).
        # Terminal lines only touch state; upserting them would clobber the
        # old event's row with the replacement message's text.
        for vtec in vtecs:
            key = vtec_event_key(vtec)
            active_keys.add(key)
            store.record_alert_message(props["id"], key)
            existing = store.get_alert(key)
            if vtec["action"] not in TERMINAL_ACTIONS:
                store.upsert_alert(_alert_row(key, props, vtec, feature, affects, now))
            _dispatch(cfg, store, key, props, vtec, affects, existing, now, send)
        return
    # Non-VTEC products: references -> expiredReferences -> bare id.
    key, _ = resolve_event_key(props, store.event_key_for_message)
    active_keys.add(key)
    store.record_alert_message(props["id"], key)
    existing = store.get_alert(key)
    store.upsert_alert(_alert_row(key, props, None, feature, affects, now))
    _dispatch(cfg, store, key, props, None, affects, existing, now, send)


def _alert_row(key, props, vtec, feature, affects, now) -> dict:
    params = props.get("parameters") or {}
    threat = {k: v for k, v in params.items()
              if "DamageThreat" in k or k in ("tornadoDetection", "hailThreat",
                                              "windThreat", "maxHailSize", "maxWindGust")}
    return {
        "event_key": key,
        "message_id": props.get("id"),
        "vtec": vtec["raw"] if vtec else None,
        "event": props.get("event"),
        "severity": props.get("severity"),
        "certainty": props.get("certainty"),
        "urgency": props.get("urgency"),
        "response": props.get("response"),
        "headline": props.get("headline"),
        "nws_headline": (params.get("NWSheadline") or [None])[0],
        "description": props.get("description"),
        "instruction": props.get("instruction"),
        "onset": props.get("onset"),
        "ends": props.get("ends"),
        "expires": props.get("expires"),
        "area_desc": props.get("areaDesc"),
        "ugc": json.dumps(((props.get("geocode") or {}).get("UGC")) or []),
        "geometry": json.dumps(feature.get("geometry")) if feature.get("geometry") else None,
        "threat_params": json.dumps(threat),
        "affects_point": 1 if affects else 0,
        "updated_at": now.timestamp(),
    }


def _extended_past_threshold(old_ends, new_ends) -> bool:
    if not old_ends or not new_ends:
        return False
    try:
        return parse_iso(new_ends) - parse_iso(old_ends) > EXTENSION_THRESHOLD
    except ValueError:
        return False


def _dispatch(cfg, store, key, props, vtec, affects, existing, now, send):
    state = dict((existing or {}).get("state") or {})
    tier = tier_for(props, vtec)
    severity = (props.get("severity") or "unknown").lower()
    damage = damage_threat(props)
    ends = props.get("ends") or props.get("expires")
    action = vtec["action"] if vtec else None

    # Cancellation / expiry messages: all-clear for notified tier-1 events.
    # Gate on the tier persisted at notification time first; a Cancel can
    # arrive with downgraded severity and recompute as tier 2.
    if props.get("messageType") == "Cancel" or action in ("CAN", "EXP"):
        if ((state.get("tier") == 1 or tier == 1)
                and state.get("notified") and not state.get("cleared")):
            if not send(f"All clear: {props.get('event', 'alert')} has ended."):
                return  # state untouched; the next poll retries the all-clear
            state["cleared"] = True
        state["canceled"] = True
        store.set_alert_state(key, state)
        return

    # Upgrades (e.g. SV.W upgraded to TO.W) end this event; the replacement
    # is a new VTEC event and notifies on its own. No all-clear here.
    if action == "UPG":
        state["canceled"] = True
        store.set_alert_state(key, state)
        return

    if not state.get("notified"):
        reason = "new"
    elif SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(state.get("severity", "unknown"), 0):
        reason = "escalation"
    elif DAMAGE_RANK.get(damage, 0) > DAMAGE_RANK.get(state.get("damage_threat", ""), 0):
        reason = "escalation"
    elif _extended_past_threshold(state.get("ends"), ends):
        reason = "extended"
    else:
        reason = None
    if not reason:
        return

    prefix = {"new": "", "escalation": "[escalation] ", "extended": "[extended] "}[reason]
    text = format_alert(props, affects, prefix)
    if tier == 1:
        # Immediate, bypasses quiet hours. @channel only for Tornado Warning.
        ping = bool(vtec and (vtec["phen"], vtec["sig"]) == ("TO", "W")) \
            or props.get("event") == "Tornado Warning"
        delivered = send(text, at_channel=ping)
    elif in_quiet_hours(cfg, now):
        store.queue_digest(key, text, now.timestamp())
        delivered = True
    else:
        delivered = send(text)
    if not delivered:
        return  # state untouched; the next 30-60s poll retries the send

    store.set_alert_state(key, {
        "notified": True, "tier": tier, "severity": severity,
        "damage_threat": damage, "ends": ends, "cleared": False,
        "canceled": state.get("canceled", False), "notified_at": now.isoformat(),
    })


def _expiry_sweep(store, active_keys, now, send):
    """All-clear for notified tier-1 events that fell out of the active feed
    past their end time (expiry without an explicit Cancel message)."""
    for row in store.list_alerts():
        state = row.get("state") or {}
        if (state.get("tier") != 1 or not state.get("notified")
                or state.get("cleared") or state.get("canceled")
                or row["event_key"] in active_keys):
            continue
        end = row.get("ends") or row.get("expires")
        try:
            if end and parse_iso(end) <= now:
                if send(f"All clear: {row.get('event', 'alert')} has expired."):
                    state["cleared"] = True
                    store.set_alert_state(row["event_key"], state)
        except ValueError:
            continue


def flush_digest(cfg, store, now=None, send=None) -> int:
    """Send queued tier-2 notifications once quiet hours end. No-op inside
    quiet hours or with an empty queue. The queue is only deleted after a
    confirmed send; a failed post retries on the next housekeeping tick
    (duplicate-on-retry beats silent loss)."""
    now = now or datetime.now(timezone.utc)
    if in_quiet_hours(cfg, now):
        return 0
    items = store.peek_digest()
    if not items:
        return 0
    send = send or _default_sender(cfg)
    lines = "\n".join(f"- {item['summary'].splitlines()[0]}" for item in items)
    if not send(f"Morning digest: {len(items)} overnight alert update(s):\n{lines}"):
        return 0
    store.delete_digest([item["id"] for item in items])
    return len(items)


# ---- IEM watchdog ----

WATCHDOG_PHENSIG = {("TO", "W"), ("SV", "W"), ("FF", "W")}


def _iem_event_key(props: dict, now: datetime):
    wfo = (props.get("wfo") or "").strip()
    office = "K" + wfo if len(wfo) == 3 else wfo
    etn = props.get("eventid")
    phen, sig = props.get("phenomena"), props.get("significance")
    if not (office and phen and sig and etn is not None):
        return None
    year = now.year
    for field in ("issue", "polygon_begin", "utc_issue"):
        value = props.get(field)
        if value:
            try:
                year = int(str(value)[:4])
                break
            except ValueError:
                continue
    return f"{office}.{phen}.{sig}.{int(etn):04d}.{year}"


def check_watchdog(cfg, store, features, now=None, send=None) -> int:
    """Fire the watchdog path when IEM shows an active TO.W/SV.W/FF.W polygon
    covering your location that the primary NWS cache lacks."""
    now = now or datetime.now(timezone.utc)
    send = send or _default_sender(cfg)
    fired = 0
    for feature in features or []:
        props = feature.get("properties") or {}
        phensig = (props.get("phenomena"), props.get("significance"))
        if phensig not in WATCHDOG_PHENSIG:
            continue
        end = props.get("expire_utc") or props.get("expire") or props.get("polygon_end")
        if end:
            try:
                if parse_iso(str(end)) <= now:
                    continue
            except ValueError:
                pass
        if not point_in_geometry(cfg.lon, cfg.lat, feature.get("geometry")):
            continue
        key = _iem_event_key(props, now)
        if key and store.get_alert(key):
            continue  # primary cache has it; watchdog satisfied
        marker = f"watchdog_notified:{key or props.get('id', 'unknown')}"
        if store.meta_get(marker):
            continue
        label = {"TO": "Tornado Warning", "SV": "Severe Thunderstorm Warning",
                 "FF": "Flash Flood Warning"}.get(phensig[0], "warning")
        if not send(f"[watchdog] IEM shows an active {label} polygon over your location"
                    f" that the primary NWS feed does not have (event {key or '?'})."
                    " Treat as live; check NWS directly.",
                    at_channel=(phensig == ("TO", "W"))):
            continue  # marker not set; the next watchdog poll retries
        store.meta_set(marker, now.isoformat())
        fired += 1
    return fired


def check_primary_silence(cfg, store, now=None, send=None) -> bool:
    """Ops warning when the primary NWS alert poller has been failing >5 min.
    Warns once per outage; re-arms after the next success."""
    now = now or datetime.now(timezone.utc)
    status = store.source_status("nws_alerts") or {}
    last = status.get("last_success")
    started = store.meta_get("pollers_started_at")
    reference = last or (float(started) if started else now.timestamp())
    silent_for = now.timestamp() - reference
    if silent_for <= cfg.poller_silence_alarm:
        if store.meta_get("nws_silence_warned"):
            store.meta_set("nws_silence_warned", "")
        return False
    if store.meta_get("nws_silence_warned"):
        return False
    send = send or _default_sender(cfg)
    if not send(f"[watchdog] NWS alert poller has not succeeded for {int(silent_for // 60)} min;"
                " leaning on the IEM watchdog until it recovers."):
        return False  # marker not set; the next housekeeping tick retries
    store.meta_set("nws_silence_warned", now.isoformat())
    return True
