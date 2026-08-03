"""
Re-run ONLY the disposition stage of the 100-call full-cycle experiment
through the migrated two-stage pipeline (signal_extractor_emi +
emi_classifier), with language detection, and compare against production.

Per account from data/full_cycle_results.json (account_id, call_duration_s):
  1. Stage 1: extract_signals (gemini-2.5-flash, audio from the cached
     fc_audio dir, include_language=True)
  2. Stage 2: emi_classifier.classify(signals, call_duration_s, current_dt)
  3. Fetch production newest rows via db_context.get_recent_interactions,
     pick the row matching the experiment's call date (fallback: newest),
     unwrap {signals, output, ...} -> flat output.
  4. Record ours-vs-prod disposition/sub_disposition match, language, usage,
     cost, latency; where prod stored Stage-1 signals, also check classifier
     parity (our classifier on PROD signals vs prod's stored output).

Incremental save to data/two_stage_dispo_results.json.

Run: venv/bin/python scripts/rerun_disposition_two_stage.py [--pilot]
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv("/home/vk/Desktop/Propensity Score/.env")

import pipeline.llm_provider as llm_provider  # noqa: E402
from pipeline.signal_extractor_emi import extract_signals, ExtractionIncompleteError  # noqa: E402
from pipeline.emi_classifier import classify  # noqa: E402
from pipeline.db_context import get_recent_interactions  # noqa: E402

AUDIO_DIR = "/home/vk/.claude/jobs/d513ca17/tmp/fc_audio"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_PATH = os.path.join(ROOT, "data", "full_cycle_results.json")
RESULTS_PATH = os.path.join(ROOT, "data", "two_stage_dispo_results.json")

MODEL = "gemini-2.5-flash"
# USD per 1M tokens — approximate; raw tokens saved for exact repricing.
# cached_in = discounted rate for cache-hit text input (OpenAI automatic
# prompt caching); gemini path doesn't report cached tokens (rate unused).
PRICING_BY_MODEL = {
    "gemini-2.5-flash": {"text_in": 0.30, "cached_in": 0.30, "audio_in": 1.00, "out": 2.50},
    "gemini-3.5-flash-lite": {"text_in": 0.30, "cached_in": 0.30, "audio_in": 1.00, "out": 2.50},
    "gpt-audio-1.5": {"text_in": 2.50, "cached_in": 0.25, "audio_in": 32.00, "out": 10.00},
}
PRICING = PRICING_BY_MODEL[MODEL]


def cost_usd(usage: dict) -> float:
    cached = usage.get("cached_input_tokens", 0)
    text_in = usage.get("text_input_tokens", 0) - cached
    return (max(text_in, 0) / 1e6 * PRICING["text_in"]
            + cached / 1e6 * PRICING.get("cached_in", PRICING["text_in"])
            + usage.get("audio_input_tokens", 0) / 1e6 * PRICING["audio_in"]
            + usage.get("output_tokens", 0) / 1e6 * PRICING["out"])


def unwrap(row: dict) -> dict:
    """Production two-stage rows nest the disposition dict under "output"
    ({signals, output, call_qa, date}); older single-pass rows are flat.
    Return the flat disposition dict either way, keeping the row's date."""
    if isinstance(row, dict) and isinstance(row.get("output"), dict):
        flat = dict(row["output"])
        flat.setdefault("date", row.get("date"))
        return flat
    return row


_CALL_DATE_FORMATS = ("%A, %B %d, %Y %I:%M %p", "%Y-%m-%d")


