"""
Shared vision client for Layers B and D.

Two providers behind one function. Switch with an env var, no code change:

    LLM_PROVIDER=anthropic   # default
    LLM_PROVIDER=gemini
    LLM_PROVIDER=openai

`ask_json()` returns (parsed_json, usage) from either, so Layers B and D never
learn which one answered. That is the point: the pipeline's accuracy story
should not be entangled with a vendor, and running the same sheet through both
is how you find out whether a provider is actually better AT THIS TASK rather
than on someone's benchmark.

ANTHROPIC GOTCHAS — each cost real debugging time, each fails in a way that
does not point at its cause:

1. `accept-encoding` override. The SDK ships `httpx2`, whose zstd decoder calls
   `Decompressor.decompress(..., output_buffer_limit=...)`. The installed
   `zstandard` (0.25.0) has no such kwarg, so every request dies as a bare
   `APIConnectionError` — which reads like a network or auth failure and is
   neither. Declining zstd sidesteps the decoder entirely.

2. Thinking blocks. Claude Opus 5 has thinking ON by default, so `content[0]`
   is a ThinkingBlock, not text. `content[0].text` raises AttributeError.
   Always select by block type — `text_of()` below.

3. `max_tokens` is a THINKING + OUTPUT budget. With effort="xhigh" a dense
   tile spends thousands of tokens reasoning before it writes a token of JSON,
   and a budget that runs out mid-string surfaces as `JSONDecodeError:
   Unterminated string` — which reads like a malformed response and is nothing
   of the kind. We check `stop_reason` and say so in as many words.

4. Streaming is not optional at a large budget. The SDK refuses a
   non-streaming request whose `max_tokens` implies it could run past ten
   minutes, and refuses it up front, so every tile fails in seconds. The budget
   needed to stop gotcha 3 is over that line, hence `.stream()`.

GEMINI GOTCHAS:

5. Its `responseSchema` is an OpenAPI-3.0 subset, not JSON Schema.
   `additionalProperties` is rejected outright, so the same schema object has
   to be stripped before it is sent — see `_gemini_schema()`.

6. A blocked or truncated response still returns HTTP 200 with a candidate
   that has no `parts`. Reading `parts[0]` blind raises KeyError and hides the
   real reason, which is in `finishReason`. Check it.

7. Free-tier keys return 503 on image requests under load, and 429 with
   `limit: 0` for Pro models. Neither is a bug in the request. Layer D's retry
   absorbs the transient ones; a hard 429 means that model needs billing.

OPENAI GOTCHAS:

8. `strict: true` structured output requires EVERY property to appear in its
   object's `required` list and `additionalProperties: false` everywhere — a
   stricter contract than Anthropic's. Our schema already satisfies it, but a
   new optional field would be rejected at request time, not silently ignored.

9. Reasoning tokens are billed as output and counted against
   `max_output_tokens`. A budget that looks generous can be consumed entirely
   by reasoning, returning `status: "incomplete"` with EMPTY text — the same
   trap as gotcha 3, wearing different clothes. We check `status`.
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.request

from PIL import Image

ANTHROPIC_MODEL = "claude-opus-5"
GEMINI_MODEL = "gemini-3.8-flash"
OPENAI_MODEL = "gpt-5.5"
from pipeline.paths import ENV_FILE as _ENV
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def load_env() -> None:
    """Populate keys from .env if they aren't already set."""
    if not _ENV.exists():
        return
    for line in _ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def provider() -> str:
    load_env()
    return os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()


def active_model() -> str:
    """Which model a call would actually use — worth recording in run output,
    so a scored result can never be mistaken for one from another model."""
    p = provider()
    if p == "gemini":
        return os.environ.get("GEMINI_MODEL", GEMINI_MODEL)
    if p == "openai":
        return os.environ.get("OPENAI_MODEL", OPENAI_MODEL)
    return os.environ.get("ANTHROPIC_MODEL", ANTHROPIC_MODEL)


def has_key() -> bool:
    """Whether the ACTIVE provider can run. Checking the wrong provider's key
    would report a stage ready that is about to fail on the first call."""
    load_env()
    var = {"gemini": "GEMINI_API_KEY",
           "openai": "OPENAI_API_KEY"}.get(provider(), "ANTHROPIC_API_KEY")
    return bool(os.environ.get(var))


class Truncated(ValueError):
    """The model ran out of output budget mid-answer. Billed, but unusable;
    retrying with the same budget fails the same way."""

    def __init__(self, message: str, usage: dict | None = None):
        super().__init__(message)
        self.usage = usage or {}


