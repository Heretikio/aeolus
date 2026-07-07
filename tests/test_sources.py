"""Source fetcher unit tests with a stubbed transport. No network."""

import traceback

import pytest
import requests

import sources


class FakeResp:
    def __init__(self, status=200, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_normalize_block_tolerates_schema_drift():
    raw = {"hourly": {"time": ["t0", "t1", "t2"],
                      "temperature_2m": [70.0, 71.0, 72.0],
                      "uv_index": [1.0],           # drift: array shorter than time
                      "weather_code": "notalist"}}  # drift: wrong type
    rows = sources.normalize_block(
        raw, "hourly", ["temperature_2m", "uv_index", "weather_code"])
    assert rows[0][1] == {"temperature_2m": 70.0, "uv_index": 1.0}
    assert rows[2][1] == {"temperature_2m": 72.0}


def test_nws_alerts_conditional_get(cfg, store, monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(headers or {}))
        if "If-None-Match" in (headers or {}):
            return FakeResp(status=304)
        return FakeResp(
            headers={"ETag": '"abc123"',
                     "Last-Modified": "Thu, 03 Jul 2026 18:00:00 GMT"},
            payload={"features": [{"properties": {"id": "x"}}]},
        )

    monkeypatch.setattr(sources.requests, "get", fake_get)

    first = sources.fetch_nws_alerts(cfg, store)
    assert len(first) == 1
    assert store.meta_get("nws_alerts_etag") == '"abc123"'

    second = sources.fetch_nws_alerts(cfg, store)
    assert second is None  # 304: feed unchanged
    assert calls[1]["If-None-Match"] == '"abc123"'
    assert calls[1]["If-Modified-Since"] == "Thu, 03 Jul 2026 18:00:00 GMT"

    # Without a store (live smoke path) the fetch stays unconditional
    assert "If-None-Match" not in calls[0]


def test_pirate_key_never_reaches_poller_logs(cfg, monkeypatch):
    """Failures repeat every backoff cycle into the logs, so neither the
    exception message nor its traceback chain may carry the key."""
    cfg.pirate_weather_key = "SECRETKEY123"

    def boom(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError(f"kaboom for url: {url}")

    monkeypatch.setattr(sources.requests, "get", boom)
    with pytest.raises(RuntimeError) as excinfo:
        sources.fetch_pirate(cfg)
    rendered = "".join(traceback.format_exception(
        type(excinfo.value), excinfo.value, excinfo.value.__traceback__))
    assert "SECRETKEY123" not in rendered
    assert "***" in str(excinfo.value)


def test_http_error_status_redacted(cfg, monkeypatch):
    cfg.pirate_weather_key = "SECRETKEY123"
    monkeypatch.setattr(sources.requests, "get",
                        lambda *a, **k: FakeResp(status=403))
    with pytest.raises(RuntimeError) as excinfo:
        sources.fetch_pirate(cfg)
    assert "HTTP 403" in str(excinfo.value)
    assert "SECRETKEY123" not in str(excinfo.value)
