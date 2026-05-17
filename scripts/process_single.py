"""
One-off: fetch DB data + run Gemini analysis for a single loan number,
then merge the result into the existing propensity_results.json and re-rank.

Usage:
    python3 scripts/process_single.py 4465864972
"""

import os
import sys
import json
import asyncio
import asyncpg
import time
import logging
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from google import genai
from google.genai import types

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
ACCOUNT_DATA   = os.path.join(BASE_DIR, "data", "account_data.json")
OUTPUT_FILE    = os.path.join(BASE_DIR, "data", "propensity_results.json")

load_dotenv(os.path.join(BASE_DIR, ".env"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prompts import PROPENSITY_PROMPT
from analyze_recordings import (
    analyze_audio,
    calculate_propensity_score,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("SingleProcessor")

# ── DB fetch ───────────────────────────────────────────────────────────────

async def fetch_one(loan_number: str) -> dict | None:
    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
    )

    account = await conn.fetchrow(
        """
        SELECT id, loan_number, name, city,
               loan_amount, dpd_bucket, emi_amount, total_amount_pending,
               assigned_to_id
        FROM account_details
        WHERE loan_number = $1
        """,
        loan_number,
    )

    if not account:
        await conn.close()
        return None

    acct = dict(account)
    account_id = acct["id"]
    for col in ("loan_amount", "emi_amount", "total_amount_pending"):
        if acct.get(col) is not None:
            acct[col] = float(acct[col])

    rows = await conn.fetch(
        """
        SELECT id, disposition, sentiment, summary,
               call_duration, processed_at, channel
        FROM activity_taskactivity
        WHERE account_id = $1
          AND activity_type = 'AI Call'
        ORDER BY processed_at DESC NULLS LAST
        LIMIT 15
        """,
        account_id,
    )
    call_history = []
    for r in rows:
        entry = dict(r)
        if entry.get("processed_at"):
            entry["processed_at"] = entry["processed_at"].isoformat()
        call_history.append(entry)

    pay_rows = await conn.fetch(
        """
        SELECT id, amount_paid, payment_date, payment_status
        FROM account_payments
        WHERE account_id = $1
        ORDER BY payment_date DESC NULLS LAST
        LIMIT 10
        """,
        account_id,
    )
    payments = []
    for r in pay_rows:
        entry = dict(r)
        if entry.get("payment_date"):
            entry["payment_date"] = str(entry["payment_date"])
        if entry.get("amount_paid") is not None:
            entry["amount_paid"] = float(entry["amount_paid"])
        payments.append(entry)

    await conn.close()
    return {"account": acct, "call_history": call_history, "payments": payments}


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/process_single.py <loan_number>")
        sys.exit(1)

    loan_number = sys.argv[1].strip()
    audio_path  = os.path.join(RECORDINGS_DIR, f"{loan_number}.mp3")

    if not os.path.exists(audio_path):
        log.error(f"Recording not found: {audio_path}")
        sys.exit(1)

    # ── Fetch DB data ──────────────────────────────────────────────────────
    log.info(f"Fetching DB data for {loan_number} ...")
    db_entry = asyncio.run(fetch_one(loan_number))

    if not db_entry:
        log.error(f"Loan {loan_number} not found in DB. Check the loan number.")
        sys.exit(1)

    log.info(f"Found: {db_entry['account']['name']} | DPD: {db_entry['account']['dpd_bucket']}")

    # ── Update account_data.json ───────────────────────────────────────────
    if os.path.exists(ACCOUNT_DATA):
        with open(ACCOUNT_DATA) as f:
            account_data = json.load(f)
    else:
        account_data = {}

    account_data[loan_number] = db_entry
    with open(ACCOUNT_DATA, "w") as f:
        json.dump(account_data, f, indent=2, default=str)
    log.info(f"account_data.json updated with {loan_number}")

    # ── Gemini analysis ────────────────────────────────────────────────────
    ist = timezone(timedelta(hours=5, minutes=30))
    current_dt = datetime.now(ist).strftime("%A, %B %d, %Y %I:%M %p")

    log.info("Sending audio to Gemini ...")
    llm_output = analyze_audio(audio_path, current_dt)
    log.info(f"Disposition: {llm_output.get('disposition')} | "
             f"Engagement: {llm_output.get('engagement_level')} | "
             f"Commitment: {llm_output.get('commitment_strength')}")

    # ── Score ──────────────────────────────────────────────────────────────
    acct         = db_entry["account"]
    call_history = db_entry["call_history"]
    payments     = db_entry["payments"]

    if call_history:
        acct["call_duration"] = call_history[0].get("call_duration")

    scoring = calculate_propensity_score(llm_output, acct, call_history, payments)
    log.info(f"Score: {scoring['propensity_score']} ({scoring['tier']})")

    # ── Build result record ────────────────────────────────────────────────
    new_record = {
        "loan_number":         loan_number,
        "name":                acct.get("name"),
        "city":                acct.get("city"),
        "loan_amount":         acct.get("loan_amount"),
        "emi_amount":          acct.get("emi_amount"),
        "total_amount_pending": acct.get("total_amount_pending"),
        "dpd_bucket":          acct.get("dpd_bucket"),
        "assigned_to_id":      acct.get("assigned_to_id"),
        "disposition":         llm_output.get("disposition"),
        "sentiment":           llm_output.get("sentiment"),
        "summary":             llm_output.get("summary"),
        "commitment_strength": llm_output.get("commitment_strength"),
        "promise_made":        llm_output.get("promise_made"),
        "promise_date":        llm_output.get("promise_date"),
        "barrier_type":        llm_output.get("barrier_type"),
        "engagement_level":    llm_output.get("engagement_level"),
        "customer_initiated_resolution": llm_output.get("customer_initiated_resolution"),
        "tone_shift":          llm_output.get("tone_shift"),
        "specific_amount_discussed": llm_output.get("specific_amount_discussed"),
        "propensity_score":    scoring["propensity_score"],
        "tier":                scoring["tier"],
        "key_reasons":         scoring["key_reasons"],
        "score_breakdown":     scoring["score_breakdown"],
        "total_calls":         len(call_history),
        "previous_dispositions": [c.get("disposition") for c in call_history[:5]],
        "total_payments":      len(payments),
        "last_payment_date":   payments[0]["payment_date"] if payments else None,
        "audio_file":          f"{loan_number}.mp3",
        "analysed_at":         datetime.now(ist).isoformat(),
    }

    # ── Merge into existing results ────────────────────────────────────────
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        accounts = existing.get("accounts", [])
    else:
        existing = {}
        accounts = []

    # Remove any existing entry for this loan (in case of re-run)
    accounts = [a for a in accounts if a["loan_number"] != loan_number]
    accounts.append(new_record)

    # Re-rank
    accounts.sort(key=lambda x: x["propensity_score"], reverse=True)
    for rank, r in enumerate(accounts, 1):
        r["rank"] = rank

    output = {
        "generated_at":   datetime.now(ist).isoformat(),
        "total_analysed": len(accounts),
        "total_errors":   existing.get("total_errors", 0),
        "tier_summary": {
            "High":   sum(1 for r in accounts if r["tier"] == "High"),
            "Medium": sum(1 for r in accounts if r["tier"] == "Medium"),
            "Low":    sum(1 for r in accounts if r["tier"] == "Low"),
        },
        "accounts": accounts,
        "errors":   [e for e in existing.get("errors", []) if e.get("loan_number") != loan_number],
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\n" + "=" * 55)
    print(f"  DONE — {loan_number} ({new_record['name']})")
    print(f"  Score  : {scoring['propensity_score']} ({scoring['tier']})")
    print(f"  Rank   : #{new_record['rank']} of {len(accounts)}")
    print(f"  Total accounts in results: {len(accounts)}")
    print("=" * 55)


if __name__ == "__main__":
    main()
