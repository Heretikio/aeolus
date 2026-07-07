"""Aeolus configuration. Every knob is env-driven with a sensible default."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _s(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    # Your location and NWS geography. Defaults are an example (Norman, OK);
    # set LAT/LON/NWS_ZONE/NWS_UGC/TZ for your own point.
    lat: float = 35.22
    lon: float = -97.44
    location_name: str = "Home"
    nws_zone: str = "OKC027"
    # UGC codes considered "ours" for containment checks (county + forecast zone)
    nws_ugc: str = "OKC027,OKZ029"
    # NWS alerts + IEM watchdog are US-only. Set 0 outside the US: leaving a US
    # zone configured would otherwise surface that zone's alerts everywhere.
    alerts_enabled: bool = True

    db_path: str = "./data/aeolus.db"
    # NWS asks for an identifying User-Agent with a real contact; set one.
    user_agent: str = "aeolus/1.0 (you@example.com)"
    tz: str = "America/Chicago"

    # Optional integrations. Empty pirate key = poller off.
    # Empty Slack webhook = sends are logged as DRY-RUN instead.
    pirate_weather_key: str = ""
    slack_webhook_url: str = ""

    quiet_hours_start: int = 22
    quiet_hours_end: int = 7

    # Poll cadences, seconds
    forecast_interval: int = 3600
    nowcast_interval: int = 300
    pirate_interval: int = 3600
    alerts_interval: int = 60
    alerts_interval_hot: int = 30  # while a convective/tornado watch is active (NWS documented floor)
    watchdog_interval: int = 60

    http_timeout: int = 15
    forecast_keep_runs: int = 5  # last N forecast runs kept per source/kind
    backoff_base: int = 30
    backoff_cap: int = 900
    poller_silence_alarm: int = 300  # primary alert poller silent this long -> watchdog ops warning

    # Burn-window thresholds (/v1/burn): mph and percent. no_burn beats
    # caution; the combo rule is wind AND humidity crossing together.
    burn_wind_no_burn: float = 15.0
    burn_gusts_no_burn: float = 25.0
    burn_humidity_no_burn: float = 25.0
    burn_combo_wind: float = 10.0
    burn_combo_humidity: float = 35.0
    burn_wind_caution: float = 10.0
    burn_gusts_caution: float = 20.0
    burn_humidity_caution: float = 40.0
    burn_lookahead_hours: int = 12
    # Outlook (/v1/burn/outlook): per-day verdicts on the DAILY view. A pile
    # burns ~2 days, so each day's verdict covers [00:00 local, +window).
    burn_outlook_days: int = 7
    burn_outlook_window_hours: int = 48

    # Radar (v2): IEM NEXRAD N0Q loop cache with nowCOAST WMS fallback
    radar_enabled: bool = True
    radar_loop_hours: int = 6
    # Storm region (west,south,east,north): OK and the Southern Plains
    radar_bbox: str = "-103.5,30.5,-91.5,39.5"
    radar_warm_zooms: str = "6-11"
    radar_cache_dir: str = ""    # empty -> "radar" next to the DB
    radar_cache_max_mb: int = 500
    radar_interval: int = 90
    basemap_path: str = ""       # empty -> "basemap.pmtiles" next to the DB

    def __post_init__(self):
        data_dir = os.path.dirname(os.path.abspath(self.db_path))
        self.radar_cache_dir = self.radar_cache_dir or os.path.join(data_dir, "radar")
        self.basemap_path = self.basemap_path or os.path.join(data_dir, "basemap.pmtiles")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            lat=_f("LAT", 35.22),
            lon=_f("LON", -97.44),
            location_name=_s("LOCATION_NAME", "Home"),
            nws_zone=_s("NWS_ZONE", "OKC027"),
            nws_ugc=_s("NWS_UGC", "OKC027,OKZ029"),
            alerts_enabled=_s("ALERTS_ENABLED", "1") != "0",
            db_path=_s("DB_PATH", "./data/aeolus.db"),
            user_agent=_s("USER_AGENT", "aeolus/1.0 (you@example.com)"),
            tz=_s("TZ", "America/Chicago"),
            pirate_weather_key=_s("PIRATE_WEATHER_KEY", ""),
            slack_webhook_url=_s("SLACK_WEBHOOK_URL", ""),
            quiet_hours_start=_i("QUIET_HOURS_START", 22),
            quiet_hours_end=_i("QUIET_HOURS_END", 7),
            # Clamped: a zero/negative interval would hot-loop the poller, and
            # the NWS documented polling floor is 30s (violating it risks an
            # IP block on exactly the source that matters most).
            forecast_interval=max(1, _i("FORECAST_INTERVAL", 3600)),
            nowcast_interval=max(1, _i("NOWCAST_INTERVAL", 300)),
            pirate_interval=max(1, _i("PIRATE_INTERVAL", 3600)),
            alerts_interval=max(30, _i("ALERTS_INTERVAL", 60)),
            alerts_interval_hot=max(30, _i("ALERTS_INTERVAL_HOT", 30)),
            watchdog_interval=max(1, _i("WATCHDOG_INTERVAL", 60)),
            http_timeout=_i("HTTP_TIMEOUT", 15),
            forecast_keep_runs=_i("FORECAST_KEEP_RUNS", 5),
            backoff_base=_i("BACKOFF_BASE", 30),
            backoff_cap=_i("BACKOFF_CAP", 900),
            poller_silence_alarm=_i("POLLER_SILENCE_ALARM", 300),
            burn_wind_no_burn=_f("BURN_WIND_NO_BURN", 15.0),
            burn_gusts_no_burn=_f("BURN_GUSTS_NO_BURN", 25.0),
            burn_humidity_no_burn=_f("BURN_HUMIDITY_NO_BURN", 25.0),
            burn_combo_wind=_f("BURN_COMBO_WIND", 10.0),
            burn_combo_humidity=_f("BURN_COMBO_HUMIDITY", 35.0),
            burn_wind_caution=_f("BURN_WIND_CAUTION", 10.0),
            burn_gusts_caution=_f("BURN_GUSTS_CAUTION", 20.0),
            burn_humidity_caution=_f("BURN_HUMIDITY_CAUTION", 40.0),
            burn_lookahead_hours=max(1, _i("BURN_LOOKAHEAD_HOURS", 12)),
            burn_outlook_days=max(1, _i("BURN_OUTLOOK_DAYS", 7)),
            burn_outlook_window_hours=max(1, _i("BURN_OUTLOOK_WINDOW_HOURS", 48)),
            radar_enabled=_s("RADAR_ENABLED", "1") != "0",
            radar_loop_hours=max(1, _i("RADAR_LOOP_HOURS", 6)),
            radar_bbox=_s("RADAR_BBOX", "-103.5,30.5,-91.5,39.5"),
            radar_warm_zooms=_s("RADAR_WARM_ZOOMS", "6-11"),
            radar_cache_dir=_s("RADAR_CACHE_DIR", ""),
            radar_cache_max_mb=max(1, _i("RADAR_CACHE_MAX_MB", 500)),
            # Clamped to a polite 30s floor for the IEM catalog
            radar_interval=max(30, _i("RADAR_INTERVAL", 90)),
            basemap_path=_s("BASEMAP_PATH", ""),
        )

    @property
    def ugc_codes(self) -> set:
        return {c.strip() for c in self.nws_ugc.split(",") if c.strip()}

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def radar_bbox_tuple(self) -> tuple:
        """(west, south, east, north) floats."""
        w, s, e, n = (float(v) for v in self.radar_bbox.split(","))
        return w, s, e, n

    @property
    def radar_warm_zoom_list(self) -> list:
        """RADAR_WARM_ZOOMS parsed: comma-separated 'a-b' ranges and singles."""
        zooms = set()
        for part in self.radar_warm_zooms.split(","):
            part = part.strip()
            if not part:
                continue
            lo, _, hi = part.partition("-")
            zooms.update(range(int(lo), int(hi or lo) + 1))
        return sorted(zooms)

    def stale_after(self, source: str) -> int:
        """Freshness window per source; data older than this is flagged stale."""
        table = {
            "open_meteo_forecast": self.forecast_interval * 3,
            "open_meteo_nowcast": self.nowcast_interval * 3,
            "pirate_weather": self.pirate_interval * 3,
            "nws_alerts": self.alerts_interval * 5,
            "iem_watchdog": self.watchdog_interval * 5,
            "iem_radar": self.radar_interval * 5,
        }
        return table.get(source, 3600)
