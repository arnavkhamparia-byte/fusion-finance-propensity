"""
Full-cycle candidate experiment at scale: 1000 accounts, 6 parallel workers.

Per account (the account's LATEST processed EMI call, Jul 29-30 IST, >20s,
with recording, and no AI call of any flow processed after it — so the stored
production context corresponds to the analyzed call):

  1. DISPOSITION  two-stage: signal_extractor_emi (gemini-2.5-flash + audio,
                  include_language=True) -> emi_classifier.classify
  2. NARRATIVE    azure:gpt-5.4-mini, NO audio (newest history record = our
                  stage-1 output incl. language)
  3. PROMPT BUILDER  azure:gpt-5.4-mini, strict amounts

Comparisons against production stored state mirror run_full_cycle.py.
Concurrency-safe: per-task usage via llm_provider.get_usage()/reset_usage(),
and the narrative DB monkeypatch reads from an account-keyed registry.

Run: venv/bin/python scripts/run_full_cycle_1000.py --limit 1000 --workers 6 [--pilot]
"""

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv("/home/vk/Desktop/Propensity Score/.env")

import pipeline.llm_provider as llm_provider  # noqa: E402
import pipeline.narrative_agent as na  # noqa: E402
from pipeline.signal_extractor_emi import extract_signals, ExtractionIncompleteError  # noqa: E402
from pipeline.emi_classifier import classify  # noqa: E402
from pipeline.prompt_builder import build_prompt_blocks  # noqa: E402
from pipeline.db_context import get_recent_interactions, get_latest_context  # noqa: E402

BUCKET = "tata-bot-calls"
AUDIO_DIR = "/home/vk/.claude/jobs/d513ca17/tmp/fc1000_audio"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(ROOT, "data", "full_cycle_1000_results.json")

DISPO_MODEL = "gemini-2.5-flash"
TEXT_MODEL = "azure:gpt-5.4-mini"

APPROX_PRICING = {
    DISPO_MODEL: {"text_in": 0.30, "audio_in": 1.00, "out": 2.50},
    TEXT_MODEL: {"text_in": 0.25, "audio_in": 0.0, "out": 2.00},
}

# Latest processed EMI call per account in the window; the NOT EXISTS clause
# drops accounts where ANY later AI call (any flow) was processed, because
# that call would have rewritten ai_account_latest_contexts.
SELECT_SQL = """
SELECT DISTINCT ON (t.account_id)
    t.account_id, ad.loan_number, t.disposition AS prod_task_disposition,
    t.call_duration, t.call_recording_url,
    to_char(t.processed_at AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD') AS call_date_ist,
    t.processed_at::text AS processed_at
FROM activity_taskactivity t
LEFT JOIN account_details ad ON ad.id = t.account_id
WHERE t.provider = 'Tata' AND t.activity_type = 'AI Call'
  AND t.task_status = 'Connected' AND t.disposition IS NOT NULL
  AND t.call_recording_url IS NOT NULL
  AND t.call_duration > 20
  AND t.flow = 'fusion_mfi_emi'
  AND (t.processed_at AT TIME ZONE 'Asia/Kolkata')::date >= DATE '2026-07-29'
  AND NOT EXISTS (
      SELECT 1 FROM activity_taskactivity t2
      WHERE t2.account_id = t.account_id
        AND t2.activity_type = 'AI Call'
        AND t2.processed_at > t.processed_at
  )
ORDER BY t.account_id, t.processed_at DESC
"""

# Account-keyed registries read by the (single, global) narrative monkeypatch.
SYNTH_HIST: dict = {}
FROZEN_CTX: dict = {}


async def _patched_hist(account_id, client="fusion_mfi_emi"):
    return copy.deepcopy(SYNTH_HIST[int(account_id)])


async def _patched_ctx(account_id, client="fusion_mfi_emi"):
    return copy.deepcopy(FROZEN_CTX[int(account_id)])


na.get_recent_interactions = _patched_hist
na.get_latest_context = _patched_ctx


