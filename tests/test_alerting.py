"""Alert pipeline tests: VTEC keying, affects_point, tiers, quiet hours,
escalation re-notify, all-clear, watchdog. No network anywhere."""

from datetime import timezone

import alerting
from alerting import (affects_point, in_quiet_hours, parse_vtec,
                      process_alerts, resolve_event_key)
from conftest import DAY, HIT_POLYGON, MISS_POLYGON, MORNING, NIGHT, make_feature


# ---- VTEC parsing and event keying ----

def test_parse_vtec_basic():
    v = parse_vtec("/O.NEW.KOUN.TO.W.0032.260703T1730Z-260703T1900Z/")
    assert v["action"] == "NEW"
    assert v["office"] == "KOUN"
    assert (v["phen"], v["sig"]) == ("TO", "W")
    assert v["etn"] == "0032"
    assert v["year"] == 2026


def test_parse_vtec_zero_begin_uses_end_year():
    v = parse_vtec("/O.CON.KOUN.TO.W.0032.000000T0000Z-260703T1900Z/")
    assert v["year"] == 2026


def test_parse_vtec_year_from_issuance_not_end():
    """A warning issued Dec 31 ending past midnight keys to the issuance
    year, matching VTEC ETN assignment and the IEM watchdog key."""
    v = parse_vtec("/O.NEW.KOUN.TO.W.0099.261231T2350Z-270101T0030Z/", fallback_year=2027)
    assert v["year"] == 2026


def test_parse_vtec_until_further_notice_falls_back_to_sent_year():
    v = parse_vtec("/O.NEW.KOUN.FF.W.0011.260703T1730Z-000000T0000Z/", fallback_year=2026)
    assert v["year"] == 2026
    v2 = parse_vtec("/O.CON.KOUN.FF.W.0011.000000T0000Z-000000T0000Z/", fallback_year=2026)
    assert v2["year"] == 2026


def test_vtec_continuation_with_empty_references_keeps_event_key(cfg, store, sent):
    """The verified gotcha: a live continuation arrives as messageType=Alert
    with EMPTY references and a new message id. The VTEC key must win."""
    first = make_feature(msg_id="urn:oid:2.49.0.1.840.0.1")
    process_alerts(cfg, store, [first], now=DAY, send=sent)
    assert len(sent.sent) == 1

    cont = make_feature(
        msg_id="urn:oid:2.49.0.1.840.0.2",
        vtec="/O.CON.KOUN.TO.W.0032.000000T0000Z-260703T1900Z/",
        message_type="Alert", references=[],
    )
    process_alerts(cfg, store, [cont], now=DAY, send=sent)
    assert len(sent.sent) == 1  # same event, no re-notify
    assert len(store.list_alerts()) == 1
    assert store.get_alert("KOUN.TO.W.0032.2026") is not None


def test_non_vtec_references_fallback(cfg, store, sent):
    """Special Weather Statements have no VTEC; updates must chain through
    references to the original message id or they fragment."""
    sps1 = make_feature(msg_id="urn:oid:2.49.0.1.840.0.10", event="Special Weather Statement",
                        vtec=None, severity="Moderate")
    process_alerts(cfg, store, [sps1], now=DAY, send=sent)

    sps2 = make_feature(msg_id="urn:oid:2.49.0.1.840.0.11", event="Special Weather Statement",
                        vtec=None, severity="Moderate", message_type="Update",
                        references=[{"identifier": "urn:oid:2.49.0.1.840.0.10"}])
    process_alerts(cfg, store, [sps2], now=DAY, send=sent)

    assert len(store.list_alerts()) == 1
    assert store.get_alert("urn:oid:2.49.0.1.840.0.10") is not None


def test_non_vtec_expired_references_fallback(cfg, store, sent):
    """When references is empty the chain continues into expiredReferences.
    Real NWS identifiers carry a hex hash segment; the extraction regex must
    not truncate at the first letter."""
    hex_id = "urn:oid:2.49.0.1.840.0.d40b636dd41f27b0e5c7d94b0d3d64ba32e2f971.001.1"
    sps1 = make_feature(msg_id=hex_id, event="Special Weather Statement",
                        vtec=None, severity="Moderate")
    process_alerts(cfg, store, [sps1], now=DAY, send=sent)

    sps2 = make_feature(
        msg_id="urn:oid:2.49.0.1.840.0.ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12.001.1",
        event="Special Weather Statement",
        vtec=None, severity="Moderate", message_type="Update", references=[],
        expired_references=[f"{hex_id},2026-07-03T12:15:00-05:00"],
    )
    process_alerts(cfg, store, [sps2], now=DAY, send=sent)

    assert len(store.list_alerts()) == 1
    assert store.get_alert(hex_id) is not None


