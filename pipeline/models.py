"""
Model registry — what can run each layer, in which tier, at what price.

TIERS. `light` models do the first pass; `flagship` models adjudicate what the
light pass could not agree on. The split exists because spending flagship
capacity uniformly over a sheet spends most of it on tags that any model reads
correctly.

PAIRING RULE. The two light models must come from DIFFERENT providers. Two
models from one family share training data and fail the same way, so their
agreement carries little information. Cross-provider agreement is the signal —
measured on page 1, Opus and GPT-5.5 missed disjoint categories (Opus got
`filters` and missed `sample_sinks`; GPT-5.5 the reverse), and their union
scored higher than either alone.

PRICES are USD per million tokens, checked against the provider's published
rates rather than recalled. A wrong price here produces a confident, wrong
cost model — which is worse than no cost model.
"""

from __future__ import annotations

CATALOG: dict[str, dict] = {
    # ---- Anthropic ------------------------------------------------------
    "claude-opus-5": {
        "provider": "anthropic", "tier": "flagship",
        "in": 5.00, "out": 25.00,
        "note": "Best measured recall on page 1 (0.848). Current default.",
    },
    "claude-sonnet-5": {
        "provider": "anthropic", "tier": "light",
        "in": 2.00, "out": 10.00,
        "note": "2.5x cheaper than Opus 5. Untested on these sheets.",
    },
    "claude-haiku-4-5": {
        "provider": "anthropic", "tier": "light",
        "in": 1.00, "out": 5.00,
        "note": "5x cheaper than Opus 5. 200K context. No `effort` setting.",
        "supports_effort": False,
    },
    # ---- OpenAI ---------------------------------------------------------
    "gpt-5.5": {
        "provider": "openai", "tier": "flagship",
        "in": 5.0, "out": 30.0,
        "note": "Recall 0.758 on page 1. Complementary misses to Opus. Price from third-party pages (apidog.com, 1 Oct 2026); OpenAI's page blocks automated reads.",
    },
    "gpt-5.4-mini": {
        "provider": "openai", "tier": "light",
        "in": None, "out": None,
        "note": "Untested on these sheets. Price unrecorded: sources conflict ($0.25/$2 vs $0.75/$4.50).",
    },
    # ---- Google ---------------------------------------------------------
    "gemini-3.1-pro-preview": {
        "provider": "gemini", "tier": "flagship",
        "in": 2.0, "out": 12.0,
        "note": "Unlocked by billing. Untested on these sheets. Price from ai.google.dev/gemini-api/docs/pricing (1 Oct 2026), prompts <=200k.",
    },
    "gemini-3.8-flash": {
        "provider": "gemini", "tier": "light",
        "in": 0.75, "out": 3.75,
        "note": "51 obs vs Opus 94 on one tile; 5x faster. Price from ai.google.dev (1 Oct 2026); rises to $1.50/$7.50 on 1 Jan 2027.",
    },
}

# Defaults chosen to satisfy the pairing rule above.
DEFAULT_LIGHT_PAIR = ("claude-sonnet-5", "gemini-3.8-flash")
DEFAULT_FLAGSHIP = "claude-opus-5"
DEFAULT_TABLE_MODEL = "claude-sonnet-5"


def spec(model: str) -> dict:
    if model not in CATALOG:
        raise KeyError(f"unknown model {model!r}; known: {sorted(CATALOG)}")
    return CATALOG[model]


def provider_of(model: str) -> str:
    return spec(model)["provider"]


def cost(model: str, usage: dict) -> float | None:
    """USD for one call's usage, or None where the rate is not recorded.

    Returning None rather than 0.0 keeps an unpriced model visibly unpriced
    instead of quietly reporting a free run.
    """
    s = spec(model)
    if s["in"] is None or s["out"] is None:
        return None
    return (usage.get("input_tokens", 0) / 1e6 * s["in"]
            + usage.get("output_tokens", 0) / 1e6 * s["out"])


def by_tier(tier: str) -> list[str]:
    return [m for m, s in CATALOG.items() if s["tier"] == tier]


def validate_pair(a: str, b: str) -> str | None:
    """Returns a warning string if the pair is weak, else None."""
    if a == b:
        return "Both light models are the same — agreement carries no information."
    if provider_of(a) == provider_of(b):
        return (f"Both light models are {provider_of(a)} models. Same-family models "
                f"share blind spots, so agreement between them is a weak signal. "
                f"Prefer one model from each provider.")
    return None