def cost_usd(model: str, usage: dict) -> float:
    p = APPROX_PRICING[model]
    return (usage.get("text_input_tokens", 0) / 1e6 * p["text_in"]
            + usage.get("audio_input_tokens", 0) / 1e6 * p.get("audio_in", 0)
            + usage.get("output_tokens", 0) / 1e6 * p["out"])


def stage_record(model: str, t0: float, extra: dict) -> dict:
    usage = llm_provider.get_usage()
    return {"latency_s": round(time.monotonic() - t0, 1), "usage": usage,
            "cost_usd": round(cost_usd(model, usage), 6) if usage else 0.0, **extra}


def download_audio(key: str, account_id) -> str:
    os.makedirs(AUDIO_DIR, exist_ok=True)
    path = os.path.join(AUDIO_DIR, f"{account_id}.mp3")
    if not os.path.exists(path):
        s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
        s3.download_file(BUCKET, key, path)
    return path


def ckeys(commitments) -> list:
    out = []
    for c in commitments or []:
        amt = c.get("amount")
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            pass
        out.append(str((c.get("type"), c.get("due_date"), amt)))
    return sorted(out)


def pattern(s):
    m = re.match(r"([A-Z_]+)", (s or "").strip())
    return m.group(1) if m else None


def unwrap(row: dict) -> dict:
    if isinstance(row, dict) and isinstance(row.get("output"), dict):
        flat = dict(row["output"])
        flat.setdefault("date", row.get("date"))
        return flat
    return row


_CALL_DATE_FORMATS = ("%A, %B %d, %Y %I:%M %p", "%Y-%m-%d")