def test_unknown_message_falls_back_to_bare_id(store):
    feature = make_feature(msg_id="urn:oid:2.49.0.1.840.0.30", vtec=None, references=[])
    key, vtec = resolve_event_key(feature["properties"], store.event_key_for_message)
    assert key == "urn:oid:2.49.0.1.840.0.30"
    assert vtec is None


# ---- affects_point ----

def test_affects_point_polygon_hit(cfg):
    feature = make_feature(geometry=HIT_POLYGON)
    assert affects_point(feature, cfg.lat, cfg.lon, cfg.ugc_codes) is True


def test_affects_point_polygon_miss_inside_county(cfg):
    """Polygon geometry wins over UGC: a warning polygon elsewhere in the
    county must not read as over your location."""
    feature = make_feature(geometry=MISS_POLYGON, ugc=("OKC027",))
    assert affects_point(feature, cfg.lat, cfg.lon, cfg.ugc_codes) is False


def test_affects_point_ugc_only_alert(cfg):
    feature = make_feature(geometry=None, ugc=("OKC027",))
    assert affects_point(feature, cfg.lat, cfg.lon, cfg.ugc_codes) is True


def test_affects_point_ugc_elsewhere(cfg):
    feature = make_feature(geometry=None, ugc=("OKC199",))
    assert affects_point(feature, cfg.lat, cfg.lon, cfg.ugc_codes) is False


# ---- quiet hours ----

def test_quiet_hours_boundaries(cfg):
    assert in_quiet_hours(cfg, NIGHT) is True        # 23:00 local
    assert in_quiet_hours(cfg, DAY) is False         # 13:00 local
    assert in_quiet_hours(cfg, MORNING) is False     # 08:00 local
    from datetime import datetime
    assert in_quiet_hours(cfg, datetime(2026, 7, 4, 3, 0, tzinfo=timezone.utc)) is True   # 22:00
    assert in_quiet_hours(cfg, datetime(2026, 7, 4, 11, 59, tzinfo=timezone.utc)) is True  # 06:59
    assert in_quiet_hours(cfg, datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)) is False  # 07:00


# ---- tier routing ----

def test_tornado_warning_immediate_with_channel_ping(cfg, store, sent):
    process_alerts(cfg, store, [make_feature()], now=DAY, send=sent)
    assert len(sent.sent) == 1
    text, at_channel = sent.sent[0]
    assert at_channel is True
    assert "Tornado Warning" in text


def test_severe_thunderstorm_warning_no_channel_ping(cfg, store, sent):
    feature = make_feature(event="Severe Thunderstorm Warning", severity="Severe",
                           vtec="/O.NEW.KOUN.SV.W.0101.260703T1730Z-260703T1900Z/")
    process_alerts(cfg, store, [feature], now=DAY, send=sent)
    assert len(sent.sent) == 1
    assert sent.sent[0][1] is False


def test_tier1_bypasses_quiet_hours(cfg, store, sent):
    feature = make_feature(sent="2026-07-03T23:00:00-05:00",
                           ends="2026-07-04T00:30:00-05:00",
                           expires="2026-07-04T00:30:00-05:00")
    process_alerts(cfg, store, [feature], now=NIGHT, send=sent)
    assert len(sent.sent) == 1


def test_tier2_watch_queues_during_quiet_hours_then_digests(cfg, store, sent):
    watch = make_feature(event="Tornado Watch", severity="Severe",
                         vtec="/O.NEW.KOUN.TO.A.0201.260704T0300Z-260704T1200Z/",
                         ends="2026-07-04T07:00:00-05:00",
                         expires="2026-07-04T07:00:00-05:00",
                         headline="Tornado Watch until 7 AM")
    process_alerts(cfg, store, [watch], now=NIGHT, send=sent)
    assert sent.sent == []  # suppressed into the digest

    # Still quiet: flush is a no-op
    assert alerting.flush_digest(cfg, store, now=NIGHT, send=sent) == 0
    assert sent.sent == []

    # Morning: one digest message
    assert alerting.flush_digest(cfg, store, now=MORNING, send=sent) == 1
    assert len(sent.sent) == 1
    assert "Morning digest" in sent.sent[0][0]
    assert "Tornado Watch" in sent.sent[0][0]

    # Queue drained: second flush sends nothing
    assert alerting.flush_digest(cfg, store, now=MORNING, send=sent) == 0
    assert len(sent.sent) == 1


