"""Webhook endpoint: authentication, validation, classification, rate limiting, fast acknowledgement."""
import json
import time

import pytest

from conftest import AUTH, SECRET, FakeAnalyst, payload, wait_for


def post(client, body, **kw):
    data = body if isinstance(body, (str, bytes)) else json.dumps(body)
    return client.post("/webhook/tradingview", content=data, headers={"Content-Type": "text/plain", **kw.pop("headers", {})}, **kw)


class TestAuth:
    def test_missing_secret(self, make_client):
        c = make_client()
        body = payload("stock_day_aapl")
        body.pop("secret")
        assert post(c, body).status_code == 401

    def test_wrong_secret(self, make_client):
        assert post(make_client(), payload("stock_day_aapl", secret="wrong-secret-xxxxxxxx")).status_code == 401

    def test_secret_in_header_or_query(self, make_client):
        c = make_client()
        body = payload("stock_day_aapl")
        body.pop("secret")
        assert post(c, body, headers={"X-Webhook-Secret": SECRET}).status_code == 202
        assert post(c, {**body, "signal": "q"}, params={"token": SECRET}).status_code == 202

    def test_secret_never_stored(self, make_client):
        c = make_client()
        aid = post(c, payload("stock_day_aapl")).json()["alert_id"]
        assert "secret" not in c.get(f"/api/alerts/{aid}", headers=AUTH).json()["payload"]
        assert SECRET not in json.dumps(c.get("/api/alerts", headers=AUTH).json())

    def test_ip_allowlist(self, make_client):
        c = make_client(tradingview_ip_allowlist=["52.89.214.238"])
        assert post(c, payload("stock_day_aapl")).status_code == 403


class TestValidation:
    def test_invalid_json(self, make_client):
        r = post(make_client(), "{not json")
        assert r.status_code == 400

    def test_non_object(self, make_client):
        assert post(make_client(), "[1, 2]").status_code == 400

    def test_unknown_field_named_in_error(self, make_client):
        r = post(make_client(), payload("invalid_unknown_field"))
        assert r.status_code == 422
        assert any(e["field"] == "pricee" for e in r.json()["detail"]["errors"])

    def test_bad_timeframe(self, make_client):
        r = post(make_client(), payload("invalid_timeframe"))
        assert r.status_code == 422 and "timeframe" in json.dumps(r.json())

    def test_strategy_timeframe_mismatch(self, make_client):
        r = post(make_client(), payload("stock_day_aapl", strategy="swing"))
        assert r.status_code == 422 and "swing" in r.json()["detail"]["message"]

    @pytest.mark.parametrize("field,value", [("price", -5), ("ticker", "AAPL; DROP"), ("indicators", {"rsi": "high"}),
                                             ("option", {"underlying": "SPY", "right": "both", "strike": 1, "expiry": "2026-12-18"})])
    def test_bad_values(self, make_client, field, value):
        assert post(make_client(), payload("stock_day_aapl", **{field: value})).status_code == 422

    def test_too_large(self, make_client):
        r = post(make_client(), payload("stock_day_aapl", notes="x" * 400) | {"pad": "y" * 40000})
        assert r.status_code == 413


class TestClassification:
    @pytest.mark.parametrize("name,asset,tf,strategy", [
        ("futures_scalp_mnq", "future", "1m", "scalping"),
        ("stock_day_aapl", "stock", "15m", "day"),
        ("crypto_swing_btc", "crypto", "4h", "swing"),
        ("option_swing_spy", "option", "1d", "swing"),
    ])
    def test_each_asset_class(self, make_client, name, asset, tf, strategy):
        r = post(make_client(), payload(name))
        assert r.status_code == 202, r.text
        ack = r.json()
        assert (ack["asset_class"], ack["timeframe"], ack["strategy"]) == (asset, tf, strategy)

    def test_five_minute_defaults_to_scalping_unless_day_requested(self, make_client):
        c = make_client()
        assert post(c, payload("stock_day_aapl", timeframe="5", strategy=None)).json()["strategy"] == "scalping"
        assert post(c, payload("stock_day_aapl", timeframe="5")).json()["strategy"] == "day"

    def test_ticker_exchange_prefix_stripped(self, make_client):
        c = make_client()
        aid = post(c, payload("stock_day_aapl")).json()["alert_id"]
        assert c.get(f"/api/alerts/{aid}", headers=AUTH).json()["ticker"] == "AAPL"


class TestFlow:
    def test_ack_does_not_wait_for_analysis(self, make_client):
        c = make_client(analyst=FakeAnalyst(delay=1.0))
        t = time.time()
        r = post(c, payload("futures_scalp_mnq"))
        assert r.status_code == 202 and time.time() - t < 0.5
        assert c.get(f"/api/alerts/{r.json()['alert_id']}", headers=AUTH).json()["status"] in ("queued", "analyzing")
        assert wait_for(c, r.json()["alert_id"])["status"] == "analyzed"

    def test_duplicate_suppressed(self, make_client):
        c = make_client()
        assert post(c, payload("stock_day_aapl")).json()["status"] == "accepted"
        r = post(c, payload("stock_day_aapl"))
        assert r.status_code == 202 and r.json()["status"] == "duplicate"

    def test_rate_limit(self, make_client):
        c = make_client(rate_limit_per_minute=2)
        codes = [post(c, payload("stock_day_aapl", signal=f"s{i}")).status_code for i in range(3)]
        assert codes == [202, 202, 429]
