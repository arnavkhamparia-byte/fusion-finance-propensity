"""
Step 1: Fetch account data from DB using a batch query.

Replaces the old approach of reading loan numbers from local filenames.
Now discovers accounts directly from the DB — no MP3 files needed locally.

For each account returned by the batch query, also fetches:
  - call history  (last 15 AI calls)
  - payment history

Output: data/account_data.json
  Each entry includes call_recording_url for use by analyze_recordings.py.
"""

import os
import json
import asyncio
import asyncpg
from dotenv import load_dotenv

# Load .env from parent directory
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(BASE_DIR, "data", "account_data.json")


async def fetch_all() -> dict:
    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
    )

    # ── Batch query: most recent positive-disposition AI call per account ─────
    # DISTINCT ON ensures one row per loan_number (most recent call wins).
    # Adjust assigned_to_id values and INTERVAL as needed.
    batch_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (ad.loan_number)
            ad.id             AS account_id,
            ad.loan_number,
            ad.name,
            ad.city,
            ad.loan_amount,
            ad.dpd_bucket,
            ad.emi_amount,
            ad.total_amount_pending,
            ad.assigned_to_id,
            t.call_recording_url,
            t.disposition      AS qualifying_disposition,
            t.call_duration    AS qualifying_call_duration,
            t.processed_at     AS latest_call_at
        FROM activity_taskactivity t
        JOIN account_details ad ON ad.id = t.account_id
        WHERE t.activity_type = 'AI Call'
          AND t.disposition IN (
              'Agree To Senior Manager Call',
              'Financial Hardship',
              'Requested Settlement'
          )
          AND ad.assigned_to_id IN (50, 68)
          AND t.processed_at >= CURRENT_DATE - INTERVAL '13 days'
        ORDER BY ad.loan_number, t.processed_at DESC
        """
    )

    print(f"\nBatch query returned {len(batch_rows)} accounts\n")

    results = {}

    for row in batch_rows:
        acct_base = dict(row)
        loan_number = acct_base["loan_number"]
        account_id  = acct_base["account_id"]

        print(f"  Fetching: {loan_number}", end=" ... ")

        # Decimals → float for JSON serialisation
        for col in ("loan_amount", "emi_amount", "total_amount_pending"):
            if acct_base.get(col) is not None:
                acct_base[col] = float(acct_base[col])

        if acct_base.get("latest_call_at"):
            acct_base["latest_call_at"] = acct_base["latest_call_at"].isoformat()

        # ── Call history (last 15 AI calls) ──────────────────────────────────
        history_rows = await conn.fetch(
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
        for r in history_rows:
            entry = dict(r)
            if entry.get("processed_at"):
                entry["processed_at"] = entry["processed_at"].isoformat()
            call_history.append(entry)

        # ── Payment history ───────────────────────────────────────────────────
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

        results[loan_number] = {
            "account":                    acct_base,
            "call_recording_url":         acct_base.get("call_recording_url"),
            "qualifying_disposition":     acct_base.get("qualifying_disposition"),
            "qualifying_call_duration":   acct_base.get("qualifying_call_duration"),
            "call_history":               call_history,
            "payments":                   payments,
        }

        print(f"OK  ({acct_base['name']}, DPD: {acct_base['dpd_bucket']}, "
              f"{len(call_history)} calls, {len(payments)} payments, "
              f"recording: {'yes' if acct_base.get('call_recording_url') else 'MISSING'})")

    await conn.close()
    return results


async def main():
    data = await fetch_all()

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

    missing_url = [ln for ln, v in data.items() if not v.get("call_recording_url")]
    print(f"\nDone. {len(data)} accounts saved to:")
    print(f"  {OUTPUT_FILE}")
    if missing_url:
        print(f"\nWARNING — {len(missing_url)} accounts have no call_recording_url:")
        for ln in missing_url:
            print(f"  {ln}")


if __name__ == "__main__":
    asyncio.run(main())
