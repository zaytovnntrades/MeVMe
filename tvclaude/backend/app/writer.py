"""Claude as a writer for the coordinator's daily summary and the Journal's weekly review.

It only ever sees the facts the system computed. It is told not to add numbers, and its output goes into a
strict JSON schema. If it fails, the reports still go out with the computed facts alone.
"""
import json
import logging

import anthropic

log = logging.getLogger("tvclaude.writer")

SYSTEM = """You write short, plain-English reviews for one futures trader who practises with paper trades. \
You get a JSON object of facts the system computed. Use ONLY those facts: never add a number, price, \
percentage or event that is not in them, and never predict the market. If the facts are thin, say so. \
Be direct and specific, like a trading coach who protects capital first. No financial advice, no hype, \
no emoji. Short sentences."""

SCHEMAS = {
    "daily": {"type": "object", "additionalProperties": False, "required": ["summary"],
              "properties": {"summary": {"type": "string"}}},
    "weekly": {"type": "object", "additionalProperties": False,
               "required": ["headline", "what_worked", "what_to_fix", "one_change"],
               "properties": {"headline": {"type": "string"},
                              "what_worked": {"type": "array", "items": {"type": "string"}},
                              "what_to_fix": {"type": "array", "items": {"type": "string"}},
                              "one_change": {"type": "string"}}},
}
ASK = {
    "daily": "Write `summary`: at most 3 sentences on what happened today and what it means for tomorrow.",
    "weekly": "Write the weekly review: a one-line `headline`, up to 3 `what_worked` and 3 `what_to_fix` items "
              "(one sentence each), and `one_change`: the single most useful change for next week.",
}


class ClaudeWriter:
    def __init__(self, model: str, timeout_s: float = 60, client: anthropic.AsyncAnthropic | None = None,
                 api_key: str | None = None):
        self.model, self.timeout_s = model, timeout_s
        self.client = client or (anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic())

    async def write(self, kind: str, facts: dict) -> dict | None:
        try:
            resp = await self.client.with_options(timeout=self.timeout_s).beta.messages.create(
                model=self.model, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMAS[kind]}},
                system=SYSTEM,
                messages=[{"role": "user", "content": f"{ASK[kind]}\n\nFACTS:\n```json\n"
                                                      f"{json.dumps(facts, indent=2, sort_keys=True, default=str)}\n```"}])
            if resp.stop_reason in ("refusal", "max_tokens"):
                log.warning("writer stopped: %s", resp.stop_reason)
                return None
            text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
            return json.loads(text) if text else None
        except (anthropic.APIError, json.JSONDecodeError, TypeError) as e:
            log.warning("writer failed: %s", type(e).__name__)
            return None