def _row_date(row: dict):
    from datetime import datetime
    raw = (row or {}).get("date")
    if not raw:
        return None
    for fmt in _CALL_DATE_FORMATS:
        try:
            return datetime.strptime(str(raw).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return str(raw)[:10]


async def run_account(acct: dict) -> dict:
    account_id = int(acct["account_id"])
    duration = float(acct["call_duration"])
    call_date = acct["call_date_ist"]
    current_dt = f"{call_date}T12:00:00+05:30"
    out = {"account_id": account_id, "loan_number": acct["loan_number"],
           "call_date_ist": call_date, "call_duration_s": acct["call_duration"],
           "stages": {}, "comparison": {}}

    audio_path = await asyncio.to_thread(download_audio, acct["call_recording_url"], account_id)
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    prod_hist = await get_recent_interactions(account_id)
    prod_ctx = await get_latest_context(account_id) or {}
    prod_commitments = (prod_ctx.get("combined_intelligence") or {}).get("commitments") or []

    # ---- Stage 1: two-stage disposition ----
    t0 = time.monotonic()
    llm_provider.reset_usage()
    try:
        signals = await extract_signals(audio_bytes, "audio/mpeg", current_dt,
                                        model=DISPO_MODEL, include_language=True)
    except (ExtractionIncompleteError, json.JSONDecodeError):
        llm_provider.reset_usage()
        signals = await extract_signals(audio_bytes, "audio/mpeg", current_dt,
                                        model=DISPO_MODEL, include_language=True)
    dispo_out = classify(signals, duration, current_dt)
    dispo = dict(dispo_out)
    dispo["language"] = signals.get("language")
    if signals.get("summary") and not dispo.get("summary"):
        dispo["summary"] = signals.get("summary")
    dispo.setdefault("date", call_date)
    out["stages"]["disposition"] = stage_record(DISPO_MODEL, t0, {
        "model": DISPO_MODEL, "two_stage": True,
        "disposition": dispo.get("disposition"), "sub_disposition": dispo.get("sub_disposition"),
        "language": dispo.get("language"), "signals": signals,
    })

    prod_newest = unwrap(prod_hist[0]) if prod_hist else {}
    # Staleness guard: production is live — if the newest stored row is not
    # for the analyzed call's date, a newer call rewrote the context and the
    # comparison reference is invalid. Flag it; summarize() excludes these.
    out["stale_reference"] = _row_date(prod_newest) != call_date
    out["comparison"]["disposition"] = {
        "prod_row_date": _row_date(prod_newest),
        "prod_disposition": prod_newest.get("disposition"),
        "ours_disposition": dispo.get("disposition"),
        "match": prod_newest.get("disposition") == dispo.get("disposition"),
        "prod_sub": prod_newest.get("sub_disposition"), "ours_sub": dispo.get("sub_disposition"),
    }

    # ---- Stage 2: narrative (no audio) ----
    synth_hist = [dict(dispo)] + [unwrap(copy.deepcopy(r)) for r in (prod_hist[1:] if prod_hist else [])]
    SYNTH_HIST[account_id] = synth_hist
    FROZEN_CTX[account_id] = copy.deepcopy(prod_ctx) if prod_ctx else None

    t0 = time.monotonic()
    llm_provider.reset_usage()
    narrative, status, _h, language, commitments = await na.update_account_narrative(
        account_id, model=TEXT_MODEL, use_audio=False, expected_call_date=call_date,
    )
    if narrative is None:
        out["error"] = "narrative failed"
        return out
    out["stages"]["narrative"] = stage_record(TEXT_MODEL, t0, {
        "model": TEXT_MODEL, "narrative": narrative, "account_status": status,
        "language": language, "commitments": commitments,
    })
    out["comparison"]["narrative"] = {
        "prod_status_pattern": pattern(prod_ctx.get("account_status")),
        "ours_status_pattern": pattern(status),
        "status_pattern_match": pattern(prod_ctx.get("account_status")) == pattern(status),
        "prod_commitments": ckeys(prod_commitments), "ours_commitments": ckeys(commitments),
        "commitments_superset_of_prod": set(ckeys(prod_commitments)) <= set(ckeys(commitments)),
        "language_ours": language,
    }

    # ---- Stage 3: prompt builder (strict) ----
    t0 = time.monotonic()
    llm_provider.reset_usage()
    blocks = await build_prompt_blocks(
        narrative=narrative, account_status=status,
        recent_history=json.loads(json.dumps(synth_hist, default=str)),
        default_language=language,
        previous_blocks=copy.deepcopy(prod_ctx.get("prompt_blocks")) if prod_ctx.get("prompt_blocks") else None,
        commitments=json.loads(json.dumps(commitments, default=str)),
        model=TEXT_MODEL, strict_amounts=True,
    )
    if blocks is None:
        out["error"] = "prompt builder failed"
        return out
    prod_blocks = prod_ctx.get("prompt_blocks") or {}
    version_matches, version_total = 0, 0
    for name, v in blocks.items():
        pv = prod_blocks.get(name)
        if isinstance(pv, dict) and "version" in pv:
            version_total += 1
            version_matches += int(pv["version"] == v["version"])
    out["stages"]["prompt_builder"] = stage_record(TEXT_MODEL, t0, {
        "model": TEXT_MODEL, "prompt_blocks": blocks,
    })
    out["comparison"]["prompt_builder"] = {
        "versions_matched": version_matches, "versions_compared": version_total,
        "all_versions_match": version_total > 0 and version_matches == version_total,
        "prod_versions": {k: v.get("version") for k, v in prod_blocks.items()
                          if isinstance(v, dict)},
    }
    out["total_cost_usd"] = round(sum(s["cost_usd"] for s in out["stages"].values()), 6)
    return out


def summarize(results: list) -> dict:
    from collections import Counter
    all_done = [r for r in results if "error" not in r and r.get("stages")]
    errs = [r for r in results if "error" in r]
    stale = [r for r in all_done if r.get("stale_reference")]
    done = [r for r in all_done if not r.get("stale_reference")]
    s = {"accounts_completed": len(all_done), "accounts_failed": len(errs),
         "stale_reference_excluded": len(stale),
         "failures": [{"account_id": r["account_id"], "error": r["error"]} for r in errs]}
    if not done:
        return s
    for stage, model in [("disposition", DISPO_MODEL), ("narrative", TEXT_MODEL), ("prompt_builder", TEXT_MODEL)]:
        runs = [r["stages"][stage] for r in all_done if stage in r["stages"]]
        s[stage] = {
            "model": model, "runs": len(runs),
            "total_cost_usd": round(sum(x["cost_usd"] for x in runs), 4),
            "avg_cost_usd": round(sum(x["cost_usd"] for x in runs) / len(runs), 6) if runs else None,
            "avg_latency_s": round(sum(x["latency_s"] for x in runs) / len(runs), 1) if runs else None,
        }
    dcomp = [r["comparison"]["disposition"] for r in done if "disposition" in r["comparison"]]
    s["agreement_vs_production"] = {
        "disposition_match": f"{sum(c['match'] for c in dcomp)}/{len(dcomp)}",
        "status_pattern_match": f"{sum(r['comparison']['narrative']['status_pattern_match'] for r in done if 'narrative' in r['comparison'])}/{sum('narrative' in r['comparison'] for r in done)}",
        "commitments_superset": f"{sum(r['comparison']['narrative']['commitments_superset_of_prod'] for r in done if 'narrative' in r['comparison'])}/{sum('narrative' in r['comparison'] for r in done)}",
        "prompt_versions_all_match": f"{sum(r['comparison']['prompt_builder']['all_versions_match'] for r in done if 'prompt_builder' in r['comparison'])}/{sum('prompt_builder' in r['comparison'] for r in done)}",
    }
    s["disposition_confusions_prod_to_ours"] = [
        {"prod": p, "ours": o, "count": k}
        for (p, o), k in Counter((c["prod_disposition"], c["ours_disposition"])
                                 for c in dcomp if not c["match"]).most_common(10)]
    s["languages_detected"] = dict(Counter(
        r["stages"]["disposition"].get("language") for r in done if "disposition" in r["stages"]))
    s["experiment_avg_cost_per_account_usd"] = round(
        sum(r.get("total_cost_usd", 0) for r in all_done) / len(all_done), 6)
    return s


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--out", default=RESULTS_PATH)
    args = parser.parse_args()
    limit = 4 if args.pilot else args.limit
    workers = 2 if args.pilot else args.workers

    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", 5432)),
        database="fusion_finance_mfi", user=os.environ["DB_USER"], password=os.environ["DB_PASS"],
    )
    try:
        rows = await conn.fetch(SELECT_SQL)
    finally:
        await conn.close()
    rows = sorted(rows, key=lambda r: r["processed_at"], reverse=True)[:limit]
    print(f"Selected {len(rows)} accounts (latest call Jul 29-30, no later AI call)", flush=True)

    sem = asyncio.Semaphore(workers)
    results = []
    lock = asyncio.Lock()
    t_start = time.monotonic()

    def save():
        with open(args.out, "w") as f:
            json.dump({"results": results, "summary": summarize(results),
                       "pricing_usd_per_1m": APPROX_PRICING}, f, indent=2, default=str)

    async def worker(i, row):
        acct = dict(row)
        async with sem:
            try:
                res = await run_account(acct)
            except Exception as e:
                res = {"account_id": acct["account_id"],
                       "error": f"{type(e).__name__}: {e}", "stages": {}}
        async with lock:
            results.append(res)
            n = len(results)
            if "error" in res:
                print(f"[{n}/{len(rows)}] account {acct['account_id']} FAILED: {res['error']}", flush=True)
            else:
                c = res["comparison"]
                print(f"[{n}/{len(rows)}] account {acct['account_id']} "
                      f"dispo={c['disposition']['match']} lang={c['narrative']['language_ours']} "
                      f"status={c['narrative']['status_pattern_match']} "
                      f"blocks={c['prompt_builder']['versions_matched']}/{c['prompt_builder']['versions_compared']} "
                      f"cost=${res.get('total_cost_usd')}", flush=True)
            if n % 10 == 0 or n == len(rows):
                save()
                rate = n / (time.monotonic() - t_start)
                print(f"  -- saved; {rate*3600:.0f} accounts/h, ETA "
                      f"{(len(rows)-n)/max(rate,1e-9)/60:.0f} min", flush=True)

    await asyncio.gather(*(worker(i, r) for i, r in enumerate(rows)))
    save()
    print("\n=== SUMMARY ===")
    print(json.dumps(summarize(results), indent=2))
    print(f"\nSaved: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
