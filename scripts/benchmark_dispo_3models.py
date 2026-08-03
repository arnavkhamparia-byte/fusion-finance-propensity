"""
Three-model disposition benchmark on TODAY's calls.

100 EMI calls from today (connected, >20s, with recording; latest call per
account so the stored production disposition is the comparison truth). Each
call runs through the two-stage pipeline (signal extractor + classifier) on:

  gemini-2.5-flash | gemini-3-flash-preview | gemini-3.6-flash

Caching: Vertex implicit caching (enabled by default on 2.5+/3.x) — the 32KB
static system prompt is a stable prefix; cache-hit tokens are read from
usage_metadata.cached_content_token_count and priced at the 75%-discount rate.
Thinking tokens (3.x models) are captured from thoughts_token_count and priced
at the output rate.

Run: venv/bin/python scripts/benchmark_dispo_3models.py [--pilot] [--limit 100]
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

import asyncpg
import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv("/home/vk/Desktop/Propensity Score/.env")

import pipeline.llm_provider as llm_provider  # noqa: E402
from pipeline.signal_extractor_emi import extract_signals, ExtractionIncompleteError  # noqa: E402
from pipeline.emi_classifier import classify  # noqa: E402
from pipeline.db_context import get_recent_interactions  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(ROOT, "data", "dispo_3models_2026-08-03.json")
AUDIO_DIR = "/home/vk/.claude/jobs/d513ca17/tmp/dispo3_audio"
BUCKET = "tata-bot-calls"
TODAY = "2026-08-03"

MODELS = ["gemini-2.5-flash", "gemini-3-flash-preview", "gemini-3.6-flash"]

# USD per 1M tokens. cached_in = implicit-cache hit rate (75% off input).
# thinking billed at the output rate. 3.6-flash audio rate not published
# separately — assumed equal to its text input rate (marked approx).
PRICING = {
    "gemini-2.5-flash":       {"text_in": 0.30, "cached_in": 0.075, "audio_in": 1.00, "out": 2.50},
    "gemini-3-flash-preview": {"text_in": 0.50, "cached_in": 0.125, "audio_in": 1.00, "out": 3.00},
    "gemini-3.6-flash":       {"text_in": 1.50, "cached_in": 0.375, "audio_in": 1.50, "out": 7.50},
    "gemini-3.1-flash-lite":  {"text_in": 0.25, "cached_in": 0.0625, "audio_in": 0.50, "out": 1.50},
    "gemini-3.5-flash":       {"text_in": 1.50, "cached_in": 0.375, "audio_in": 1.50, "out": 9.00},
}

SELECT_SQL = """
SELECT DISTINCT ON (t.account_id)
    t.account_id, t.call_duration, t.call_recording_url,
    to_char(t.processed_at AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD') AS call_date_ist,
    t.processed_at::text AS processed_at
FROM activity_taskactivity t
WHERE t.provider = 'Tata' AND t.activity_type = 'AI Call'
  AND t.task_status = 'Connected' AND t.disposition IS NOT NULL
  AND t.call_recording_url IS NOT NULL AND t.call_duration > 20
  AND t.flow = 'fusion_mfi_emi'
  AND (t.processed_at AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-08-03'
  AND NOT EXISTS (SELECT 1 FROM activity_taskactivity t2
                  WHERE t2.account_id = t.account_id
                    AND t2.activity_type = 'AI Call'
                    AND t2.processed_at > t.processed_at)
ORDER BY t.account_id, t.processed_at DESC
"""

_CALL_DATE_FORMATS = ("%A, %B %d, %Y %I:%M %p", "%Y-%m-%d")


def _row_date(row):
    raw = (row or {}).get("date")
    if not raw:
        return None
    for fmt in _CALL_DATE_FORMATS:
        try:
            return datetime.strptime(str(raw).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return str(raw)[:10]


def unwrap(row):
    if isinstance(row, dict) and isinstance(row.get("output"), dict):
        flat = dict(row["output"])
        flat.setdefault("date", row.get("date"))
        return flat
    return row


def cost_usd(model, usage):
    p = PRICING[model]
    cached = usage.get("cached_input_tokens", 0)
    text_in = max(usage.get("text_input_tokens", 0) - cached, 0)
    return (text_in / 1e6 * p["text_in"] + cached / 1e6 * p["cached_in"]
            + usage.get("audio_input_tokens", 0) / 1e6 * p["audio_in"]
            + (usage.get("output_tokens", 0) + usage.get("thinking_tokens", 0)) / 1e6 * p["out"])


def download_audio(key, account_id):
    os.makedirs(AUDIO_DIR, exist_ok=True)
    path = os.path.join(AUDIO_DIR, f"{account_id}.mp3")
    if not os.path.exists(path):
        s3 = boto3.client("s3", region_name="ap-south-1",
                          config=Config(signature_version="s3v4"),
                          endpoint_url="https://s3.ap-south-1.amazonaws.com")
        s3.download_file(BUCKET, key, path)
    return path


async def run_account(acct):
    account_id = int(acct["account_id"])
    duration = float(acct["call_duration"])
    current_dt = f"{acct['call_date_ist']}T12:00:00+05:30"
    out = {"account_id": account_id, "call_date_ist": acct["call_date_ist"],
           "call_duration_s": acct["call_duration"], "arms": {}}

    audio_path = await asyncio.to_thread(download_audio, acct["call_recording_url"], account_id)
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    prod_hist = await get_recent_interactions(account_id)
    prod = unwrap(prod_hist[0]) if prod_hist else {}
    out["prod_disposition"] = prod.get("disposition")
    out["prod_sub"] = prod.get("sub_disposition")
    out["prod_row_is_call_date"] = _row_date(prod) == acct["call_date_ist"]

    for model in MODELS:
        t0 = time.monotonic()
        llm_provider.reset_usage()
        try:
            try:
                signals = await extract_signals(audio_bytes, "audio/mpeg", current_dt,
                                                model=model, include_language=True)
            except (ExtractionIncompleteError, json.JSONDecodeError):
                llm_provider.reset_usage()
                signals = await extract_signals(audio_bytes, "audio/mpeg", current_dt,
                                                model=model, include_language=True,
                                                max_output_tokens=16000)
            output = classify(signals, duration, current_dt)
            usage = llm_provider.get_usage()
            out["arms"][model] = {
                "disposition": output.get("disposition"),
                "sub_disposition": output.get("sub_disposition"),
                "language": signals.get("language"),
                "match": output.get("disposition") == prod.get("disposition"),
                "sub_match": output.get("sub_disposition") == prod.get("sub_disposition"),
                "usage": usage,
                "cost_usd": round(cost_usd(model, usage), 6),
                "latency_s": round(time.monotonic() - t0, 1),
            }
        except Exception as e:
            out["arms"][model] = {"error": f"{type(e).__name__}: {e}",
                                  "latency_s": round(time.monotonic() - t0, 1)}
    return out


def summarize(results):
    s = {"accounts": len(results)}
    valid = [r for r in results if r.get("prod_disposition") and r.get("prod_row_is_call_date")]
    s["valid_reference"] = len(valid)
    for m in MODELS:
        arms = [r["arms"][m] for r in valid if m in r["arms"] and "error" not in r["arms"][m]]
        errs = [r for r in valid if "error" in r["arms"].get(m, {})]
        u = {k: sum(a["usage"].get(k, 0) for a in arms) for k in
             ("text_input_tokens", "cached_input_tokens", "audio_input_tokens",
              "output_tokens", "thinking_tokens")}
        s[m] = {
            "runs": len(arms), "errors": len(errs),
            "disposition_match": f"{sum(a['match'] for a in arms)}/{len(arms)}",
            "sub_match": f"{sum(a['sub_match'] for a in arms)}/{len(arms)}",
            "languages": dict(Counter(a["language"] for a in arms)),
            "tokens": u,
            "cache_hit_rate": round(u["cached_input_tokens"] / u["text_input_tokens"], 3) if u["text_input_tokens"] else 0,
            "total_cost_usd": round(sum(a["cost_usd"] for a in arms), 4),
            "avg_cost_usd": round(sum(a["cost_usd"] for a in arms) / len(arms), 6) if arms else None,
            "avg_latency_s": round(sum(a["latency_s"] for a in arms) / len(arms), 1) if arms else None,
            "confusions": [{"prod": p, "ours": o, "n": n} for (p, o), n in Counter(
                (r["prod_disposition"], r["arms"][m]["disposition"]) for r in valid
                if m in r["arms"] and "error" not in r["arms"][m] and not r["arms"][m]["match"]
            ).most_common(6)],
        }
    return s


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--models", default=None, help="comma-separated model list override")
    parser.add_argument("--out", default=None)
    parser.add_argument("--accounts-from", default=None,
                        help="reuse account set from a prior results JSON")
    args = parser.parse_args()
    global MODELS, RESULTS_PATH
    if args.models:
        MODELS = args.models.split(",")
    if args.out:
        RESULTS_PATH = args.out
    limit = 2 if args.pilot else args.limit

    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", 5432)),
        database="fusion_finance_mfi", user=os.environ["DB_USER"], password=os.environ["DB_PASS"])
    try:
        if args.accounts_from:
            prev = json.load(open(args.accounts_from))["results"]
            ids = [r["account_id"] for r in prev]
            rows = await conn.fetch(
                """SELECT account_id, call_duration, call_recording_url,
                          to_char(processed_at AT TIME ZONE 'Asia/Kolkata','YYYY-MM-DD') AS call_date_ist,
                          processed_at::text AS processed_at
                   FROM activity_taskactivity t
                   WHERE t.account_id = ANY($1::int[]) AND t.flow='fusion_mfi_emi'
                     AND t.activity_type='AI Call'
                     AND (t.processed_at AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-08-03'
                     AND t.call_duration > 20
                   ORDER BY t.account_id, t.processed_at DESC""", ids)
            seen = set()
            rows = [r for r in rows if r["account_id"] not in seen and not seen.add(r["account_id"])]
        else:
            rows = await conn.fetch(SELECT_SQL)
    finally:
        await conn.close()
    rows = sorted(rows, key=lambda r: r["processed_at"], reverse=True)[:limit]
    print(f"Selected {len(rows)} accounts (today, EMI, >20s, latest call)", flush=True)

    sem = asyncio.Semaphore(args.workers)
    results, lock = [], asyncio.Lock()

    def save():
        with open(RESULTS_PATH, "w") as f:
            json.dump({"results": results, "summary": summarize(results),
                       "pricing_usd_per_1m": PRICING}, f, indent=2, default=str)

    async def worker(row):
        async with sem:
            try:
                res = await run_account(dict(row))
            except Exception as e:
                res = {"account_id": row["account_id"], "arms": {},
                       "error": f"{type(e).__name__}: {e}"}
        async with lock:
            results.append(res)
            n = len(results)
            marks = " ".join(f"{m.split('-',1)[1]}={a.get('match')}" for m, a in res.get("arms", {}).items())
            print(f"[{n}/{len(rows)}] {res['account_id']} prod={res.get('prod_disposition')!r} {marks}", flush=True)
            if n % 10 == 0 or n == len(rows):
                save()

    await asyncio.gather(*(worker(r) for r in rows))
    save()
    print("\n=== SUMMARY ===")
    print(json.dumps(summarize(results), indent=2))
    print(f"\nSaved: {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
