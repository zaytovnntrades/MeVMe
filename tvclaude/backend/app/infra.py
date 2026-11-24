"""Queue, rate limiting, duplicate suppression, WebSocket broadcast and Telegram.
Every piece works in memory; with REDIS_URL set, queue, rate limit and dedup move to Redis so jobs
survive restarts and several API processes share limits."""
import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict, deque

import httpx
from fastapi import WebSocket

log = logging.getLogger("tvclaude.infra")


class MemoryQueue:
    def __init__(self):
        self.q: asyncio.Queue[int] = asyncio.Queue()

    async def put(self, alert_id: int) -> None:
        await self.q.put(alert_id)

    async def get(self) -> int:
        return await self.q.get()

    async def depth(self) -> int:
        return self.q.qsize()


class RedisQueue:
    KEY = "tvclaude:jobs"

    def __init__(self, redis):
        self.r = redis

    async def put(self, alert_id: int) -> None:
        await self.r.lpush(self.KEY, alert_id)

    async def get(self) -> int:
        while True:
            item = await self.r.brpop(self.KEY, timeout=5)
            if item:
                return int(item[1])

    async def depth(self) -> int:
        return int(await self.r.llen(self.KEY))


class RateLimiter:
    """Sliding one-minute window per key."""

    def __init__(self, per_minute: int, redis=None):
        self.limit, self.r = per_minute, redis
        self.hits: dict[str, deque] = defaultdict(deque)

    async def allow(self, key: str) -> bool:
        now = time.time()
        if self.r is not None:
            k = f"tvclaude:rl:{key}:{int(now // 60)}"
            n = await self.r.incr(k)
            if n == 1:
                await self.r.expire(k, 70)
            return n <= self.limit
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class Deduper:
    """The same alert body twice within `window` seconds is accepted once (TradingView can re-send)."""

    def __init__(self, window: int = 60, redis=None):
        self.window, self.r, self.seen = window, redis, {}

    async def first_time(self, payload: dict) -> bool:
        h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if self.r is not None:
            return bool(await self.r.set(f"tvclaude:dedup:{h}", 1, nx=True, ex=self.window))
        now = time.time()
        self.seen = {k: t for k, t in self.seen.items() if now - t < self.window}
        if h in self.seen:
            return False
        self.seen[h] = now
        return True


class Hub:
    """WebSocket broadcast to every connected dashboard."""

    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def add(self, ws: WebSocket) -> None:
        self.clients.add(ws)

    def remove(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, event: str, data: dict) -> None:
        msg = json.dumps({"event": event, "data": data}, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.remove(ws)


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None, transport: httpx.AsyncBaseTransport | None = None):
        self.token, self.chat_id, self.transport = token, chat_id, transport

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=10, transport=self.transport) as c:
                r = await c.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                 json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})
            if r.status_code != 200:
                log.warning("telegram returned HTTP %s", r.status_code)  # never log the token-bearing URL
                return False
            return True
        except httpx.HTTPError as e:
            log.warning("telegram send failed: %s", type(e).__name__)
            return False


def format_telegram(analysis: dict, risk: dict | None) -> str:
    a = analysis
    lines = [f"[PAPER] {a['symbol']} {a['timeframe']} ({a['strategy']}) - {a['decision']}",
             f"Bias {a['market_bias']} | {a['setup']} | quality {a['setup_quality']} | confidence {a['confidence']}"]
    if a.get("entry_zone"):
        lines.append(f"Entry {a['entry_zone']}  SL {a['stop_loss']}  TP {a['take_profit']}")
    if risk:
        rr = risk.get("reward_risk") or []
        lines.append(f"Risk: {risk['verdict']}" + (f" | R:R {rr}" if rr else "") +
                     (f" | size {risk['quantity']} {risk['unit']}, max loss {risk['max_loss']}" if risk.get("quantity") else ""))
    lines.append(f"Invalidation: {a['invalidation']}")
    lines.append("Analysis only - not financial advice. No order was placed.")
    return "\n".join(lines)
