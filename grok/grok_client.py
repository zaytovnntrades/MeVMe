#!/usr/bin/env python3
"""
Minimal Grok (xAI) API client - Python standard library only, no installs.

Setup:
    1. Get a key at https://console.x.ai  (API Keys)
    2. export XAI_API_KEY="xai-..."        # never hard-code it in this file

Usage:
    python3 grok_client.py models                       # list models your key can use
    python3 grok_client.py ask "Explain funding rates"  # one question
    python3 grok_client.py ask "..." --stream           # print tokens as they arrive
    python3 grok_client.py respond "Fix this function..." # /v1/responses endpoint
    python3 grok_client.py signal BTCUSDT 97250 "RSI 72, price above 200 EMA"
                                                        # structured JSON trade read

The xAI API is OpenAI-compatible: base URL https://api.x.ai/v1, Bearer auth.

Switching provider (any OpenAI-compatible endpoint works):
    LLM_PROVIDER=xai          (default)  https://api.x.ai/v1      key: XAI_API_KEY         model: grok-4.7
    LLM_PROVIDER=freellmapi              http://localhost:3001/v1 key: FREELLMAPI_API_KEY  model: auto
    LLM_BASE_URL / LLM_API_KEY / LLM_MODEL override any preset field, e.g. a FreeLLMAPI on another host.
    XAI_MODEL is still honoured for the xai preset.

    python3 grok_client.py provider       # show what is active (never prints the key)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PRESETS = {
    "xai": {"base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY",
            "model": os.environ.get("XAI_MODEL", "grok-4.7"), "hint": "xai-..."},
    "freellmapi": {"base_url": "http://localhost:3001/v1", "key_env": "FREELLMAPI_API_KEY",
                   "model": "auto", "hint": "freellmapi-... (Keys page header in the dashboard)"},
}
PROVIDER = os.environ.get("LLM_PROVIDER", "xai").lower()
if PROVIDER not in PRESETS:
    sys.exit(f"LLM_PROVIDER must be one of {', '.join(PRESETS)}, got '{PROVIDER}'.")
_PRESET = PRESETS[PROVIDER]
BASE_URL = os.environ.get("LLM_BASE_URL", _PRESET["base_url"]).rstrip("/")
KEY_ENV = "LLM_API_KEY" if os.environ.get("LLM_API_KEY") else _PRESET["key_env"]
DEFAULT_MODEL = os.environ.get("LLM_MODEL", _PRESET["model"])  # run `models` to see current names
IS_FREE_TIER = PROVIDER == "freellmapi"


def _key() -> str:
    key = os.environ.get(KEY_ENV)
    if not key:
        sys.exit(f"Set {KEY_ENV} first:  export {KEY_ENV}='{_PRESET['hint']}'")
    return key


def describe() -> str:
    return (f"provider={PROVIDER} base_url={BASE_URL} model={DEFAULT_MODEL} "
            f"key={KEY_ENV} ({'set' if os.environ.get(KEY_ENV) else 'MISSING'})")


def _request(method: str, path: str, body: dict | None = None, stream: bool = False,
             timeout: float = 120):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach {BASE_URL}: {e.reason}")
    except TimeoutError:
        sys.exit(f"Timed out after {timeout}s waiting for {BASE_URL}")
    if stream:
        return resp
    with resp:
        return json.loads(resp.read().decode())


def list_models() -> list[str]:
    return [m["id"] for m in _request("GET", "/models")["data"]]


def chat_raw(messages: list[dict], model: str = DEFAULT_MODEL, temperature: float = 0.3,
             json_mode: bool = False, timeout: float = 120) -> dict:
    """Full response. `data["model"]` is the model that actually answered (matters with routers)."""
    body = {"model": model, "messages": messages, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return _request("POST", "/chat/completions", body, timeout=timeout)


def chat(messages: list[dict], model: str = DEFAULT_MODEL, temperature: float = 0.3,
         json_mode: bool = False, timeout: float = 120) -> str:
    data = chat_raw(messages, model, temperature, json_mode, timeout)
    return data["choices"][0]["message"]["content"]


def parse_json_reply(text: str) -> dict:
    """Parse a JSON reply, tolerating ```json fences or prose around it (common on free models)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"no JSON object in reply: {text[:120]!r}")
        return json.loads(text[start:end + 1])


def chat_stream(messages: list[dict], model: str = DEFAULT_MODEL) -> str:
    body = {"model": model, "messages": messages, "stream": True}
    full = []
    with _request("POST", "/chat/completions", body, stream=True) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            delta = json.loads(chunk)["choices"][0]["delta"].get("content") or ""
            print(delta, end="", flush=True)
            full.append(delta)
    print()
    return "".join(full)


def respond(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """POST /v1/responses - the newer single-input endpoint (`input` instead of `messages`)."""
    data = _request("POST", "/responses", {"model": model, "input": prompt})
    if data.get("output_text"):
        return data["output_text"]
    parts = []
    for item in data.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text") and c.get("text"):
                parts.append(c["text"])
    return "\n".join(parts) or json.dumps(data, indent=2)


SIGNAL_SYSTEM = (
    "You are a disciplined crypto trading analyst. Given a symbol, price and "
    "indicator notes, reply ONLY with JSON: "
    '{"bias":"long|short|neutral","confidence":0-100,'
    '"reasons":["..."],"invalidation":"price or condition that kills the idea",'
    '"risk_note":"..."}. Never give certainty; this is not financial advice.'
)


def trade_signal(symbol: str, price: str, notes: str, model: str = DEFAULT_MODEL) -> dict:
    out = chat(
        [{"role": "system", "content": SIGNAL_SYSTEM},
         {"role": "user", "content": f"Symbol: {symbol}\nPrice: {price}\nNotes: {notes}"}],
        model=model, temperature=0.2, json_mode=True,
    )
    return parse_json_reply(out)


def main() -> None:
    p = argparse.ArgumentParser(description="Grok (xAI) API client")
    p.add_argument("--model", default=DEFAULT_MODEL)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models")
    sub.add_parser("provider", help="show the active provider, URL, model and key variable")
    a = sub.add_parser("ask")
    a.add_argument("prompt")
    a.add_argument("--stream", action="store_true")
    r = sub.add_parser("respond", help="use the /v1/responses endpoint")
    r.add_argument("prompt")
    s = sub.add_parser("signal")
    s.add_argument("symbol")
    s.add_argument("price")
    s.add_argument("notes")
    args = p.parse_args()

    if args.cmd == "provider":
        print(describe())
    elif args.cmd == "models":
        print("\n".join(list_models()))
    elif args.cmd == "ask":
        msgs = [{"role": "user", "content": args.prompt}]
        if args.stream:
            chat_stream(msgs, args.model)
        else:
            print(chat(msgs, args.model))
    elif args.cmd == "respond":
        print(respond(args.prompt, args.model))
    elif args.cmd == "signal":
        print(json.dumps(trade_signal(args.symbol, args.price, args.notes, args.model), indent=2))


if __name__ == "__main__":
    main()
