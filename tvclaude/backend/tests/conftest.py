import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.analyst import AnalysisError, AnalysisMeta
from app.config import Settings
from app.infra import Telegram
from app.main import create_app
from app.schemas import ClaudeAnalysis

PAYLOADS = Path(__file__).parent / "payloads"
SECRET = "test-webhook-secret-0123456789"
TOKEN = "test-dashboard-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def payload(name: str, **overrides) -> dict:
    data = json.loads((PAYLOADS / f"{name}.json").read_text())
    data["secret"] = SECRET
    data.update(overrides)
    return data


def make_analysis(**kw) -> ClaudeAnalysis:
    """A plausible MNQ long that clears 1:2 at TP2 from the worst edge of the entry zone."""
    base = dict(symbol="MNQ1!", asset_class="future", timeframe="1m", strategy="scalping", market_bias="BULLISH",
                setup="VWAP reclaim after sweep", bullish_conditions=["Price above VWAP"],
                bearish_conditions=["Resistance at 21020"], entry_zone=[20998.0, 21000.0], stop_loss=20990.0,
                take_profit=[21010.0, 21020.0, 21030.0], risk_reward="1:2", setup_quality="B",
                confidence="MODERATE", confirmation=["EMA 9 above EMA 21"], invalidation="Close below 20986",
                reasons_to_avoid=["Into resistance at 21020"], data_used=["price", "vwap"], data_requests=[],
                decision="LONG_SETUP", reasoning="Illustrative.")
    base.update(kw)
    return ClaudeAnalysis(**base)


class FakeAnalyst:
    """Stands in for ClaudeAnalyst: same analyze() signature, no network."""

    def __init__(self, result: ClaudeAnalysis | None = None, error: AnalysisError | None = None, delay: float = 0):
        self.result, self.error, self.delay, self.calls = result or make_analysis(), error, delay, []

    async def analyze(self, context, module, min_rr, missing, rules=None):
        self.calls.append({"context": context, "module": module.name, "min_rr": min_rr, "missing": missing,
                           "rules": rules})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result, AnalysisMeta(model="claude-opus-5-5", latency_ms=5, input_tokens=900, output_tokens=400)


def make_settings(tmp_path, **kw) -> Settings:
    specs = tmp_path / "specs.json"
    specs.write_text(json.dumps({"futures": {"MNQ": {"multiplier": 2, "tick_size": 0.25}},
                                 "options": {"default_multiplier": 100}}))
    base = dict(webhook_secret=SECRET, dashboard_token=TOKEN, database_url=f"sqlite:///{tmp_path}/t.db",
                account_balance=10_000, risk_percent=1.0, contract_specs_file=str(specs),
                rate_limit_per_minute=1000, analysis_workers=1, _env_file=None)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def _make(analyst=None, telegram=None, **settings_kw):
        app = create_app(make_settings(tmp_path, **settings_kw), analyst=analyst or FakeAnalyst(),
                         telegram=telegram or Telegram(None, None))
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def wait_for(client, alert_id: int, timeout: float = 5.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        a = client.get(f"/api/alerts/{alert_id}", headers=AUTH).json()
        if a["status"] in ("analyzed", "failed"):
            return a
        time.sleep(0.02)
    raise AssertionError(f"alert {alert_id} not processed in {timeout}s")


def fake_claude_response(text: str, stop_reason: str = "end_turn", **extra):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)],
                           model=extra.get("model", "claude-opus-5-5"), stop_details=extra.get("stop_details"),
                           usage=SimpleNamespace(input_tokens=1200, output_tokens=500,
                                                 iterations=extra.get("iterations")))