def test_tier2_sends_immediately_outside_quiet_hours(cfg, store, sent):
    watch = make_feature(event="Severe Thunderstorm Watch", severity="Severe",
                         vtec="/O.NEW.KOUN.SV.A.0301.260703T1700Z-260704T0000Z/")
    process_alerts(cfg, store, [watch], now=DAY, send=sent)
    assert len(sent.sent) == 1
    assert sent.sent[0][1] is False


def test_extreme_severity_without_warning_phensig_is_tier1(cfg, store, sent):
    """Anything severity Extreme rides tier 1 even without TO/SV/FF.W VTEC."""
    feature = make_feature(event="Blizzard Warning", severity="Extreme",
                           vtec="/O.NEW.KOUN.BZ.W.0005.260703T1730Z-260704T1900Z/",
                           ends="2026-07-04T14:00:00-05:00",
                           expires="2026-07-04T14:00:00-05:00")
    process_alerts(cfg, store, [feature], now=NIGHT, send=sent)
    assert len(sent.sent) == 1  # sent despite quiet hours
    assert sent.sent[0][1] is False  # @channel is Tornado Warning only


# ---- within-event re-notify rules ----

def test_no_renotify_on_plain_continuation(cfg, store, sent):
    process_alerts(cfg, store, [make_feature()], now=DAY, send=sent)
    cont = make_feature(msg_id="urn:oid:2.49.0.1.840.0.2",
                        vtec="/O.CON.KOUN.TO.W.0032.000000T0000Z-260703T1900Z/")
    process_alerts(cfg, store, [cont], now=DAY, send=sent)
    assert len(sent.sent) == 1


def test_renotify_on_severity_escalation(cfg, store, sent):
    watch = make_feature(event="Severe Thunderstorm Watch", severity="Moderate",
                         vtec="/O.NEW.KOUN.SV.A.0301.260703T1700Z-260704T0000Z/")
    process_alerts(cfg, store, [watch], now=DAY, send=sent)
    escalated = make_feature(msg_id="urn:oid:2.49.0.1.840.0.2",
                             event="Severe Thunderstorm Watch", severity="Severe",
                             vtec="/O.CON.KOUN.SV.A.0301.000000T0000Z-260704T0000Z/")
    process_alerts(cfg, store, [escalated], now=DAY, send=sent)
    assert len(sent.sent) == 2
    assert "[escalation]" in sent.sent[1][0]


def test_renotify_on_damage_threat_escalation(cfg, store, sent):
    base = make_feature(event="Severe Thunderstorm Warning", severity="Severe",
                        vtec="/O.NEW.KOUN.SV.W.0101.260703T1730Z-260703T1900Z/")
    process_alerts(cfg, store, [base], now=DAY, send=sent)
    worse = make_feature(msg_id="urn:oid:2.49.0.1.840.0.2",
                         event="Severe Thunderstorm Warning", severity="Severe",
                         vtec="/O.CON.KOUN.SV.W.0101.000000T0000Z-260703T1900Z/",
                         extra_params={"thunderstormDamageThreat": ["DESTRUCTIVE"]})
    process_alerts(cfg, store, [worse], now=DAY, send=sent)
    assert len(sent.sent) == 2
    assert "[escalation]" in sent.sent[1][0]


def test_renotify_on_ends_extension_over_30_min(cfg, store, sent):
    process_alerts(cfg, store, [make_feature()], now=DAY, send=sent)
    extended = make_feature(msg_id="urn:oid:2.49.0.1.840.0.2",
                            vtec="/O.EXT.KOUN.TO.W.0032.000000T0000Z-260703T1945Z/",
                            ends="2026-07-03T14:45:00-05:00",
                            expires="2026-07-03T14:45:00-05:00")
    process_alerts(cfg, store, [extended], now=DAY, send=sent)
    assert len(sent.sent) == 2
    assert "[extended]" in sent.sent[1][0]


