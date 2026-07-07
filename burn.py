"""Burn-window verdict: can the burn pile be lit right now?

Pure decision logic over canonical conditions rows (adapters.py schema) and
the cached NWS alerts. Thresholds live on Config (BURN_* env vars); this
module does no I/O. Guidance only: county burn bans are declared by people,
not weather feeds, and are not modeled here.

Rules (defaults, all env-tunable):
- no_burn: sustained wind >= 15 mph, gusts >= 25 mph, humidity <= 25%, an
  active Red Flag Warning / Fire Weather Warning, or the combo of
  wind >= 10 mph AND humidity <= 35%.
- caution: wind 10-15 mph, gusts 20-25 mph, humidity 25-40%, an active
  Fire Weather Watch, or missing wind/humidity data ("insufficient data").
- go: everything else.
Every trigger yields a human reason string; the worst tier wins.
"""

import re
from datetime import datetime, timedelta, timezone

import alerting

GO = "go"
CAUTION = "caution"
NO_BURN = "no_burn"
VERDICT_RANK = {GO: 0, CAUTION: 1, NO_BURN: 2}

# Wind forecasts degrade past a few days: outlook days 4+ carry the flag.
LOW_CONFIDENCE_FROM_DAY = 4

# Alert events gating the verdict, matched case-insensitively as substrings.
# "Fire Weather Watch" must not match the warning terms, and non-fire alerts
# (heat, flood, wind chill, ...) must never trigger anything here.
NO_BURN_EVENT_TERMS = ("red flag", "fire weather warning")
CAUTION_EVENT_TERMS = ("fire weather watch",)


def _num(row, key):
    v = (row or {}).get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _matching_events(alerts, terms):
    out = []
    for a in alerts or []:
        event = a.get("event") or ""
        if any(t in event.lower() for t in terms):
            out.append(event)
    return out


def evaluate(cfg, row, active_alerts):
    """(verdict, reasons) for one canonical conditions row plus the alerts
    active at that instant. Missing wind or humidity can never yield "go":
    the data gap itself degrades the verdict to caution."""
    wind = _num(row, "wind_mph")
    gusts = _num(row, "gusts_mph")
    humidity = _num(row, "humidity_pct")

    no_burn, caution = [], []

    if wind is not None:
        if wind >= cfg.burn_wind_no_burn:
            no_burn.append(f"wind {round(wind)} mph")
        elif wind >= cfg.burn_wind_caution:
            caution.append(f"wind {round(wind)} mph")
    if gusts is not None:
        if gusts >= cfg.burn_gusts_no_burn:
            no_burn.append(f"gusts {round(gusts)} mph")
        elif gusts >= cfg.burn_gusts_caution:
            caution.append(f"gusts {round(gusts)} mph")
    if humidity is not None:
        if humidity <= cfg.burn_humidity_no_burn:
            no_burn.append(f"humidity {round(humidity)}%")
        elif humidity <= cfg.burn_humidity_caution:
            caution.append(f"humidity {round(humidity)}%")

    # Combo rule: moderate wind on dry air is a no_burn even when neither
    # factor crosses its own hard limit. Skipped when a single-factor
    # no_burn already names the culprit (the combo string would be noise).
    if (wind is not None and humidity is not None
            and wind >= cfg.burn_combo_wind and humidity <= cfg.burn_combo_humidity
            and wind < cfg.burn_wind_no_burn and humidity > cfg.burn_humidity_no_burn):
        no_burn.append(f"wind {round(wind)} mph with humidity {round(humidity)}%")

    no_burn.extend(f"{e} active"
                   for e in _matching_events(active_alerts, NO_BURN_EVENT_TERMS))
    caution.extend(f"{e} active"
                   for e in _matching_events(active_alerts, CAUTION_EVENT_TERMS))

    if no_burn:
        return NO_BURN, no_burn
    if wind is None or humidity is None:
        caution.append("insufficient data")
    if caution:
        return CAUTION, caution
    return GO, []