def _row_date(row) -> str:
    """Normalize a history row's "date" stamp to YYYY-MM-DD, or ""."""
    raw = row.get("date") if isinstance(row, dict) else None
    if not raw:
        return ""
    for fmt in _CALL_DATE_FORMATS:
        try:
            return datetime.strptime(str(raw), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(raw)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def pick_prod_row(hist: list, call_date_ist: str):
    """Prefer the newest history row dated the experiment's call date (a newer
    call may have landed since the original run); fallback to the newest row."""
    for raw in hist or []:
        if _row_date(unwrap(raw)) == call_date_ist:
            return raw
    return hist[0] if hist else None


async def run_account(entry: dict) -> dict:
    account_id = entry["account_id"]
    duration = float(entry["call_duration_s"])
    call_date = entry.get("call_date_ist") or datetime.now().strftime("%Y-%m-%d")
    current_dt = f"{call_date}T12:00:00+05:30"

    out = {"account_id": account_id, "call_date_ist": call_date,
           "call_duration_s": duration}

    audio_path = os.path.join(AUDIO_DIR, f"{account_id}.mp3")
    if not os.path.exists(audio_path):
        out["error"] = f"audio missing: {audio_path}"
        return out
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    # ---- Stage 1: signal extraction (retry once on incomplete extraction,
    # same contract as production) ----
    t0 = time.monotonic()
    llm_provider.LAST_USAGE = {}
    try:
        signals = await extract_signals(audio_bytes, "audio/mpeg", current_dt,
                                        model=MODEL, include_language=True)
    except ExtractionIncompleteError as e:
        print(f"  extraction incomplete ({e}); retrying once", flush=True)
        llm_provider.LAST_USAGE = {}
        signals = await extract_signals(audio_bytes, "audio/mpeg", current_dt,
                                        model=MODEL, include_language=True)
    usage = dict(llm_provider.LAST_USAGE)
    out["latency_s"] = round(time.monotonic() - t0, 1)
    out["usage"] = usage
    out["cost_usd"] = round(cost_usd(usage), 6) if usage else 0.0

    # ---- Stage 2: deterministic classifier ----
    output = classify(signals, duration, current_dt)
    out["signals"] = signals
    out["output"] = output
    out["language"] = signals.get("language")

    # ---- Production comparison ----
    prod_hist = await get_recent_interactions(account_id)
    prod_raw = pick_prod_row(prod_hist, call_date)
    prod = unwrap(prod_raw) if prod_raw else {}
    prod_signals = prod_raw.get("signals") if isinstance(prod_raw, dict) else None

    cmp = {
        "prod_row_date": _row_date(prod),
        "prod_row_is_call_date": _row_date(prod) == call_date,
        "prod_disposition": prod.get("disposition"),
        "ours_disposition": output.get("disposition"),
        "disposition_match": prod.get("disposition") == output.get("disposition"),
        "prod_sub": prod.get("sub_disposition"),
        "ours_sub": output.get("sub_disposition"),
        "sub_disposition_match": prod.get("sub_disposition") == output.get("sub_disposition"),
        # Reference: what the single-pass experiment recorded as prod at run time.
        "prod_disposition_at_original_run": (entry.get("comparison", {})
                                             .get("disposition", {}).get("prod_disposition")),
    }
    # Optional: classifier parity — our ported classifier on PROD's stored
    # Stage-1 signals should reproduce prod's stored disposition.
    if isinstance(prod_signals, dict):
        try:
            replay = classify(dict(prod_signals), duration, current_dt)
            cmp["classifier_parity_on_prod_signals"] = (
                replay.get("disposition") == prod.get("disposition"))
            cmp["classifier_replay_disposition"] = replay.get("disposition")
        except Exception as e:
            cmp["classifier_parity_on_prod_signals"] = None
            cmp["classifier_replay_error"] = f"{type(e).__name__}: {e}"
    else:
        cmp["classifier_parity_on_prod_signals"] = None
    out["comparison"] = cmp
    return out


def summarize(results: list) -> dict:
    done = [r for r in results if "error" not in r]
    errs = [r for r in results if "error" in r]
    s = {"accounts_completed": len(done), "accounts_failed": len(errs),
         "failures": [{"account_id": r["account_id"], "error": r["error"]} for r in errs]}
    if not done:
        return s
    comp = [r["comparison"] for r in done if r["comparison"].get("prod_disposition")]
    n = len(comp)
    s["disposition_match"] = f"{sum(c['disposition_match'] for c in comp)}/{n}"
    s["sub_disposition_match"] = f"{sum(c['sub_disposition_match'] for c in comp)}/{n}"
    s["prod_row_is_call_date"] = f"{sum(c['prod_row_is_call_date'] for c in comp)}/{n}"

    confusions = Counter(
        (c["prod_disposition"], c["ours_disposition"])
        for c in comp if not c["disposition_match"])
    s["top_confusion_pairs_prod_to_ours"] = [
        {"prod": p, "ours": o, "count": k} for (p, o), k in confusions.most_common(8)]

    s["language_distribution"] = dict(Counter(r.get("language") for r in done))

    parity = [c["classifier_parity_on_prod_signals"] for c in comp
              if c.get("classifier_parity_on_prod_signals") is not None]
    s["classifier_parity_on_prod_signals"] = (
        f"{sum(parity)}/{len(parity)}" if parity else "n/a (no prod signals stored)")

    s["total_cost_usd"] = round(sum(r["cost_usd"] for r in done), 4)
    s["avg_cost_usd"] = round(sum(r["cost_usd"] for r in done) / len(done), 6)
    s["avg_latency_s"] = round(sum(r["latency_s"] for r in done) / len(done), 1)
    s["model"] = MODEL
    return s


def save(results):
    with open(RESULTS_PATH, "w") as f:
        json.dump({"results": results, "summary": summarize(results),
                   "pricing_usd_per_1m": PRICING}, f, indent=2, default=str)


async def main():
    global MODEL, RESULTS_PATH, PRICING
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", action="store_true", help="run first 2 accounts only")
    parser.add_argument("--model", default=MODEL, help="Stage-1 model")
    parser.add_argument("--out", default=RESULTS_PATH, help="results JSON path")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="seconds to sleep between accounts (TPM pacing)")
    args = parser.parse_args()
    MODEL = args.model
    RESULTS_PATH = args.out
    PRICING = PRICING_BY_MODEL[MODEL]

    with open(INPUT_PATH) as f:
        entries = json.load(f)["results"]
    entries = [e for e in entries if e.get("call_duration_s") is not None]
    if args.pilot:
        entries = entries[:2]
    print(f"Re-running disposition stage for {len(entries)} accounts", flush=True)

    results = []
    for i, entry in enumerate(entries, 1):
        print(f"[{i}/{len(entries)}] account {entry['account_id']} "
              f"({entry['call_duration_s']}s)", flush=True)
        try:
            res = await run_account(entry)
        except Exception as e:
            res = {"account_id": entry["account_id"], "error": f"{type(e).__name__}: {e}"}
        results.append(res)
        if "error" in res:
            print(f"  FAILED: {res['error']}", flush=True)
        else:
            c = res["comparison"]
            print(f"  prod={c['prod_disposition']!r} ours={c['ours_disposition']!r} "
                  f"match={c['disposition_match']} sub_match={c['sub_disposition_match']} "
                  f"lang={res['language']} cost=${res['cost_usd']} "
                  f"lat={res['latency_s']}s", flush=True)
        save(results)
        if args.sleep and i < len(entries):
            await asyncio.sleep(args.sleep)

    print("\n=== SUMMARY ===")
    print(json.dumps(summarize(results), indent=2))
    print(f"\nSaved: {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
