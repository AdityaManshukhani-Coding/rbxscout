"""prufer.py client tests (ATLAS_PRUFER_HYDRATOR_PLAN.md Phase 1).

Covers the strict-schema validation of an UNTRUSTED response path (free
public proxies), the freshness gate (Atlas drops games mid-history), proxy
pool validation/retirement, retry-once semantics, and the batch refresh
contract. All HTTP is faked — live behavior was proven separately on a real
GitHub Actions runner (2,164-proxy full-pool test: 9.0% yield, 2026-10-04).

Note on config: prufer.py reads its PRUFER_* constants from the environment
at import time. Tests therefore monkeypatch the module attributes directly
(setenv after import is a no-op for them).
"""

import json
import time

import pytest
import requests

import prufer
from prufer import PruferClient


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _series_payload(last_age_hours=1.0, n_points=3, playing=1200, visits=50_000_000):
    """A valid series whose LAST point is `last_age_hours` old (UTC buckets)."""
    now = time.time()
    points = []
    for i in range(n_points):
        ts = now - (last_age_hours * 3600 + (n_points - 1 - i) * 3600)
        points.append(
            {
                "bucket": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)),
                "playing": playing,
                "visits": visits,
                "favorited": 10_000,
                "ratingPercent": 92.3,
            }
        )
    return {"series": points}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Patch prufer's import-time constants directly (env is read at import)."""
    monkeypatch.setattr(prufer, "PRUFER_TIMEOUT", 2)
    monkeypatch.setattr(prufer, "PRUFER_POOL_MIN_SIZE", 1)
    monkeypatch.setattr(prufer, "PRUFER_POOL_VALIDATION_SAMPLE", 4)
    monkeypatch.setattr(prufer, "PRUFER_VALIDATION_THREADS", 2)
    monkeypatch.setattr(prufer, "PRUFER_FETCH_THREADS", 2)
    monkeypatch.setattr(prufer, "PRUFER_MAX_HOURS", 2.0)
    monkeypatch.setattr(prufer, "PRUFER_ENABLED", True)
    monkeypatch.setattr(prufer, "PRUFER_REQUESTS_PER_TICK", 300)


@pytest.fixture()
def client():
    return PruferClient()


def _set_pool(client, proxies=("1.1.1.1:8080", "2.2.2.2:3128", "3.3.3.3:80")):
    client._pool = list(proxies)
    client._pool_cursor = 0
    client._pool_attempted = True


def _fake_get_route(monkeypatch, routes):
    """routes: callables (url, proxies=, params=) -> FakeResponse or None."""
    calls = []

    def fake_get(url, timeout=None, proxies=None, params=None, **kwargs):
        calls.append({"url": url, "proxies": proxies, "params": params})
        for matcher in routes:
            out = matcher(url, proxies=proxies, params=params)
            if out is not None:
                return out
        raise requests.RequestException(f"unrouted: {url}")

    monkeypatch.setattr(prufer.requests, "get", fake_get)
    return calls


# --------------------------------------------------------------------------- #
# Strict payload validation (untrusted proxy path)
# --------------------------------------------------------------------------- #
class TestSeriesPayloadValidation:
    def test_valid_payload_passes(self):
        out = PruferClient._validate_series_payload(FakeResponse(payload=_series_payload()))
        assert out is not None and len(out) == 3
        assert out[-1]["playing"] == 1200

    def test_non_json_rejected(self):
        assert PruferClient._validate_series_payload(FakeResponse(text="<html>403</html>")) is None

    def test_missing_series_rejected(self):
        assert PruferClient._validate_series_payload(FakeResponse(payload={"data": []})) is None

    def test_null_playing_rejected(self):
        """Real Atlas artifact: stale series end with playing:null. A null
        discards the WHOLE payload — partial series corrupt trends."""
        p = _series_payload()
        p["series"][-1]["playing"] = None
        assert PruferClient._validate_series_payload(FakeResponse(payload=p)) is None

    def test_negative_ccu_rejected(self):
        assert PruferClient._validate_series_payload(
            FakeResponse(payload=_series_payload(playing=-5))
        ) is None

    def test_absurd_ccu_rejected(self):
        assert PruferClient._validate_series_payload(
            FakeResponse(payload=_series_payload(playing=99_999_999_999))
        ) is None

    def test_absurd_visits_rejected(self):
        assert PruferClient._validate_series_payload(
            FakeResponse(payload=_series_payload(visits=10_000_000_000_000_000))
        ) is None

    def test_bad_bucket_format_rejected(self):
        p = _series_payload()
        p["series"][0]["bucket"] = "yesterday-ish"
        assert PruferClient._validate_series_payload(FakeResponse(payload=p)) is None

    def test_empty_series_is_none(self):
        assert PruferClient._validate_series_payload(FakeResponse(payload={"series": []})) is None

    def test_non_dict_point_rejected(self):
        assert PruferClient._validate_series_payload(FakeResponse(payload={"series": [1, 2]})) is None

    def test_bucket_ts_parsed_as_utc(self):
        """Buckets are UTC ('...Z'); local-time interpretation would skew the
        freshness gate by the machine's UTC offset."""
        ts = PruferClient._parse_bucket_ts("2026-10-04T12:00:00.000Z")
        import calendar
        import datetime as dt
        expected = calendar.timegm(dt.datetime(2026, 10, 4, 12, 0, 0).timetuple())
        assert ts == expected