def lookahead(cfg, current_verdict, future_rows, alerts, tzinfo):
    """Scan the upcoming hourly rows with the same rules; alerts are
    re-tested per hour, so a Red Flag Warning that ends mid-window releases
    the verdict. Returns (changes_at, next_verdict, next_reasons) for the
    first hour whose verdict differs from now, or (None, None, None) when
    the verdict holds through the window."""
    for ts, row in future_rows or []:
        try:
            hour_utc = (datetime.fromisoformat(ts).replace(tzinfo=tzinfo)
                        .astimezone(timezone.utc))
        except ValueError:
            continue
        active = [a for a in alerts or [] if alerting.alert_is_active(a, hour_utc)]
        verdict, reasons = evaluate(cfg, row, active)
        if verdict != current_verdict:
            return ts, verdict, reasons
    return None, None, None


# ---- multi-day outlook ----

# Single-factor reason strings, parsed back for dedup so a day keeps only
# its most extreme instance per factor. Higher score = worse conditions.
_FACTOR_SCORES = (
    (re.compile(r"^wind (\d+) mph$"), lambda m: int(m.group(1))),
    (re.compile(r"^gusts (\d+) mph$"), lambda m: int(m.group(1))),
    (re.compile(r"^humidity (\d+)%$"), lambda m: -int(m.group(1))),
    (re.compile(r"^wind (\d+) mph with humidity (\d+)%$"),
     lambda m: int(m.group(1)) - int(m.group(2))),
)


def _day_reasons(hour_results, worst):
    """Worst-tier triggers with day+hour attribution ("Fri 14:00: gusts
    27 mph"), deduped: numeric factors keep their most extreme hour,
    everything else (alerts, insufficient data) keeps its first hour.
    Chronological output."""
    best = {}  # dedup key -> (score, local_dt, reason)
    for local, verdict, reasons in hour_results:
        if verdict != worst:
            continue
        for reason in reasons:
            key, score = reason, None
            for rx, scorer in _FACTOR_SCORES:
                m = rx.match(reason)
                if m:
                    key, score = rx.pattern, scorer(m)
                    break
            prev = best.get(key)
            if prev is None or (score is not None and score > prev[0]):
                best[key] = (score, local, reason)
    entries = sorted(best.values(), key=lambda e: e[1])
    return [f"{local.strftime('%a %H:%M')}: {reason}" for _, local, reason in entries]


def outlook(cfg, rows, alerts, tzinfo, start_date):
    """Per-day burn outlook. Our fires usually burn for ~2 days, so a day's
    verdict covers the fire's whole life: the WORST hourly verdict in
    [day 00:00 local, +burn_outlook_window_hours), full 24h days (overnight
    wind matters to a smoldering pile). Alerts are re-tested per hour.

    Data honesty: days whose window is only partially covered by the hourly
    run evaluate what exists and carry partial=true (never fabricate); days
    with no coverage at all are omitted; days LOW_CONFIDENCE_FROM_DAY and
    beyond carry low_confidence=true."""
    parsed = []
    for ts, row in rows or []:
        try:
            local = datetime.fromisoformat(ts).replace(tzinfo=tzinfo)
        except ValueError:
            continue
        parsed.append((local, row))

    out = []
    for i in range(cfg.burn_outlook_days):
        day = start_date + timedelta(days=i)
        win_start = datetime(day.year, day.month, day.day, tzinfo=tzinfo)
        win_end = win_start + timedelta(hours=cfg.burn_outlook_window_hours)
        hours = [(local, row) for local, row in parsed
                 if win_start <= local < win_end]
        if not hours:
            continue
        worst = GO
        results = []
        for local, row in hours:
            hour_utc = local.astimezone(timezone.utc)
            active = [a for a in alerts or []
                      if alerting.alert_is_active(a, hour_utc)]
            verdict, reasons = evaluate(cfg, row, active)
            results.append((local, verdict, reasons))
            if VERDICT_RANK[verdict] > VERDICT_RANK[worst]:
                worst = verdict
        out.append({
            "date": day.isoformat(),
            "verdict": worst,
            "reasons": _day_reasons(results, worst),
            "partial": len(hours) < cfg.burn_outlook_window_hours,
            "low_confidence": i >= LOW_CONFIDENCE_FROM_DAY - 1,
        })
    return out