def test_no_renotify_on_small_ends_extension(cfg, store, sent):
    process_alerts(cfg, store, [make_feature()], now=DAY, send=sent)
    nudged = make_feature(msg_id="urn:oid:2.49.0.1.840.0.2",
                          vtec="/O.EXT.KOUN.TO.W.0032.000000T0000Z-260703T1910Z/",
                          ends="2026-07-03T14:10:00-05:00",
                          expires="2026-07-03T14:10:00-05:00")
    process_alerts(cfg, store, [nudged], now=DAY, send=sent)
    assert len(sent.sent) == 1


# ---- all-clear ----

def test_all_clear_on_cancel_message(cfg, store, sent):
    process_alerts(cfg, store, [make_feature()], now=DAY, send=sent)
    cancel = make_feature(msg_id="urn:oid:2.49.0.1.840.0.2", message_type="Cancel",
                          vtec="/O.CAN.KOUN.TO.W.0032.000000T0000Z-260703T1900Z/")
    process_alerts(cfg, store, [cancel], now=DAY, send=sent)
    assert len(sent.sent) == 2
    assert "All clear" in sent.sent[1][0]
    # A repeat cancel must not re-send
    process_alerts(cfg, store, [cancel], now=DAY, send=sent)
    assert len(sent.sent) == 2


def test_all_clear_on_expiry_sweep(cfg, store, sent):
    from datetime import datetime
    process_alerts(cfg, store, [make_feature()], now=DAY, send=sent)
    after_end = datetime(2026, 7, 3, 19, 30, tzinfo=timezone.utc)  # past 14:00 CDT ends
    process_alerts(cfg, store, [], now=after_end, send=sent)
    assert len(sent.sent) == 2
    assert "All clear" in sent.sent[1][0]
    process_alerts(cfg, store, [], now=after_end, send=sent)
    assert len(sent.sent) == 2  # once only


def test_no_all_clear_for_tier2(cfg, store, sent):
    from datetime import datetime
    watch = make_feature(event="Severe Thunderstorm Watch", severity="Severe",
                         vtec="/O.NEW.KOUN.SV.A.0301.260703T1700Z-260703T1900Z/")
    process_alerts(cfg, store, [watch], now=DAY, send=sent)
    after_end = datetime(2026, 7, 3, 19, 30, tzinfo=timezone.utc)
    process_alerts(cfg, store, [], now=after_end, send=sent)
    assert len(sent.sent) == 1


def test_multi_vtec_upgrade_message_notifies_new_warning(cfg, store, sent):
    """One CAP message can carry both the UPG of the old watch and the NEW
    of the replacement warning (NWS 10-1703 ordering: UPG line first). The
    warning must be stored, active, and notified; keying the whole message
    to the first VTEC line would silently drop it."""
    watch = make_feature(event="Winter Storm Watch", severity="Moderate",
                         vtec="/O.NEW.KOUN.WS.A.0004.260703T1730Z-260704T1900Z/",
                         ends="2026-07-04T14:00:00-05:00",
                         expires="2026-07-04T14:00:00-05:00")
    process_alerts(cfg, store, [watch], now=DAY, send=sent)
    assert len(sent.sent) == 1  # tier-2 watch, sent immediately by day

    upgrade = make_feature(
        msg_id="urn:oid:2.49.0.1.840.0.2", event="Winter Storm Warning",
        severity="Extreme", vtec=None,
        ends="2026-07-04T14:00:00-05:00", expires="2026-07-04T14:00:00-05:00",
        headline="Winter Storm Warning until 2 PM Saturday",
        extra_params={"VTEC": ["/O.UPG.KOUN.WS.A.0004.000000T0000Z-260704T1900Z/",
                               "/O.NEW.KOUN.WS.W.0011.260703T1800Z-260704T1900Z/"]},
    )
    process_alerts(cfg, store, [upgrade], now=DAY, send=sent)

    assert store.get_alert("KOUN.WS.A.0004.2026")["state"].get("canceled") is True
    warning = store.get_alert("KOUN.WS.W.0011.2026")
    assert warning is not None
    assert warning["event"] == "Winter Storm Warning"
    assert alerting.alert_is_active(warning, DAY) is True
    assert warning["state"].get("notified") is True
    assert len(sent.sent) == 2
    assert "Winter Storm Warning" in sent.sent[1][0]