def box_scale(model: str | None, width: int, height: int) -> tuple[float, float]:
    """Multipliers that turn a model's box coordinates into pixels of the image
    it was shown. Gemini returns boxes on a 0-1000 scale whatever the image
    size (measured: its largest x in a 1250 px tile was exactly 1000), so
    without this every Gemini box lands shrunk toward the top-left corner."""
    from pipeline.models import CATALOG, provider_of
    m = model or active_model()
    p = provider_of(m) if m in CATALOG else provider()
    return (width / 1000, height / 1000) if p == "gemini" else (1.0, 1.0)


def has_key_for(model: str) -> bool:
    """Whether the provider behind `model` has a key configured."""
    from pipeline.models import CATALOG, provider_of
    load_env()
    p = provider_of(model) if model in CATALOG else provider()
    var = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}.get(p, "ANTHROPIC_API_KEY")
    return bool(os.environ.get(var))


def client():
    import anthropic

    load_env()
    # See gotcha 1 — do not remove the header without re-testing.
    return anthropic.Anthropic(default_headers={"accept-encoding": "gzip, deflate"})


def text_of(message) -> str:
    """First text block. See gotcha 2 — never index content[0] blindly."""
    return next((b.text for b in message.content if b.type == "text"), "")


def encode(img: Image.Image, fmt: str = "PNG") -> dict:
    """Image -> an Anthropic base64 content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": f"image/{fmt.lower()}",
            "data": _b64(img, fmt),
        },
    }


def _b64(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, fmt)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _gemini_schema(s):
    """Strip the keys Gemini's schema subset rejects. See gotcha 5."""
    if isinstance(s, dict):
        return {k: _gemini_schema(v) for k, v in s.items() if k != "additionalProperties"}
    if isinstance(s, list):
        return [_gemini_schema(v) for v in s]
    return s


def ask_json(
    img: Image.Image,
    prompt: str,
    schema: dict,
    *,
    max_tokens: int = 8000,
    effort: str = "high",
    system: str | None = None,
    model: str | None = None,
    tag: dict | None = None,
) -> tuple[dict, dict]:
    """One structured vision call (or text-only when `img` is None).

    `tag` labels the call for live stats, e.g. {"layer": "D", "tile": "t03"}.

    `model` overrides the env-selected provider for this call alone. The
    cascade runs two models CONCURRENTLY, so provider choice cannot live in a
    process-wide env var — mutating it from worker threads would race, and each
    tile would be read by whichever provider happened to be set at that
    instant. Passing the model explicitly is what makes concurrency safe.

    Returns (parsed_json, usage). Structured outputs make malformed JSON
    impossible, which removes a whole class of live-demo failure (§7).
    """
    import time

    from pipeline import stats
    from pipeline.models import CATALOG, cost, provider_of

    model = model or active_model()
    p = provider_of(model) if model in CATALOG else provider()
    entry = {**(tag or {}), "model": model, "provider": p, "effort": effort}
    t0 = time.time()
    try:
        if p == "gemini":
            data, usage = _ask_gemini(img, prompt, schema, max_tokens=max_tokens,
                                      system=system, model=model)
        elif p == "openai":
            data, usage = _ask_openai(img, prompt, schema, max_tokens=max_tokens,
                                      effort=effort, system=system, model=model)
        else:
            data, usage = _ask_anthropic(img, prompt, schema, max_tokens=max_tokens,
                                         effort=effort, system=system, model=model)
    except Exception as exc:
        u = getattr(exc, "usage", {}) or {}
        stats.record({**entry, "status": "error", "latency": round(time.time() - t0, 2),
                      "error": f"{type(exc).__name__}: {exc}"[:160],
                      "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
                      "cost": cost(model, u) if u and model in CATALOG else None})
        raise
    stats.record({**entry, "status": "ok", "latency": round(time.time() - t0, 2),
                  "ttft": usage.get("ttft"),
                  "input_tokens": usage.get("input_tokens", 0),
                  "output_tokens": usage.get("output_tokens", 0),
                  "cost": cost(model, usage) if model in CATALOG else None})
    return data, usage


def _output_config(model: str, effort: str, schema: dict) -> dict:
    """Some models (Haiku 4.5) reject `effort`; leave it out for them."""
    from pipeline.models import CATALOG
    cfg = {"format": {"type": "json_schema", "schema": schema}}
    if CATALOG.get(model, {}).get("supports_effort", True):
        cfg["effort"] = effort
    return cfg


