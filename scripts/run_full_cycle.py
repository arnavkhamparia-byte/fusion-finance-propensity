"""
Full-cycle production-candidate experiment on today's connected calls.

Per account (newest connected call today, duration > 20s, with recording):

  1. DISPOSITION  gemini-2.5-flash + audio, WITH the new `language` key
  2. NARRATIVE    azure:gpt-5.4-mini, NO audio (newest history record = our
                  disposition output, so language flows from stage 1)
  3. PROMPT BUILDER  azure:gpt-5.4-mini, strict amounts

Then compares each stage against what PRODUCTION already stored in the DB
(ai_disposition_analytics newest row, ai_account_latest_contexts), and logs
token usage + cost per stage for the experiment side.

Caveats (by design, documented in the report):
- Production context is post-call state; our narrative receives it as
  "previous narrative", so the narrative comparison is a near-idempotency
  check rather than a clean pre-call replay.
- Stage-1 disposition uses the single-pass EMI prompt; production may route
  through the two-stage pipeline, so disposition agreement is informative
  context, not the object under test.

Run: venv/bin/python scripts/run_full_cycle.py --limit 100 [--pilot]
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
from pipeline.disposition_agent import analyze_call  # noqa: E402
from pipeline.prompt_builder import build_prompt_blocks  # noqa: E402
from pipeline.db_context import get_recent_interactions, get_latest_context  # noqa: E402

BUCKET = "tata-bot-calls"
AUDIO_DIR = "/home/vk/.claude/jobs/d513ca17/tmp/fc_audio"
RESULTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "full_cycle_results.json",
)

DISPO_MODEL = "gemini-2.5-flash"
TEXT_MODEL = "azure:gpt-5.4-mini"

# USD per 1M tokens — approximate; raw tokens saved for exact repricing.
APPROX_PRICING = {
    DISPO_MODEL: {"text_in": 0.30, "audio_in": 1.00, "out": 2.50},
    TEXT_MODEL: {"text_in": 0.25, "audio_in": 0.0, "out": 2.00},
}
# Reference: measured production-config costs from the A/B benchmarks
# (gemini + audio narrative $0.0057, gemini builder $0.0040).
PROD_REFERENCE_COST = {"narrative": 0.0057, "prompt_builder": 0.0040}

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
  AND t.processed_at >= ((now() at time zone 'Asia/Kolkata')::date::timestamp) at time zone 'Asia/Kolkata'
ORDER BY t.account_id, t.processed_at DESC
"""


def cost_usd(model: str, usage: dict) -> float:
    p = APPROX_PRICING[model]
    return (usage.get("text_input_tokens", 0) / 1e6 * p["text_in"]
            + usage.get("audio_input_tokens", 0) / 1e6 * p.get("audio_in", 0)
            + usage.get("output_tokens", 0) / 1e6 * p["out"])


def stage_record(model: str, t0: float, extra: dict) -> dict:
    usage = dict(llm_provider.LAST_USAGE)
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
    """Production two-stage rows nest the disposition dict under "output"
    ({signals, output, call_qa, date}); older single-pass rows are flat.
    Return the flat disposition dict either way, keeping the row's date."""
    if isinstance(row, dict) and isinstance(row.get("output"), dict):
        flat = dict(row["output"])
        flat.setdefault("date", row.get("date"))
        return flat
    return row


async def run_account(acct: dict) -> dict:
    account_id = acct["account_id"]
    out = {"account_id": account_id, "loan_number": acct["loan_number"],
           "call_date_ist": acct["call_date_ist"], "call_duration_s": acct["call_duration"],
           "stages": {}, "comparison": {}}

    audio_path = download_audio(acct["call_recording_url"], account_id)

    # Production stored state (reference for comparison AND narrative inputs)
    prod_hist = await get_recent_interactions(account_id)
    prod_ctx = await get_latest_context(account_id) or {}
    prod_commitments = (prod_ctx.get("combined_intelligence") or {}).get("commitments") or []

    # ---- Stage 1: disposition (gemini + audio + language) ----
    t0 = time.monotonic()
    llm_provider.LAST_USAGE = {}
    dispo = await analyze_call(
        audio_path, model=DISPO_MODEL, include_language=True,
        call_duration_s=float(acct["call_duration"]),
    )
    if dispo is None:
        out["error"] = "disposition failed"
        return out
    out["stages"]["disposition"] = stage_record(DISPO_MODEL, t0, {
        "model": DISPO_MODEL,
        "disposition": dispo.get("disposition"), "sub_disposition": dispo.get("sub_disposition"),
        "language": dispo.get("language"), "summary": dispo.get("summary"),
    })

    prod_newest = unwrap(prod_hist[0]) if prod_hist else {}
    out["comparison"]["disposition"] = {
        "prod_disposition": prod_newest.get("disposition"),
        "ours_disposition": dispo.get("disposition"),
        "match": prod_newest.get("disposition") == dispo.get("disposition"),
        "prod_sub": prod_newest.get("sub_disposition"), "ours_sub": dispo.get("sub_disposition"),
    }

    # ---- Stage 2: narrative (azure gpt-5.4-mini, no audio) ----
    # Newest history record becomes OUR disposition output (carries language).
    synth_hist = [dict(dispo)] + [unwrap(copy.deepcopy(r)) for r in (prod_hist[1:] if prod_hist else [])]
    frozen_ctx = copy.deepcopy(prod_ctx) if prod_ctx else None

    async def _hist(_id, client="fusion_mfi_emi"):
        return copy.deepcopy(synth_hist)

    async def _ctx(_id, client="fusion_mfi_emi"):
        return copy.deepcopy(frozen_ctx)

    na.get_recent_interactions = _hist
    na.get_latest_context = _ctx

    t0 = time.monotonic()
    llm_provider.LAST_USAGE = {}
    narrative, status, _h, language, commitments = await na.update_account_narrative(
        account_id, model=TEXT_MODEL, use_audio=False,
        expected_call_date=acct["call_date_ist"],
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

    # ---- Stage 3: prompt builder (azure gpt-5.4-mini, strict) ----
    t0 = time.monotonic()
    llm_provider.LAST_USAGE = {}
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
    }
    out["total_cost_usd"] = round(sum(s["cost_usd"] for s in out["stages"].values()), 6)
    return out