def test_garbage_vtec_and_sent_parse_to_none_not_raise():
    assert parse_vtec("/O.NEW.KOUN.XX.Y.00ZZ.2Gbadstamp-2Galsobad/") is None
    assert parse_vtec("not a vtec at all") is None
    assert alerting._sent_year({"sent": "not-a-date"}) is None
    assert alerting._sent_year({"sent": None}) is None


def test_malformed_feature_does_not_sink_the_poll(cfg, store, sent):
    """One broken feature (here: garbage geometry that crashes the
    point-in-polygon check) costs only itself; the Tornado Warning behind it
    in the same feed must still notify."""
    bad = make_feature(msg_id="urn:oid:2.49.0.1.840.0.66",
                       event="Special Weather Statement", severity="Moderate",
                       vtec=None,
                       geometry={"type": "Polygon", "coordinates": [["garbage"]]})
    good = make_feature()
    process_alerts(cfg, store, [bad, good], now=DAY, send=sent)
    assert any("Tornado Warning" in text for text, _ in sent.sent)
    assert store.get_alert("KOUN.TO.W.0032.2026")["state"].get("notified") is True


def test_upgrade_ends_event_without_all_clear(cfg, store, sent):
    svw = make_feature(event="Severe Thunderstorm Warning", severity="Severe",
                       vtec="/O.NEW.KOUN.SV.W.0101.260703T1730Z-260703T1900Z/")
    process_alerts(cfg, store, [svw], now=DAY, send=sent)
    upg = make_feature(msg_id="urn:oid:2.49.0.1.840.0.2",
                       event="Severe Thunderstorm Warning", severity="Severe",
                       vtec="/O.UPG.KOUN.SV.W.0101.000000T0000Z-260703T1900Z/")
    process_alerts(cfg, store, [upg], now=DAY, send=sent)
    assert len(sent.sent) == 1  # no all-clear while the upgrade is live
    row = store.get_alert("KOUN.SV.W.0101.2026")
    assert row["state"].get("canceled") is True


# ---- send failures must retry, never drop ----

class FlakySender:
    """Fails the first n sends (returns False, like a Slack outage), then
    delivers. Records only successful deliveries."""

    def __init__(self, fail_first=1):
        self.fail_first = fail_first
        self.attempts = 0
        self.sent = []

    def __call__(self, text, at_channel=False):
        self.attempts += 1
        if self.attempts <= self.fail_first:
            return False
        self.sent.append((text, at_channel))
        return True


def test_failed_tier1_send_retries_on_next_poll(cfg, store):
    flaky = FlakySender(fail_first=1)
    process_alerts(cfg, store, [make_feature()], now=DAY, send=flaky)
    assert flaky.sent == []  # first send failed
    assert not store.get_alert("KOUN.TO.W.0032.2026")["state"].get("notified")

    process_alerts(cfg, store, [make_feature()], now=DAY, send=flaky)
    assert len(flaky.sent) == 1  # retried as "new" and delivered
    assert store.get_alert("KOUN.TO.W.0032.2026")["state"].get("notified") is True


def test_failed_all_clear_send_retries_on_next_poll(cfg, store, sent):
    process_alerts(cfg, store, [make_feature()], now=DAY, send=sent)
    cancel = make_feature(msg_id="urn:oid:2.49.0.1.840.0.2", message_type="Cancel",
                          vtec="/O.CAN.KOUN.TO.W.0032.000000T0000Z-260703T1900Z/")
    flaky = FlakySender(fail_first=1)
    process_alerts(cfg, store, [cancel], now=DAY, send=flaky)
    assert flaky.sent == []
    assert not store.get_alert("KOUN.TO.W.0032.2026")["state"].get("cleared")

    process_alerts(cfg, store, [cancel], now=DAY, send=flaky)
    assert len(flaky.sent) == 1
    assert "All clear" in flaky.sent[0][0]
    assert store.get_alert("KOUN.TO.W.0032.2026")["state"].get("cleared") is True


