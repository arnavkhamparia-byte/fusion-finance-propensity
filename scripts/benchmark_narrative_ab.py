"""
Narrative agent A/B benchmark: with-audio vs no-audio, Gemini vs OpenAI.

Four arms per account, all fed IDENTICAL frozen inputs (history + previous
context are fetched once per account and injected, so no arm sees fresher
DB state than another):

  A  gemini-2.5-flash  + audio   (production reference)
  B  gemini-2.5-flash  no audio
  C  gpt-audio-mini    + audio
  D  gpt-5.4-mini      no audio

Per-arm token usage is captured from the provider responses and priced with
the APPROX_PRICING table below (USD per 1M tokens — approximate list prices;
raw token counts are saved so costs can be recomputed exactly).

Run: venv/bin/python scripts/benchmark_narrative_ab.py --limit 20
     [--pilot]  (2 accounts only, for a smoke test)
"""

import argparse
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
load_dotenv("/home/vk/Desktop/Propensity Score/.env")

import pipeline.llm_provider as llm_provider  # noqa: E402
import pipeline.narrative_agent as na  # noqa: E402
from pipeline.db_context import get_recent_interactions, get_latest_context  # noqa: E402

BUCKET = "tata-bot-calls"
AUDIO_DIR = os.environ.get("AB_AUDIO_DIR", "/home/vk/.claude/jobs/d513ca17/tmp/ab_audio")
RESULTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "narrative_ab_results.json",
)

ARMS = [
    {"key": "A_gemini_audio", "model": "gemini-2.5-flash", "use_audio": True},
    {"key": "B_gemini_noaudio", "model": "gemini-2.5-flash", "use_audio": False},
    {"key": "C_gptaudiomini_audio", "model": "gpt-audio-mini", "use_audio": True},
    {"key": "D_gpt54mini_noaudio", "model": "gpt-5.4-mini", "use_audio": False},
]

# USD per 1M tokens — APPROXIMATE list prices; edit here to reprice.
# Raw token counts are stored per call, so exact cost is always recomputable.
APPROX_PRICING = {
    "gemini-2.5-flash": {"text_in": 0.30, "audio_in": 1.00, "out": 2.50},
    "gpt-audio-mini":   {"text_in": 0.60, "audio_in": 10.00, "out": 2.40},
    "gpt-5.4-mini":     {"text_in": 0.25, "audio_in": 0.0,  "out": 2.00},
}

SELECT_CALLS_SQL_TEMPLATE = """
SELECT DISTINCT ON (t.account_id)
    t.account_id,
    ad.loan_number,
    t.disposition,
    t.call_duration,
    t.call_recording_url,
    to_char(t.processed_at AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD') AS call_date_ist,
    t.processed_at::text AS processed_at
FROM activity_taskactivity t
LEFT JOIN account_details ad ON ad.id = t.account_id
WHERE t.provider = 'Tata'
  AND t.activity_type = 'AI Call'
  AND t.task_status = 'Connected'
  AND t.disposition IS NOT NULL
  AND t.call_recording_url IS NOT NULL
  AND t.call_duration BETWEEN 30 AND 300
  {extra_filter}
ORDER BY t.account_id, t.processed_at DESC
"""


def cost_usd(model: str, usage: dict) -> float:
    p = APPROX_PRICING[model]
    return (
        usage.get("text_input_tokens", 0) / 1e6 * p["text_in"]
        + usage.get("audio_input_tokens", 0) / 1e6 * p["audio_in"]
        + usage.get("output_tokens", 0) / 1e6 * p["out"]
    )


async def select_accounts(limit: int, reuse_ids: list = None) -> list:
    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", 5432)),
        database="fusion_finance_mfi", user=os.environ["DB_USER"], password=os.environ["DB_PASS"],
    )
    try:
        if reuse_ids:
            sql = SELECT_CALLS_SQL_TEMPLATE.format(extra_filter="AND t.account_id = ANY($1)")
            rows = await conn.fetch(sql, reuse_ids)
        else:
            sql = SELECT_CALLS_SQL_TEMPLATE.format(
                extra_filter="AND t.processed_at >= now() - interval '3 days'")
            rows = await conn.fetch(sql)
    finally:
        await conn.close()
    # Newest calls first across accounts, then trim.
    rows = sorted(rows, key=lambda r: r["processed_at"], reverse=True)[: limit * 3]
    picked = []
    for r in rows:
        # Only keep accounts whose disposition row already landed (the no-audio
        # arms depend on it being the newest history entry).
        hist = await get_recent_interactions(r["account_id"])
        if not hist:
            continue
        picked.append({**dict(r), "_history": hist})
        if len(picked) >= limit:
            break
    return picked


def download_audio(key: str, account_id) -> str:
    os.makedirs(AUDIO_DIR, exist_ok=True)
    path = os.path.join(AUDIO_DIR, f"{account_id}.mp3")
    if not os.path.exists(path):
        s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
        s3.download_file(BUCKET, key, path)
    return path