def _ask_anthropic(img, prompt, schema, *, max_tokens, effort, system, model=None) -> tuple[dict, dict]:
    import time

    kw = {}
    if system:
        kw["system"] = system
    content = ([encode(img)] if img is not None else []) + [{"type": "text", "text": prompt}]
    t0, ttft = time.time(), None
    # See gotcha 4 — .stream(), not .create(), or a large budget is rejected.
    with client().messages.stream(
        model=model or active_model(),
        max_tokens=max_tokens,
        output_config=_output_config(model or active_model(), effort, schema),
        messages=[{"role": "user", "content": content}],
        **kw,
    ) as stream:
        # Time to first token counts thinking too: it is when the model
        # starts producing anything, which is what a waiting user feels.
        for ev in stream:
            if ttft is None and ev.type == "content_block_delta":
                ttft = round(time.time() - t0, 2)
        msg = stream.get_final_message()

    if msg.stop_reason == "max_tokens":
        # See gotcha 3 — do not let this reach json.loads(), where it becomes
        # an unrecognisable "Unterminated string". The call is still billed, so
        # its usage travels with the error and is recorded.
        raise Truncated(
            f"response hit max_tokens={max_tokens} and was truncated mid-JSON",
            {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens})
    return json.loads(text_of(msg)), {
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
        "model": msg.model,
        "ttft": ttft,
    }


def _ask_gemini(img, prompt, schema, *, max_tokens, system, model=None) -> tuple[dict, dict]:
    load_env()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
    model = model or active_model()

    body = {
        "contents": [{
            "role": "user",
            "parts": ([{"inline_data": {"mime_type": "image/png", "data": _b64(img)}}]
                      if img is not None else []) + [{"text": prompt}],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(schema),
            "maxOutputTokens": max_tokens,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    req = urllib.request.Request(
        _GEMINI_URL.format(model=model),
        data=json.dumps(body).encode(),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        r = json.load(urllib.request.urlopen(req, timeout=540))
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        # 503/429 here are capacity and quota, not malformed requests — say so,
        # because the raw message reads like a client error. See gotcha 7.
        raise RuntimeError(f"Gemini {model} HTTP {e.code}: {detail}") from None

    cand = (r.get("candidates") or [{}])[0]
    parts = cand.get("content", {}).get("parts")
    if not parts:
        # See gotcha 6 — a 200 with no parts is a block or a truncation.
        raise ValueError(
            f"Gemini {model} returned no content (finishReason="
            f"{cand.get('finishReason')}); likely truncated or blocked"
        )
    if cand.get("finishReason") == "MAX_TOKENS":
        raise ValueError(
            f"Gemini {model} hit maxOutputTokens={max_tokens} and was truncated mid-JSON; "
            f"raise the budget for this call"
        )

    u = r.get("usageMetadata", {})
    return json.loads(parts[0]["text"]), {
        "input_tokens": u.get("promptTokenCount", 0),
        "output_tokens": u.get("candidatesTokenCount", 0),
        "model": model,
    }


def _ask_openai(img, prompt, schema, *, max_tokens, effort, system, model=None) -> tuple[dict, dict]:
    load_env()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
    model = model or active_model()
    # Anthropic's effort ladder has one rung above OpenAI's; clamp rather than
    # send a value the API will reject.
    eff = {"xhigh": "high"}.get(effort, effort)

    body = {
        "model": model,
        "input": [{"role": "user", "content": (
            [{"type": "input_image", "image_url": f"data:image/png;base64,{_b64(img)}"}]
            if img is not None else []) + [{"type": "input_text", "text": prompt}]}],
        "text": {"format": {"type": "json_schema", "name": "observations",
                            "strict": True, "schema": schema}},
        "reasoning": {"effort": eff},
        "max_output_tokens": max_tokens,
    }
    if system:
        body["instructions"] = system

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        r = json.load(urllib.request.urlopen(req, timeout=900))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenAI {model} HTTP {e.code}: {e.read().decode()[:300]}") from None

    if r.get("status") == "incomplete":
        # See gotcha 9 — reasoning ate the budget; text comes back empty.
        why = (r.get("incomplete_details") or {}).get("reason")
        raise ValueError(
            f"OpenAI {model} returned incomplete (reason={why}) at "
            f"max_output_tokens={max_tokens}; raise the budget for this call"
        )
    text = "".join(
        c["text"] for o in r.get("output", []) if o.get("type") == "message"
        for c in o.get("content", []) if c.get("type") == "output_text"
    )
    if not text:
        raise ValueError(f"OpenAI {model} returned no output text (status={r.get('status')})")

    u = r.get("usage", {})
    return json.loads(text), {
        "input_tokens": u.get("input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "model": model,
    }