# --------------------------------------------------------------------------- #
# Freshness gate — the heart of the design
# --------------------------------------------------------------------------- #
class TestFreshnessGate:
    def _route_series(self, monkeypatch, payload):
        return _fake_get_route(
            monkeypatch,
            [lambda url, proxies=None, params=None: FakeResponse(payload=payload)],
        )

    def test_fresh_point_passes(self, client, monkeypatch):
        _set_pool(client)
        self._route_series(monkeypatch, _series_payload(last_age_hours=1.0))
        stats = client.fresh_stats(994732206)
        assert stats is not None
        assert stats["ccu"] == 1200
        assert stats["visits"] == 50_000_000

    def test_stale_point_fails_the_gate(self, client, monkeypatch):
        """The Steal-An-Egg case: tracker dropped the game weeks ago."""
        _set_pool(client)
        self._route_series(monkeypatch, _series_payload(last_age_hours=24 * 30))
        assert client.fresh_stats(994732206) is None

    def test_gate_boundary_respects_override(self, client, monkeypatch):
        monkeypatch.setattr(prufer, "PRUFER_MAX_HOURS", 3.0)
        _set_pool(client)
        self._route_series(monkeypatch, _series_payload(last_age_hours=2.5))
        assert client.fresh_stats(994732206) is not None

    def test_future_bucket_rejected(self, client, monkeypatch):
        """A tampering proxy claiming data from the future."""
        _set_pool(client)
        self._route_series(monkeypatch, _series_payload(last_age_hours=-5))
        assert client.fresh_stats(994732206) is None

    def test_no_pool_means_no_fetch(self, client):
        assert client.fresh_stats(994732206) is None

    def test_empty_series_gives_no_stats(self, client, monkeypatch):
        _set_pool(client)
        self._route_series(monkeypatch, {"series": []})
        assert client.fresh_stats(994732206) is None

    def test_disabled_client_gives_no_stats(self, client, monkeypatch):
        monkeypatch.setattr(prufer, "PRUFER_ENABLED", False)
        _set_pool(client)
        assert client.fresh_stats(994732206) is None
        assert client.fetch_series(994732206) is None