async def run_account(acct: dict) -> dict:
    account_id = acct["account_id"]
    audio_path = download_audio(acct["call_recording_url"], account_id)

    # Freeze inputs once; every arm gets a deep copy (narrative_agent mutates
    # commitment dicts in place).
    frozen_history = acct["_history"]
    frozen_ctx = await get_latest_context(account_id)

    async def _hist(_id, client="fusion_mfi_emi"):
        return copy.deepcopy(frozen_history)

    async def _ctx(_id, client="fusion_mfi_emi"):
        return copy.deepcopy(frozen_ctx)

    na.get_recent_interactions = _hist
    na.get_latest_context = _ctx

    out = {
        "account_id": account_id,
        "loan_number": acct["loan_number"],
        "call_date_ist": acct["call_date_ist"],
        "disposition": acct["disposition"],
        "call_duration_s": int(acct["call_duration"]),
        "arms": {},
    }
    for arm in ARMS:
        t0 = time.monotonic()
        llm_provider.LAST_USAGE = {}
        narrative, status, _hist_out, language, commitments = await na.update_account_narrative(
            account_id,
            audio_path=audio_path if arm["use_audio"] else None,
            model=arm["model"],
            use_audio=arm["use_audio"],
            expected_call_date=None if arm["use_audio"] else acct["call_date_ist"],
        )
        usage = dict(llm_provider.LAST_USAGE)
        out["arms"][arm["key"]] = {
            "model": arm["model"],
            "use_audio": arm["use_audio"],
            "ok": narrative is not None,
            "narrative": narrative,
            "account_status": status,
            "language": language,
            "commitments": commitments,
            "latency_s": round(time.monotonic() - t0, 1),
            "usage": usage,
            "cost_usd": round(cost_usd(arm["model"], usage), 6) if usage else None,
        }
        print(f"  {arm['key']}: ok={narrative is not None} "
              f"tokens={usage.get('total_input_tokens', '?')}in/{usage.get('output_tokens', '?')}out "
              f"cost=${out['arms'][arm['key']]['cost_usd']} "
              f"({out['arms'][arm['key']]['latency_s']}s)", flush=True)
    return out


def summarize(results: list) -> dict:
    summary = {}
    for arm in ARMS:
        k = arm["key"]
        runs = [r["arms"][k] for r in results if k in r["arms"]]
        ok = [r for r in runs if r["ok"]]
        summary[k] = {
            "model": arm["model"],
            "use_audio": arm["use_audio"],
            "runs": len(runs),
            "failures": len(runs) - len(ok),
            "total_text_input_tokens": sum(r["usage"].get("text_input_tokens", 0) for r in ok),
            "total_audio_input_tokens": sum(r["usage"].get("audio_input_tokens", 0) for r in ok),
            "total_output_tokens": sum(r["usage"].get("output_tokens", 0) for r in ok),
            "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in ok), 4),
            "avg_cost_per_call_usd": round(sum(r["cost_usd"] or 0 for r in ok) / len(ok), 6) if ok else None,
            "avg_latency_s": round(sum(r["latency_s"] for r in ok) / len(ok), 1) if ok else None,
            "avg_narrative_words": round(sum(len((r["narrative"] or "").split()) for r in ok) / len(ok)) if ok else None,
        }
    return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--pilot", action="store_true", help="2 accounts only")
    parser.add_argument("--reuse", type=str, default=None,
                        help="Path to a previous results JSON — re-run on the same account_ids.")
    parser.add_argument("--out", type=str, default=None, help="Results output path override.")
    args = parser.parse_args()
    limit = 2 if args.pilot else args.limit

    global RESULTS_PATH
    if args.out:
        RESULTS_PATH = args.out

    reuse_ids = None
    if args.reuse:
        prev = json.load(open(args.reuse))
        reuse_ids = [r["account_id"] for r in prev["results"] if r.get("arms")]
        limit = len(reuse_ids)
        print(f"Reusing {limit} account ids from {args.reuse}", flush=True)

    accounts = await select_accounts(limit, reuse_ids=reuse_ids)
    print(f"Selected {len(accounts)} accounts", flush=True)

    results = []
    for i, acct in enumerate(accounts, 1):
        print(f"[{i}/{len(accounts)}] account {acct['account_id']} "
              f"({acct['disposition']}, {acct['call_duration']}s, {acct['call_date_ist']})", flush=True)
        try:
            results.append(await run_account(acct))
        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
            results.append({"account_id": acct["account_id"], "error": str(e), "arms": {}})
        # Save incrementally so a crash loses nothing.
        with open(RESULTS_PATH, "w") as f:
            json.dump({"results": results, "summary": summarize([r for r in results if r["arms"]]),
                       "pricing_usd_per_1m": APPROX_PRICING}, f, indent=2, default=str)

    print("\n=== COST / USAGE SUMMARY ===")
    print(json.dumps(summarize([r for r in results if r["arms"]]), indent=2))
    print(f"\nSaved: {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
