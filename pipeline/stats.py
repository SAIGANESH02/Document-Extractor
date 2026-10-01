"""
Live per-call statistics for every model call.

`llm.ask_json` records one entry per call — layer, model, tokens, cost,
latency, time to first token — so the viewer can show what each layer costs
while it runs, and a run record keeps the numbers afterwards. Calls happen on
worker threads, so the recorder is a lock-guarded list rather than anything
thread-local.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_calls: list[dict] = []
_t0 = [time.time()]


def reset() -> None:
    with _lock:
        _calls.clear()
        _t0[0] = time.time()


def record(entry: dict) -> None:
    with _lock:
        entry["at"] = round(time.time() - _t0[0], 2)   # seconds since the run started
        _calls.append(entry)


def snapshot() -> dict:
    """All calls so far plus totals per layer and overall."""
    with _lock:
        calls = [dict(c) for c in _calls]
    layers: dict[str, dict] = {}
    for c in calls:
        L = layers.setdefault(c.get("layer", "?"), {
            "calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0,
            "cost": 0.0, "unpriced_calls": 0, "latency_sum": 0.0, "ttft": [], "models": []})
        L["calls"] += 1
        L["errors"] += c.get("status") == "error"
        L["input_tokens"] += c.get("input_tokens") or 0
        L["output_tokens"] += c.get("output_tokens") or 0
        if c.get("cost") is None:
            L["unpriced_calls"] += c.get("status") != "error"
        else:
            L["cost"] += c["cost"]
        L["latency_sum"] += c.get("latency") or 0
        if c.get("ttft") is not None:
            L["ttft"].append(c["ttft"])
        if c.get("model") and c["model"] not in L["models"]:
            L["models"].append(c["model"])
    for L in layers.values():
        L["avg_latency"] = round(L.pop("latency_sum") / L["calls"], 2) if L["calls"] else None
        t = L.pop("ttft")
        L["avg_ttft"] = round(sum(t) / len(t), 2) if t else None
        L["cost"] = round(L["cost"], 4)
    total_cost = round(sum(L["cost"] for L in layers.values()), 4)
    return {"calls": calls, "layers": layers, "total_cost": total_cost,
            "total_calls": len(calls),
            "unpriced_calls": sum(L["unpriced_calls"] for L in layers.values())}