# --------------------------------------------------------------------------- #
# Proxy pool lifecycle
# --------------------------------------------------------------------------- #
class TestProxyPool:
    def test_ensure_pool_validates_and_keeps_working(self, client, monkeypatch):
        """List fetch -> canary validation; only working proxies survive."""
        monkeypatch.setattr(
            prufer, "PRUFER_PROXY_LISTS", ["https://lists.test/http.txt"]
        )

        def fake_get(url, timeout=None, proxies=None, params=None, **kw):
            if "lists.test" in url:
                return FakeResponse(text="1.1.1.1:8080\n2.2.2.2:80\n3.3.3.3:80\n4.4.4.4:80")
            # canary: odd-octet proxies work, even ones die
            if proxies and proxies["http"].split("//")[1].startswith(("1.", "3.")):
                return FakeResponse(payload=_series_payload(n_points=1))
            raise requests.RequestException("dead proxy")

        monkeypatch.setattr(prufer.requests, "get", fake_get)
        assert client.ensure_pool(min_size=2) is True
        assert sorted(client._pool) == ["1.1.1.1:8080", "3.3.3.3:80"]

    def test_ensure_pool_fails_when_too_few_work(self, client, monkeypatch):
        monkeypatch.setattr(prufer, "PRUFER_PROXY_LISTS", ["https://lists.test/http.txt"])
        monkeypatch.setattr(prufer, "PRUFER_POOL_MIN_SIZE", 5)

        def fake_get(url, timeout=None, proxies=None, params=None, **kw):
            if "lists.test" in url:
                return FakeResponse(text="1.1.1.1:80\n2.2.2.2:80")
            return FakeResponse(payload=_series_payload(n_points=1))

        monkeypatch.setattr(prufer.requests, "get", fake_get)
        assert client.ensure_pool() is False
        assert client.pool_size == 2  # still kept for opportunistic use

    def test_pool_attempted_only_once(self, client, monkeypatch):
        _set_pool(client)
        n = {"v": 0}

        def counting_get(url, **kw):
            n["v"] += 1
            raise requests.RequestException("no network in this test")

        monkeypatch.setattr(prufer.requests, "get", counting_get)
        assert client.ensure_pool() is True  # pool already populated
        assert n["v"] == 0

    def test_burned_proxy_retired_on_403(self, client, monkeypatch):
        _set_pool(client, proxies=("bad.proxy:80", "good.proxy:80"))

        def route(url, proxies=None, params=None):
            if proxies and proxies["http"].startswith("http://bad.proxy"):
                return FakeResponse(status_code=403, text="forbidden")
            return FakeResponse(payload=_series_payload())

        _fake_get_route(monkeypatch, [route])
        out = client.fetch_series(994732206)
        assert out is not None
        assert "bad.proxy:80" not in client._pool
        assert "good.proxy:80" in client._pool

    def test_tampered_body_retires_proxy(self, client, monkeypatch):
        _set_pool(client, proxies=("evil.proxy:80", "kind.proxy:80"))

        def route(url, proxies=None, params=None):
            if proxies and proxies["http"].startswith("http://evil.proxy"):
                return FakeResponse(payload={"series": [{"bucket": "garbage"}]})
            return FakeResponse(payload=_series_payload())

        _fake_get_route(monkeypatch, [route])
        assert client.fetch_series(994732206) is not None
        assert "evil.proxy:80" not in client._pool

    def test_unroutable_network_returns_none(self, client, monkeypatch):
        _set_pool(client, proxies=("a:1", "b:2"))
        _fake_get_route(
            monkeypatch,
            [lambda url, proxies=None, params=None: FakeResponse(status_code=500, text="x")],
        )
        assert client.fetch_series(994732206) is None

    def test_rotation_round_robin(self, client):
        _set_pool(client, proxies=("a:1", "b:2", "c:3"))
        picks = [client._next_proxy() for _ in range(6)]
        assert picks == ["a:1", "b:2", "c:3", "a:1", "b:2", "c:3"]


# --------------------------------------------------------------------------- #
# Batch refresh contract
# --------------------------------------------------------------------------- #
class TestRefreshBatch:
    def test_batch_returns_only_gated_games(self, client, monkeypatch):
        _set_pool(client)

        def route(url, proxies=None, params=None):
            uid = int(url.rsplit("/", 1)[1])
            if uid % 2 == 0:
                return FakeResponse(payload=_series_payload(last_age_hours=1.0))
            return FakeResponse(payload=_series_payload(last_age_hours=24 * 20))  # stale

        _fake_get_route(monkeypatch, [route])
        out = client.refresh_batch([100, 101, 102, 103])
        assert sorted(out) == [100, 102]
        assert out[100]["ccu"] == 1200

    def test_batch_respects_request_cap(self, client, monkeypatch):
        _set_pool(client)
        seen = []

        def route(url, proxies=None, params=None):
            seen.append(url)
            return FakeResponse(payload=_series_payload())

        _fake_get_route(monkeypatch, [route])
        client.refresh_batch([1, 2, 3, 4, 5], limit=2)
        assert len(seen) == 2

    def test_batch_zero_cap_means_no_requests(self, client, monkeypatch):
        monkeypatch.setattr(prufer, "PRUFER_REQUESTS_PER_TICK", 0)
        _set_pool(client)
        seen = []
        _fake_get_route(
            monkeypatch,
            [
                lambda url, proxies=None, params=None: (
                    seen.append(url) or FakeResponse(payload=_series_payload())
                )
            ],
        )
        assert client.refresh_batch([1, 2, 3]) == {}
        assert seen == []

    def test_batch_with_empty_pool_returns_empty(self, client, monkeypatch):
        """No proxies validate -> the whole pass is a clean no-op (fail-open)."""
        monkeypatch.setattr(prufer, "PRUFER_PROXY_LISTS", ["https://lists.test/http.txt"])

        def fake_get(url, timeout=None, proxies=None, params=None, **kw):
            if "lists.test" in url:
                return FakeResponse(text="1.1.1.1:80")
            raise requests.RequestException("canary dead")

        monkeypatch.setattr(prufer.requests, "get", fake_get)
        assert client.refresh_batch([1, 2, 3]) == {}
