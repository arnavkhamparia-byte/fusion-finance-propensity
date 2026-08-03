"""
Prompt builder A/B benchmark: gemini-2.5-flash (production reference) vs
gpt-5.4-mini, on identical frozen inputs.

Inputs per account come from PRODUCTION state — the narrative/account_status/
commitments/prompt_blocks stored in ai_account_latest_contexts (written by the
production Gemini pipeline) plus the disposition history. Both arms see the
exact same inputs, so differences are purely model-driven. Language is fixed
to Hindi (no-audio pipeline scope).

Accounts: the same set used by the narrative A/B (data/narrative_ab_results_run1.json),
topped up with recently-updated contexts if some of those have no stored narrative.

Run: venv/bin/python scripts/benchmark_promptbuilder_ab.py [--limit 20] [--pilot]
"""

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from dotenv import load_dotenv

load_dotenv("/home/vk/Desktop/Propensity Score/.env")

import pipeline.llm_provider as llm_provider  # noqa: E402
from pipeline.db_context import get_recent_interactions, get_latest_context  # noqa: E402
from pipeline.prompt_builder import build_prompt_blocks  # noqa: E402

RESULTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "promptbuilder_ab_results_run2.json",
)
NARRATIVE_RUN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "narrative_ab_results_run1.json",
)

ARMS = [
    {"key": "G_gemini", "model": "gemini-2.5-flash"},
    {"key": "O_gpt54mini", "model": "gpt-5.4-mini"},
]

# USD per 1M tokens — approximate; raw tokens are saved for exact repricing.
APPROX_PRICING = {
    "gemini-2.5-flash": {"text_in": 0.30, "out": 2.50},
    "gpt-5.4-mini": {"text_in": 0.25, "out": 2.00},
}


def cost_usd(model: str, usage: dict) -> float:
    p = APPROX_PRICING[model]
    return (
        usage.get("total_input_tokens", 0) / 1e6 * p["text_in"]
        + usage.get("output_tokens", 0) / 1e6 * p["out"]
    )


async def pick_accounts(limit: int) -> list:
    """Same accounts as the narrative A/B first, topped up with recently
    updated production contexts. Only accounts with a stored narrative count
    (empty narrative short-circuits to defaults without an LLM call)."""
    ids = []
    if os.path.exists(NARRATIVE_RUN_PATH):
        prev = json.load(open(NARRATIVE_RUN_PATH))
        ids = [r["account_id"] for r in prev["results"] if r.get("arms")]

    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", 5432)),
        database="fusion_finance_mfi", user=os.environ["DB_USER"], password=os.environ["DB_PASS"],
    )
    try:
        extra = await conn.fetch(
            """SELECT account_id FROM ai_account_latest_contexts
               WHERE narrative IS NOT NULL AND length(narrative) > 50
               ORDER BY updated_at DESC LIMIT $1""", limit * 3)
    finally:
        await conn.close()
    for r in extra:
        aid = int(r["account_id"])
        if aid not in ids:
            ids.append(aid)

    picked = []
    for aid in ids:
        ctx = await get_latest_context(aid)
        if not ctx or not ctx.get("narrative") or len(ctx["narrative"].strip()) < 50:
            continue
        hist = await get_recent_interactions(aid)
        picked.append({"account_id": aid, "_ctx": ctx, "_hist": hist})
        if len(picked) >= limit:
            break
    return picked


async def run_account(acct: dict) -> dict:
    ctx, hist = acct["_ctx"], acct["_hist"]
    commitments = (ctx.get("combined_intelligence") or {}).get("commitments") or []
    out = {"account_id": acct["account_id"], "arms": {}}
    for arm in ARMS:
        t0 = time.monotonic()
        llm_provider.LAST_USAGE = {}
        blocks = await build_prompt_blocks(
            narrative=ctx["narrative"],
            account_status=ctx.get("account_status") or "",
            recent_history=json.loads(json.dumps(hist, default=str)),
            default_language="Hindi",
            previous_blocks=json.loads(json.dumps(ctx.get("prompt_blocks"), default=str)) if ctx.get("prompt_blocks") else None,
            commitments=json.loads(json.dumps(commitments, default=str)),
            model=arm["model"],
            strict_amounts=True,
        )
        usage = dict(llm_provider.LAST_USAGE)
        out["arms"][arm["key"]] = {
            "model": arm["model"],
            "ok": blocks is not None,
            "prompt_blocks": blocks,
            "latency_s": round(time.monotonic() - t0, 1),
            "usage": usage,
            "cost_usd": round(cost_usd(arm["model"], usage), 6) if usage else None,
        }
        print(f"  {arm['key']}: ok={blocks is not None} "
              f"tokens={usage.get('total_input_tokens', '?')}in/{usage.get('output_tokens', '?')}out "
              f"cost=${out['arms'][arm['key']]['cost_usd']} ({out['arms'][arm['key']]['latency_s']}s)",
              flush=True)
    return out


def summarize(results: list) -> dict:
    summary = {}
    for arm in ARMS:
        k = arm["key"]
        runs = [r["arms"][k] for r in results if k in r["arms"]]
        ok = [r for r in runs if r["ok"]]
        summary[k] = {
            "model": arm["model"],
            "runs": len(runs),
            "failures": len(runs) - len(ok),
            "total_input_tokens": sum(r["usage"].get("total_input_tokens", 0) for r in ok),
            "total_output_tokens": sum(r["usage"].get("output_tokens", 0) for r in ok),
            "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in ok), 4),
            "avg_cost_per_call_usd": round(sum(r["cost_usd"] or 0 for r in ok) / len(ok), 6) if ok else None,
            "avg_latency_s": round(sum(r["latency_s"] for r in ok) / len(ok), 1) if ok else None,
        }
    # Version agreement between the two arms
    both = [r for r in results
            if r["arms"].get("G_gemini", {}).get("ok") and r["arms"].get("O_gpt54mini", {}).get("ok")]
    if both:
        per_block = {}
        full_match = 0
        for r in both:
            g = r["arms"]["G_gemini"]["prompt_blocks"]
            o = r["arms"]["O_gpt54mini"]["prompt_blocks"]
            all_same = True
            for block in g:
                same = g[block]["version"] == o[block]["version"]
                per_block.setdefault(block, [0, 0])
                per_block[block][0] += int(same)
                per_block[block][1] += 1
                all_same = all_same and same
            full_match += int(all_same)
        summary["_version_agreement"] = {
            "accounts_compared": len(both),
            "all_blocks_identical": full_match,
            "per_block": {b: f"{m}/{n}" for b, (m, n) in sorted(per_block.items())},
        }
    return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args()
    limit = 2 if args.pilot else args.limit

    accounts = await pick_accounts(limit)
    print(f"Selected {len(accounts)} accounts with production contexts", flush=True)

    results = []
    for i, acct in enumerate(accounts, 1):
        print(f"[{i}/{len(accounts)}] account {acct['account_id']}", flush=True)
        try:
            results.append(await run_account(acct))
        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
            results.append({"account_id": acct["account_id"], "error": str(e), "arms": {}})
        with open(RESULTS_PATH, "w") as f:
            json.dump({"results": results, "summary": summarize([r for r in results if r["arms"]]),
                       "pricing_usd_per_1m": APPROX_PRICING}, f, indent=2, default=str)

    print("\n=== SUMMARY ===")
    print(json.dumps(summarize([r for r in results if r["arms"]]), indent=2))
    print(f"\nSaved: {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