def summarize(results: list) -> dict:
    done = [r for r in results if "error" not in r and r.get("stages")]
    errs = [r for r in results if "error" in r]
    s = {"accounts_completed": len(done), "accounts_failed": len(errs),
         "failures": [{"account_id": r["account_id"], "error": r["error"]} for r in errs]}
    if not done:
        return s
    for stage, model in [("disposition", DISPO_MODEL), ("narrative", TEXT_MODEL), ("prompt_builder", TEXT_MODEL)]:
        runs = [r["stages"][stage] for r in done if stage in r["stages"]]
        s[stage] = {
            "model": model, "runs": len(runs),
            "total_cost_usd": round(sum(x["cost_usd"] for x in runs), 4),
            "avg_cost_usd": round(sum(x["cost_usd"] for x in runs) / len(runs), 6) if runs else None,
            "avg_latency_s": round(sum(x["latency_s"] for x in runs) / len(runs), 1) if runs else None,
        }
    s["agreement_vs_production"] = {
        "disposition_match": f"{sum(r['comparison']['disposition']['match'] for r in done if 'disposition' in r['comparison'])}/{len(done)}",
        "status_pattern_match": f"{sum(r['comparison']['narrative']['status_pattern_match'] for r in done if 'narrative' in r['comparison'])}/{sum('narrative' in r['comparison'] for r in done)}",
        "commitments_superset": f"{sum(r['comparison']['narrative']['commitments_superset_of_prod'] for r in done if 'narrative' in r['comparison'])}/{sum('narrative' in r['comparison'] for r in done)}",
        "prompt_versions_all_match": f"{sum(r['comparison']['prompt_builder']['all_versions_match'] for r in done if 'prompt_builder' in r['comparison'])}/{sum('prompt_builder' in r['comparison'] for r in done)}",
    }
    from collections import Counter
    s["languages_detected"] = dict(Counter(
        r["stages"]["disposition"].get("language") for r in done if "disposition" in r["stages"]))
    s["experiment_avg_cost_per_account_usd"] = round(
        sum(r.get("total_cost_usd", 0) for r in done) / len(done), 6)
    s["production_reference_note"] = (
        "Production narrative+builder measured at "
        f"~${PROD_REFERENCE_COST['narrative'] + PROD_REFERENCE_COST['prompt_builder']:.4f}/account "
        "(gemini-2.5-flash with audio, from the A/B benchmarks); disposition runs in both worlds."
    )
    return s


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args()
    limit = 2 if args.pilot else args.limit

    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", 5432)),
        database="fusion_finance_mfi", user=os.environ["DB_USER"], password=os.environ["DB_PASS"],
    )
    try:
        rows = await conn.fetch(SELECT_SQL)
    finally:
        await conn.close()
    rows = sorted(rows, key=lambda r: r["processed_at"], reverse=True)[:limit]
    print(f"Selected {len(rows)} accounts (today, connected, >20s, with recording)", flush=True)

    results = []
    for i, r in enumerate(rows, 1):
        acct = dict(r)
        print(f"[{i}/{len(rows)}] account {acct['account_id']} ({acct['call_duration']}s)", flush=True)
        try:
            res = await run_account(acct)
        except Exception as e:
            res = {"account_id": acct["account_id"], "error": f"{type(e).__name__}: {e}", "stages": {}}
            print(f"  FAILED: {res['error']}", flush=True)
        results.append(res)
        if "error" not in res:
            c = res["comparison"]
            print(f"  dispo={c['disposition']['match']} lang={c['narrative']['language_ours']} "
                  f"status={c['narrative']['status_pattern_match']} "
                  f"blocks={c['prompt_builder']['versions_matched']}/{c['prompt_builder']['versions_compared']} "
                  f"cost=${res['total_cost_usd']}", flush=True)
        with open(RESULTS_PATH, "w") as f:
            json.dump({"results": results, "summary": summarize(results),
                       "pricing_usd_per_1m": APPROX_PRICING}, f, indent=2, default=str)

    print("\n=== SUMMARY ===")
    print(json.dumps(summarize(results), indent=2))
    print(f"\nSaved: {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
