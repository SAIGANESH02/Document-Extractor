"""
Pipeline orchestrator — runs the layers and emits progress events.

    A ingest → B sheet context → ( C detect ∥ D observe ∥ T tables ) → E resolve → score

C, D and T depend only on A's tiles and B's context, never on each other, so
they start at the same moment on their own threads. Their progress is merged
into one event stream through a queue, which is what lets the viewer show all
three moving at once.

Stage status is reported honestly. A stage that cannot run says why —
`blocked` (no API key), `error` (it failed, with the message) — because a demo
that silently pretends to run a stage is worse than one that says what it is.
That applies to caches too: an empty cached result is treated as absent rather
than as "zero observations", which is a failure that looks like a clean run.
Every reused result says when it was made and by which model.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import stats  # noqa: E402
from pipeline.detect import LayerC, Region, tile_grid  # noqa: E402
from pipeline.llm import active_model, has_key_for  # noqa: E402
from pipeline.models import DEFAULT_FLAGSHIP, DEFAULT_LIGHT_PAIR, DEFAULT_TABLE_MODEL, cost  # noqa: E402
from pipeline.paths import BENCH, CACHE, RENDERS, RUNS  # noqa: E402

STAGES = [
    ("A", "Ingest", "Native raster, tile grid, text-layer probe"),
    ("B", "Sheet context", "Legend, symbol dictionary, note-block boxes"),
    ("C", "Detection", "Independent text detection — what did D miss?"),
    ("D", "Observe", "Per-tile symbol + tag observations"),
    ("T", "Tables", "Find tables, read each one cell by cell"),
    ("V", "Verify", "Visual yes/no check for shape-defined categories"),
    ("E", "Resolve", "Dedupe, suppress, map to categories by rule"),
]
PARALLEL = ("C", "D", "T")


@dataclass
class StageResult:
    id: str
    name: str
    detail: str
    status: str = "waiting"  # waiting|running|done|pending|blocked|error
    seconds: float | None = None
    note: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class RunConfig:
    """Which model runs which layer. Layer B defaults to the strongest model:
    it is one call per sheet, and every later layer inherits its mistakes."""
    model_b: str = ""
    d_mode: str = "single"            # single | cascade
    model_d: str = ""
    light_a: str = DEFAULT_LIGHT_PAIR[0]
    light_b: str = DEFAULT_LIGHT_PAIR[1]
    flagship: str = DEFAULT_FLAGSHIP
    model_t: str = ""
    # "Reproduce every table on the sheet": a missing table fails that outright,
    # an extra one clearly labelled costs cents. So title-block, revision and
    # reference-list tables are read too, labelled by kind. Set False to skip.
    include_admin_tables: bool = True
    # Tables are read twice by different models and voted cell by cell; a third
    # model breaks ties. Each misreads different glyphs (Opus V->Y, Sonnet 1->I
    # on one crop), so two different readers catch what one strong reader repeats.
    model_t2: str = "gemini-3.8-flash"
    model_t3: str = "gemini-3.1-pro-preview"
    # Load whatever is saved for this page and call no model at all: what the
    # viewer does when a page is opened, so saved results show up immediately.
    cache_only: bool = False
    # Layer V: the model that answers yes/no questions about cropped symbols.
    model_v: str = ""

    def __post_init__(self):
        self.model_b = self.model_b or os.environ.get("LAYER_B_MODEL") or DEFAULT_FLAGSHIP
        self.model_d = self.model_d or active_model()
        # Tables: Sonnet 5 read all 62 checked cells right on both benchmark runs;
        # Opus read the drain-valve V's as Y every time (data/bench/benchmark.md).
        self.model_t = self.model_t or DEFAULT_TABLE_MODEL
        self.model_v = self.model_v or DEFAULT_FLAGSHIP

    def d_label(self) -> str:
        if self.d_mode == "cascade":
            return f"cascade: {self.light_a} + {self.light_b} → {self.flagship}"
        return self.model_d

    def d_models(self) -> list[str]:
        return [self.light_a, self.light_b, self.flagship] if self.d_mode == "cascade" else [self.model_d]


def list_pages() -> list[dict]:
    out = []
    for p in sorted(RENDERS.glob("page*_full.png"), key=lambda p: int(p.stem[4:-5])):
        pg = int(p.stem[4:-5])
        with Image.open(p) as im:
            w, h = im.size
        meta_f = RENDERS / f"page{pg}_meta.json"
        meta = json.load(open(meta_f)) if meta_f.exists() else {}
        out.append({
            "page": pg, "path": str(p), "width": w, "height": h,
            "source_pdf": meta.get("source_pdf"),
            "text_layer_chars": meta.get("text_layer_chars"),
            "cached": (CACHE / f"page{pg}_regions.json").exists()
            or (BENCH / f"page{pg}_regions.json").exists(),
        })
    return out


# ---- cache helpers ---------------------------------------------------------

def _slug(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "-" for c in s)


def _stamp(path: Path) -> dict:
    """Where a cached result came from and when — shown in the UI so a reused
    result is never mistaken for a fresh one."""
    when = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return {"cached": True, "cache_file": path.name, "cached_at": when.strftime("%d %b %H:%M")}


def _ctx_path(page: int, model: str) -> Path:
    """Layer B results are keyed by model: switching models must not reuse
    another model's legend read. The pre-split file is always Claude Opus's."""
    path = CACHE / f"page{page}_context__{_slug(model)}.json"
    legacy = CACHE / f"page{page}_context.json"
    if not path.exists() and legacy.exists() and model == "claude-opus-5":
        return legacy
    return path


def _obs_slug(cfg: RunConfig) -> str:
    from pipeline.observe import PROMPT_FINGERPRINT as fp
    if cfg.d_mode == "cascade":
        return _slug(f"cascade--{cfg.light_a}--{cfg.light_b}--{cfg.flagship}--p{fp}")
    return _slug(f"{cfg.model_d}--p{fp}")


def _obs_path(page: int, cfg: RunConfig) -> Path:
    """Observation caches are keyed by model (or cascade trio). One filename
    for every provider would let one model's run overwrite another's."""
    return CACHE / f"page{page}_observations__{_obs_slug(cfg)}.json"


def _older_version(path: Path, prefix: str) -> Path | None:
    """The newest saved result for the same page and model(s) made under an
    earlier prompt or reader version. Shown — labelled as older — rather than
    hidden: results already paid for stay visible; Re-run fresh replaces them."""
    if path.exists():
        return path
    olds = sorted((f for f in CACHE.glob(prefix + "*.json") if f != path),
                  key=lambda f: f.stat().st_mtime, reverse=True)
    return olds[0] if olds else None


def _cached_observations(path: Path) -> tuple[list[dict], dict] | None:
    """(observations, meta) or None. A partial cache is kept AND labelled —
    discarding fourteen good tiles because one failed wastes real money, and
    reusing it silently is how an empty file looks like a clean run."""
    if not path.exists():
        return None
    blob = json.load(open(path))
    obs, meta = (blob, {}) if isinstance(blob, list) else (blob.get("observations", []), blob.get("meta", {}))
    return (obs, meta) if obs else None


def _cached_regions(page: int) -> tuple[list[dict], Path] | None:
    for cand in (CACHE / f"page{page}_regions.json", BENCH / f"page{page}_regions.json"):
        if cand.exists():
            return json.load(open(cand)), cand
    return None


def _cost_then(meta: dict, model: str) -> float | None:
    """What a cached Layer D result cost when it was made, from its stored tokens."""
    if not meta.get("input_tokens"):
        return None
    try:
        return cost(model, meta)
    except KeyError:
        return None


# ---- parallel workers ----------------------------------------------------------
# Each worker reports through `put(stage_id, **fields)` and leaves its result
# in `out`. A failure becomes an `error` stage, so one broken layer cannot
# take the other two down with it.

Put = Callable[..., None]


def _layer_c(page: int, img: Image.Image, tiles, force: bool, cache_only: bool,
             put: Put, out: dict) -> None:
    t = time.time()
    hit = None if force else _cached_regions(page)
    if hit:
        st = _stamp(hit[1])
        out["regions"] = hit[0]
        put("C", status="done", seconds=0.0,
            note=f"{len(hit[0])} regions · from cache, saved {st['cached_at']}",
            data={"regions": hit[0], **st})
        return
    if cache_only:
        put("C", status="pending", note="not saved for this page yet — press Run")
        return
    put("C", status="running", note=f"0/{len(tiles)} tiles")
    from pipeline.detect import dedupe
    det = LayerC(mode="detect")
    raw: list[Region] = []
    for i, b in enumerate(tiles):
        raw.extend(det.detect_tile(img, b, tile_id=f"t{i:02d}"))
        put("C", status="running", note=f"{i + 1}/{len(tiles)} tiles · {len(raw)} regions",
            data={"progress": (i + 1) / len(tiles)})
    regions = [r.to_dict() for r in dedupe(raw)]
    json.dump(regions, open(CACHE / f"page{page}_regions.json", "w"))
    out["regions"] = regions
    put("C", status="done", seconds=round(time.time() - t, 2),
        note=f"{len(regions)} regions ({len(raw) - len(regions)} dupes dropped)",
        data={"regions": regions, "cached": False})


def _layer_d(page: int, img: Image.Image, tiles, ctx: dict, cfg: RunConfig, force: bool,
             put: Put, out: dict) -> None:
    out["observations"], out["meta"] = [], {}
    missing = [m for m in cfg.d_models() if not has_key_for(m)]
    if missing:
        put("D", status="blocked", note=f"no API key for {', '.join(missing)}")
        return
    exact = _obs_path(page, cfg)
    base = exact.name.rsplit("--p", 1)[0]          # same page + model(s), any prompt version
    path = (None if force else _older_version(exact, f"page{page}_observations__{base}")) or exact
    if path == exact and not exact.exists() and cfg.d_mode == "single":
        legacy = CACHE / f"page{page}_observations__{_slug(cfg.model_d)}.json"
        path = legacy if legacy.exists() and not force else exact
    hit = None if force else _cached_observations(path)
    if hit and hit[1].get("tiles_failed"):
        # A partial cache must not become a ceiling: re-run to try to better it.
        hit = None
    if hit:
        obs, meta = hit
        st = _stamp(path)
        then = _cost_then(meta, cfg.model_d) if cfg.d_mode == "single" else meta.get("cost_total")
        out.update(observations=obs, meta=meta, cached=True, cost_then=then)
        older = " · OLDER PROMPT VERSION — Re-run fresh to update" if path != exact else ""
        put("D", status="done", seconds=0.0,
            note=f"{len(obs)} observations · {cfg.d_label()} · from cache, saved {st['cached_at']}{older}",
            data={"observations": obs, "meta": meta, "cost_then": then,
                  "errors": meta.get("error_messages", []), **st})
        return

    if cfg.cache_only:
        put("D", status="pending", note=f"not saved for {cfg.d_label()} yet — press Run")
        return
    t = time.time()
    put("D", status="running", note=f"0/{len(tiles)} tiles · {cfg.d_label()}")
    try:
        if cfg.d_mode == "cascade":
            from pipeline.cascade import run as cascade_run
            seen: dict[str, tuple[int, int]] = {}

            def prog(stage, d, n, o):
                seen[stage] = (d, n)
                done = sum(x[0] for x in seen.values())
                total = sum(x[1] for x in seen.values())
                put("D", status="running",
                    note=" · ".join(f"{k}: {v[0]}/{v[1]}" for k, v in seen.items()),
                    data={"progress": done / total if total else 0})

            obs, rep = cascade_run(img, tiles, ctx, cfg.light_a, cfg.light_b, cfg.flagship,
                                   on_progress=prog)
            light_fail = sum(u.get("errors", 0) for s in rep["stages"] if s["stage"] == "light"
                             for u in s.get("usage", {}).values())
            meta = {"mode": "cascade", "tiles_total": len(tiles), "tiles_failed": 0,
                    "light_tile_failures": light_fail,
                    "cascade": {k: rep.get(k) for k in ("escalation", "merge", "cost", "pairing_warning")},
                    "cost_total": rep["cost"].get("total")}
        else:
            from pipeline.observe import observe_page
            obs, usage = observe_page(
                img, tiles, ctx, model=cfg.model_d,
                on_progress=lambda d, n, o: put("D", status="running",
                                                note=f"{d}/{n} tiles · {o} observations",
                                                data={"progress": d / n}))
            meta = {"tiles_total": len(tiles), "tiles_ok": usage.get("calls", 0),
                    "tiles_failed": usage.get("errors", 0),
                    "error_messages": usage.get("error_messages", []),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0)}
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed
        put("D", status="error", note=str(exc)[:160])
        return

    meta["model"] = cfg.d_label()
    if obs:
        json.dump({"observations": obs, "meta": meta}, open(path, "w"), indent=1)
    failed = meta.get("tiles_failed", 0)
    note = f"{len(obs)} observations · {cfg.d_label()}"
    if failed and obs:
        note += f" · {failed}/{len(tiles)} TILE(S) FAILED — cached as PARTIAL"
    elif failed:
        note += f" · ALL {failed}/{len(tiles)} TILES FAILED — nothing cached"
    out.update(observations=obs, meta=meta, cached=False)
    put("D", status="error" if failed else "done", seconds=round(time.time() - t, 2), note=note,
        data={"observations": obs, "meta": meta, "cached": False,
              "errors": meta.get("error_messages", [])})


ADMIN_ZONES = ("title_block", "revision_table")


def _layer_t(page: int, img: Image.Image, cfg: RunConfig, force: bool, ctx: dict,
             put: Put, out: dict) -> None:
    from pipeline import tables as T
    out["tables"] = []
    if not has_key_for(cfg.model_t):
        put("T", status="blocked", note=f"no API key for {cfg.model_t}")
        return
    scope = "" if cfg.include_admin_tables else "--dataonly"
    readers = "+".join(_slug(m) for m in (cfg.model_t, cfg.model_t2, cfg.model_t3) if m)
    path = CACHE / f"page{page}_tables__{readers}{scope}--v3.json"
    found_path = None if force else _older_version(path, f"page{page}_tables__")
    if found_path is not None:
        older = found_path != path
        path = found_path
        blob = json.load(open(path))
        st = _stamp(path)
        if older:
            st["cache_file"] += " (earlier table-reader version — Re-run fresh to update)"
        out["tables"] = blob["tables"]
        out["cost_then"] = blob.get("cost")
        n = len(blob["tables"])
        put("T", status="done", seconds=0.0,
            note=(f"{n} table(s)" if n else "no tables on this sheet") + f" · from cache, saved {st['cached_at']}",
            data={"tables": blob["tables"], "errors": blob.get("errors", []), **st})
        return
    if cfg.cache_only:
        put("T", status="pending", note="not saved for this page yet — press Run")
        return
    t = time.time()
    try:
        put("T", status="running", note="looking for tables")
        # Finding tables is a layout job like Layer B's: the strongest model
        # draws the tightest boxes (Sonnet's once missed a whole column).
        found = T.find_tables(img, cfg.model_b)
        # Title-block and revision tables are drawing administration, not sheet
        # content. The find prompt says so, but it is not always obeyed, so the
        # rule is enforced here using Layer B's zones. Whether they should count
        # as "every table on the sheet" is an open question for the SMEs.
        zones = [z["bbox"] for z in ctx.get("suppress_regions", []) if z.get("kind") in ADMIN_ZONES]
        inside = lambda b, z: z[0] <= (b[0] + b[2]) / 2 <= z[2] and z[1] <= (b[1] + b[3]) / 2 <= z[3]
        for t in found:      # a "data" table sitting inside the title block is administration
            if t.get("kind", "data") == "data" and any(inside(t["bbox"], z) for z in zones):
                t["kind"] = "title_block"
        if not cfg.include_admin_tables:
            skipped = [t for t in found if t.get("kind", "data") != "data"]
            found = [t for t in found if t.get("kind", "data") == "data"]
            if skipped:
                put("T", status="running", note=f"skipped {len(skipped)} title-block table(s)",
                    data={"skipped_admin_tables": [t["title"] for t in skipped]})
        if not found:
            json.dump({"tables": [], "errors": []}, open(path, "w"))
            put("T", status="done", seconds=round(time.time() - t, 2), note="no tables on this sheet",
                data={"tables": [], "cached": False})
            return
        put("T", status="running", note=f"reading 0/{len(found)} tables")
        second = cfg.model_t2 if cfg.model_t2 and has_key_for(cfg.model_t2) else None
        third = cfg.model_t3 if cfg.model_t3 and has_key_for(cfg.model_t3) else None
        tables, errors = T.read_tables(
            img, found, cfg.model_t, second=second, tiebreak=third,
            on_progress=lambda d, n: put("T", status="running", note=f"reading {d}/{n} tables",
                                         data={"progress": d / n}))
    except Exception as exc:  # noqa: BLE001
        put("T", status="error", note=str(exc)[:160])
        return
    t_cost = (stats.snapshot()["layers"].get("T") or {}).get("cost")
    json.dump({"tables": tables, "errors": errors, "cost": t_cost}, open(path, "w"), indent=1)
    outdir = RUNS / f"page{page}_tables"
    outdir.mkdir(exist_ok=True)
    for tb in tables:
        (outdir / f"table{tb['index']}.csv").write_text(T.to_csv(tb))
    (outdir / "tables.md").write_text("\n".join(T.to_markdown(tb) for tb in tables))
    out["tables"] = tables
    shrunk = sum(1 for tb in tables if tb["read_scale"] < 1)
    disputed = sum(len(tb.get("disputed", [])) for tb in tables)
    put("T", status="error" if errors else "done", seconds=round(time.time() - t, 2),
        note=f"{len(tables)}/{len(found)} table(s) read"
             + (f" · {shrunk} read below native size" if shrunk else "")
             + (f" · {disputed} cell(s) disputed — check them" if disputed else " · readers agree")
             + (f" · {len(errors)} failed" if errors else ""),
        data={"tables": tables, "errors": errors, "cached": False})


# Pipe labels, prose annotations and "other" are not where a missing equipment
# category hides; leaving them out keeps the list short enough to read live.
NOT_EQUIPMENT = {"annotation", "line_label", "other"}


def _taglike_unclaimed(unclaimed: list[dict]) -> list[dict]:
    """Readings no rule claimed that look like equipment tags. On an unseen
    sheet this is where a category we have no rule for shows up, so it is
    written out beside the answers instead of being lost."""
    from pipeline.review import _TAGLIKE
    seen, out = set(), []
    for o in unclaimed:
        t = " ".join(str(o.get("text", "")).split())
        if (o.get("kind") not in NOT_EQUIPMENT and _TAGLIKE.match(t)
                and t.upper() not in seen and not t.replace(" ", "").isdigit()):
            seen.add(t.upper())
            out.append({"text": t, "kind": o.get("kind"), "symbol": o.get("symbol"),
                        "attached_to": o.get("attached_to"), "bbox": o.get("bbox")})
    return sorted(out, key=lambda o: (str(o["kind"]), o["text"]))


# ---- the run -------------------------------------------------------------------

def run(page: int, force: bool = False, cfg: RunConfig | None = None) -> Iterator[dict]:
    """Yield progress events. Each event is a dict the UI renders directly."""
    cfg = cfg or RunConfig()
    stats.reset()
    t_run = time.time()
    stages = {s[0]: StageResult(*s) for s in STAGES}

    def emit(sid: str, **kw) -> dict:
        st = stages[sid]
        data = kw.pop("data", None)
        if data is not None:
            st.data = {**st.data, **data}
        for k, v in kw.items():
            setattr(st, k, v)
        if kw.get("status") in ("running", "done") and "started_at" not in st.data:
            st.data["started_at"] = round(time.time() - t_run, 2)
        return {"type": "stage", "stage": asdict(st)}

    def stats_event(extra: dict | None = None) -> dict:
        return {"type": "stats", "stats": {**stats.snapshot(), **(extra or {})}}

    yield {"type": "init", "stages": [asdict(s) for s in stages.values()],
           "parallel": list(PARALLEL), "config": asdict(cfg), "d_label": cfg.d_label()}

    # ---- A: ingest --------------------------------------------------------------
    t = time.time()
    yield emit("A", status="running")
    img = Image.open(RENDERS / f"page{page}_full.png")
    img.load()
    tiles = tile_grid(*img.size)
    meta_f = RENDERS / f"page{page}_meta.json"
    pmeta = json.load(open(meta_f)) if meta_f.exists() else {}
    tl = pmeta.get("text_layer_chars")
    yield emit("A", status="done", seconds=round(time.time() - t, 2),
               note=f"{img.size[0]}×{img.size[1]}px · {len(tiles)} tiles"
                    + (f" · text layer {tl} chars" if tl is not None else ""),
               data={"width": img.size[0], "height": img.size[1], "tiles": tiles})

    # ---- B: sheet context -----------------------------------------------------------
    ctx_path = _ctx_path(page, cfg.model_b)
    ctx: dict = {}
    b_cost_then = None
    if not has_key_for(cfg.model_b):
        yield emit("B", status="blocked", note=f"no API key for {cfg.model_b}")
    elif ctx_path.exists() and not force:
        ctx = json.load(open(ctx_path))
        st = _stamp(ctx_path)
        b_cost_then = ctx.get("_cost")
        yield emit("B", status="done", seconds=0.0,
                   note=f"{ctx.get('sheet_id', '?')} · {cfg.model_b} · from cache, saved {st['cached_at']}",
                   data={"context": ctx, "model": cfg.model_b, **st})
    elif cfg.cache_only:
        yield emit("B", status="pending", note=f"not saved for {cfg.model_b} yet — press Run")
    else:
        t = time.time()
        yield emit("B", status="running", note=f"reading legend + notes · {cfg.model_b}")
        try:
            from pipeline.sheet_context import sheet_context
            ctx, usage = sheet_context(img, cfg.model_b)
            ctx_path = CACHE / f"page{page}_context__{_slug(cfg.model_b)}.json"
            ctx["_cost"] = cost(cfg.model_b, usage)
            json.dump(ctx, open(ctx_path, "w"), indent=1)
            yield emit("B", status="done", seconds=round(time.time() - t, 2),
                       note=(f"{ctx['sheet_id']} · {len(ctx['symbols'])} symbols · "
                             f"{len(ctx['suppress_regions'])} suppress zones · {cfg.model_b}"),
                       data={"context": ctx, "usage": usage, "model": cfg.model_b, "cached": False})
        except Exception as exc:  # noqa: BLE001
            ctx = {}
            yield emit("B", status="error", note=str(exc)[:120])
        yield stats_event()

    # ---- C ∥ D ∥ T ---------------------------------------------------------------------
    events: queue.Queue = queue.Queue()
    results: dict[str, dict] = {k: {} for k in PARALLEL}

    def put(sid, **kw):
        events.put(("stage", sid, kw))

    workers = {
        "C": (_layer_c, (page, img, tiles, force, cfg.cache_only)),
        "D": (_layer_d, (page, img, tiles, ctx, cfg, force)),
        "T": (_layer_t, (page, img, cfg, force, ctx)),
    }
    started = round(time.time() - t_run, 2)
    for sid in PARALLEL:
        stages[sid].data["started_at"] = started

    def wrap(sid, fn, args):
        try:
            fn(*args, put, results[sid])
        except Exception as exc:  # noqa: BLE001 — never let one layer kill the others
            put(sid, status="error", note=f"{type(exc).__name__}: {exc}"[:160])
        finally:
            events.put(("finished", sid, {}))

    threads = [threading.Thread(target=wrap, args=(sid, *workers[sid]), daemon=True) for sid in PARALLEL]
    for th in threads:
        th.start()
    live, last_stats = set(PARALLEL), 0.0
    while live:
        try:
            kind, sid, kw = events.get(timeout=1.0)
        except queue.Empty:
            yield stats_event()
            continue
        if kind == "finished":
            live.discard(sid)
            stages[sid].data["finished_at"] = round(time.time() - t_run, 2)
            yield {"type": "stage", "stage": asdict(stages[sid])}
            continue
        yield emit(sid, **kw)
        if time.time() - last_stats > 1.0:
            last_stats = time.time()
            yield stats_event()

    regions_out = results["C"].get("regions", [])
    observations = results["D"].get("observations", [])
    tables = results["T"].get("tables", [])

    # ---- V: visual checks for shape-defined categories -----------------------------
    from pipeline import verify as Vf
    from pipeline.resolve import load_rules
    rules_now = load_rules()
    verdicts: dict = {}
    needs = observations and any(r.get("verify") for r in rules_now.get("rules", []))
    if not needs:
        yield emit("V", status="pending", note="no visual checks needed" if observations else "needs Layer D observations")
    elif not has_key_for(cfg.model_v) and not cfg.cache_only:
        verdicts = Vf.verdicts_for(page, cfg.model_v, rules_now)
        yield emit("V", status="blocked", note=f"no API key for {cfg.model_v} — using saved answers / wording rule")
    else:
        t = time.time()
        yield emit("V", status="running", note=f"checking candidates · {cfg.model_v}")
        vq: queue.Queue = queue.Queue()
        vres: dict = {}

        def vwork():
            try:
                vres["v"], vres["r"] = Vf.run(page, img, observations, rules_now, cfg.model_v, cfg.cache_only,
                                              on_progress=lambda d, n: vq.put((d, n)))
            except Exception as exc:  # noqa: BLE001
                vres["e"] = exc
            finally:
                vq.put(None)

        threading.Thread(target=vwork, daemon=True).start()
        while (item := vq.get()) is not None:
            yield emit("V", status="running", note=f"{item[0]}/{item[1]} symbols checked · {cfg.model_v}",
                       data={"progress": item[0] / item[1]})
        if "e" in vres:
            verdicts = Vf.verdicts_for(page, cfg.model_v, rules_now)
            yield emit("V", status="error", note=f"{vres['e']}"[:140] + " — using wording rule")
        else:
            verdicts, rep = vres["v"], vres["r"]
            yes = sum(1 for v in verdicts.values() if v.get("answer") == "yes")
            missing = rep["candidates"] - len(verdicts)
            note = (f"{rep['candidates']} symbols · {yes} yes · {len(verdicts) - yes} no/unclear · "
                    f"{rep['asked']} asked, {rep['cached']} from cache")
            if missing and cfg.cache_only:
                note += f" · {missing} not checked yet — press Run"
            yield emit("V", status="error" if rep["errors"] else "done",
                       seconds=round(time.time() - t, 2), note=note,
                       data={"verdicts": verdicts, "errors": rep["errors"], "cached": rep["asked"] == 0})
        yield stats_event()

    # ---- E: resolve (deterministic) -----------------------------------------------------
    resolved: dict = {}
    if not observations:
        yield emit("E", status="pending", note="needs Layer D observations")
    else:
        t = time.time()
        yield emit("E", status="running")
        try:
            from pipeline.resolve import resolve
            resolved = resolve(observations, ctx, rules_now, verdicts)
            a = resolved["audit"]
            yield emit("E", status="done", seconds=round(time.time() - t, 2),
                       note=(f"{len(resolved['answers'])} categories · {a['resolved']} kept "
                             f"({a['suppressed']} suppressed, {a['duplicates']} dupes)"),
                       data=resolved)
        except Exception as exc:  # a bad RULES.yaml edit must name itself
            yield emit("E", status="error", note=str(exc)[:160])

    # ---- score ---------------------------------------------------------------------------
    from pipeline.score import document_for_sheet
    from pipeline.score import score as score_answers

    doc_id = document_for_sheet(ctx.get("sheet_id"))
    scored = score_answers(resolved.get("answers", {}), doc_id if doc_id is not None else -1,
                           layer_c_regions=regions_out,
                           observations=observations if observations else None)
    cov = (scored.get("coverage") or {}).get("coverage")
    cached_costs = {}
    if stages["B"].data.get("cached"):
        cached_costs["B"] = b_cost_then
    if results["D"].get("cached"):
        cached_costs["D"] = results["D"].get("cost_then")
    if stages["T"].data.get("cached"):
        cached_costs["T"] = results["T"].get("cost_then")
    final_stats = {**stats.snapshot(), "cached_layers": cached_costs,
                   "wall_seconds": round(time.time() - t_run, 2)}
    yield {"type": "stats", "stats": final_stats}

    summary = {
        "page": page,
        "document_id": doc_id,
        "sheet_id": ctx.get("sheet_id"),
        "sheet_type": ctx.get("sheet_type"),
        "suppress_regions": ctx.get("suppress_regions", []),
        "regions": len(regions_out),
        "small_regions": sum(1 for r in regions_out if (r["bbox"][3] - r["bbox"][1]) < 40),
        "tiles": len(tiles),
        "observations": len(observations),
        "answers": resolved.get("answers", {}),
        "provenance": resolved.get("provenance", {}),
        "audit": resolved.get("audit", {}),
        "unclaimed": resolved.get("unclaimed_count"),
        "unclassified": _taglike_unclaimed(resolved.get("unclaimed", [])),
        "score": scored,
        "coverage": cov,
        "coverage_note": (
            "needs Layer D observations" if cov is None else
            f"{(scored['coverage'] or {}).get('matched')} of "
            f"{(scored['coverage'] or {}).get('detector_regions')} detector regions "
            f"accounted for by Layer D"),
        "tables": tables,
        "config": asdict(cfg),
        "d_label": cfg.d_label(),
        "stats": final_stats,
    }

    # Persist: one timestamped file for history, one stable `latest` pointer.
    # Provenance, suppress zones, tables and per-call stats are dropped from the
    # stored copy so a history of runs stays small enough to keep forever.
    if cfg.cache_only:
        # Loading saved results is not a run: don't add it to the history.
        yield {"type": "done", "summary": summary}
        return
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    record = {k: v for k, v in summary.items()
              if k not in ("provenance", "suppress_regions", "tables", "unclassified")}
    record["stats"] = {k: v for k, v in final_stats.items() if k != "calls"}
    record["run_at"] = stamp
    record["model"] = cfg.d_label()
    record["obs_cache"] = _obs_path(page, cfg).name
    record["ctx_cache"] = _ctx_path(page, cfg.model_b).name
    record["layer_d_meta"] = results["D"].get("meta", {})
    for name in (f"page{page}_{stamp}.json", f"page{page}_latest.json"):
        json.dump(record, open(RUNS / name, "w"), indent=1)

    yield {"type": "done", "summary": summary}