def test_failed_digest_send_keeps_queue(cfg, store, sent):
    watch = make_feature(event="Tornado Watch", severity="Severe",
                         vtec="/O.NEW.KOUN.TO.A.0201.260704T0300Z-260704T1200Z/",
                         ends="2026-07-04T07:00:00-05:00",
                         expires="2026-07-04T07:00:00-05:00")
    process_alerts(cfg, store, [watch], now=NIGHT, send=sent)

    flaky = FlakySender(fail_first=1)
    assert alerting.flush_digest(cfg, store, now=MORNING, send=flaky) == 0
    assert store.peek_digest() != []  # queue survives the failed post
    assert alerting.flush_digest(cfg, store, now=MORNING, send=flaky) == 1
    assert store.peek_digest() == []


def test_failed_watchdog_send_retries(cfg, store):
    flaky = FlakySender(fail_first=1)
    feature = {"geometry": HIT_POLYGON,
               "properties": {"wfo": "OUN", "phenomena": "TO", "significance": "W",
                              "eventid": 88, "issue": "2026-07-03T17:30:00Z",
                              "expire": "2026-07-03T19:00:00Z"}}
    assert alerting.check_watchdog(cfg, store, [feature], now=DAY, send=flaky) == 0
    assert alerting.check_watchdog(cfg, store, [feature], now=DAY, send=flaky) == 1
    assert alerting.check_watchdog(cfg, store, [feature], now=DAY, send=flaky) == 0


# ---- hot-watch cadence flag ----

def test_any_hot_watch(cfg, store, sent):
    assert alerting.any_hot_watch(store, DAY) is False
    watch = make_feature(event="Tornado Watch", severity="Severe",
                         vtec="/O.NEW.KOUN.TO.A.0201.260703T1700Z-260704T0000Z/",
                         ends="2026-07-03T19:00:00-05:00",
                         expires="2026-07-03T19:00:00-05:00")
    process_alerts(cfg, store, [watch], now=DAY, send=sent)
    assert alerting.any_hot_watch(store, DAY) is True


# ---- IEM watchdog ----

def _iem_feature(phen="TO", sig="W", eventid=77, geometry=None):
    return {
        "geometry": geometry or HIT_POLYGON,
        "properties": {
            "wfo": "OUN", "phenomena": phen, "significance": sig,
            "eventid": eventid, "issue": "2026-07-03T17:30:00Z",
            "expire": "2026-07-03T19:00:00Z",
        },
    }


def test_watchdog_fires_when_primary_cache_lacks_warning(cfg, store, sent):
    fired = alerting.check_watchdog(cfg, store, [_iem_feature()], now=DAY, send=sent)
    assert fired == 1
    assert "[watchdog]" in sent.sent[0][0]
    assert sent.sent[0][1] is True  # tornado polygon pings the channel
    # Same polygon on the next poll: no repeat
    assert alerting.check_watchdog(cfg, store, [_iem_feature()], now=DAY, send=sent) == 0


def test_watchdog_quiet_when_primary_has_event(cfg, store, sent):
    process_alerts(cfg, store, [make_feature(
        vtec="/O.NEW.KOUN.TO.W.0077.260703T1730Z-260703T1900Z/")], now=DAY, send=sent)
    sent.sent.clear()
    assert alerting.check_watchdog(cfg, store, [_iem_feature(eventid=77)], now=DAY, send=sent) == 0


def test_watchdog_ignores_polygon_missing_point(cfg, store, sent):
    feature = _iem_feature(geometry=MISS_POLYGON)
    assert alerting.check_watchdog(cfg, store, [feature], now=DAY, send=sent) == 0


def test_primary_silence_warning_once(cfg, store, sent):
    store.set_source_success("nws_alerts", DAY.timestamp() - 600)
    assert alerting.check_primary_silence(cfg, store, now=DAY, send=sent) is True
    assert "[watchdog]" in sent.sent[0][0]
    assert alerting.check_primary_silence(cfg, store, now=DAY, send=sent) is False
    assert len(sent.sent) == 1
    # Recovery re-arms the alarm
    store.set_source_success("nws_alerts", DAY.timestamp())
    assert alerting.check_primary_silence(cfg, store, now=DAY, send=sent) is False
    store.set_source_success("nws_alerts", DAY.timestamp() - 600)
    assert alerting.check_primary_silence(cfg, store, now=DAY, send=sent) is True
