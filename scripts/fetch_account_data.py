"""
Step 1: Fetch account data from DB for all 30 accounts in the recordings folder.

For each loan number (filename), pulls:
  - account_details: core account info
  - activity_taskactivity: last 15 AI call entries (disposition history)
  - account_payments: payment history

Output: data/account_data.json
"""

import os
import json
import asyncio
import asyncpg
from dotenv import load_dotenv

# Load .env from parent directory
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
OUTPUT_FILE   = os.path.join(BASE_DIR, "data", "account_data.json")


def get_loan_numbers():
    """Extract loan numbers from recording filenames."""
    numbers = []
    for f in sorted(os.listdir(RECORDINGS_DIR)):
        if f.lower().endswith((".mp3", ".wav", ".ogg")):
            numbers.append(os.path.splitext(f)[0])
    return numbers


async def fetch_all(loan_numbers: list) -> dict:
    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
    )

    results = {}

    for loan_number in loan_numbers:
        print(f"  Fetching: {loan_number}", end=" ... ")

        # ── Account details ──────────────────────────────────────────
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
            print("NOT FOUND — skipping")
            continue

        acct = dict(account)
        account_id = acct["id"]

        # Decimals → float for JSON
        for col in ("loan_amount", "emi_amount", "total_amount_pending"):
            if acct.get(col) is not None:
                acct[col] = float(acct[col])

        # ── Call history (last 15 AI calls) ──────────────────────────
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

        # ── Payment history ───────────────────────────────────────────
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
            "account": acct,
            "call_history": call_history,
            "payments": payments,
        }

        print(f"OK  ({acct['name']}, DPD: {acct['dpd_bucket']}, "
              f"{len(call_history)} calls, {len(payments)} payments)")

    await conn.close()
    return results


async def main():
    loan_numbers = get_loan_numbers()
    print(f"\nFound {len(loan_numbers)} recordings in {RECORDINGS_DIR}\n")

    if not loan_numbers:
        print("No recordings found. Place .mp3 files in the recordings/ folder.")
        return

    data = await fetch_all(loan_numbers)

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"\nDone. Data for {len(data)}/{len(loan_numbers)} accounts saved to:")
    print(f"  {OUTPUT_FILE}")

    missing = set(loan_numbers) - set(data.keys())
    if missing:
        print(f"\nWARNING — {len(missing)} loan numbers not found in DB:")
        for m in sorted(missing):
            print(f"  {m}")


if __name__ == "__main__":
    asyncio.run(main())
