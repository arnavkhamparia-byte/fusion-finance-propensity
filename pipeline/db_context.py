"""
Read-only asyncpg readers for the fusion_finance_mfi DB.
Ports get_recent_interactions / get_latest_context from the production
intelligence_service (fusion_mfi_emi client only). Never writes to the DB.
"""

import os
import json
import logging
import asyncpg
from dotenv import load_dotenv

# Load .env from parent directory
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

logger = logging.getLogger("DBContext")

# DB_NAME in .env points elsewhere — this pipeline reads fusion_finance_mfi only.
DB_NAME = "fusion_finance_mfi"


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        database=DB_NAME,
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
    )


async def get_recent_interactions(account_id: int, client: str = "fusion_mfi_emi") -> list:
    """
    Fetches the latest interaction transcripts for a given account_id.
    Returns: interaction_history (list of disposition-JSON dicts, each stamped
    with a "date" key from transcript_date when missing).
    """
    query = """
        SELECT
            combined_json as response,
            created_at as transcript_date
        FROM ai_disposition_analytics
        WHERE account_id = $1
        ORDER BY created_at DESC
        LIMIT 10;
    """
    try:
        conn = await _connect()
        try:
            records = await conn.fetch(query, str(account_id))
        finally:
            await conn.close()

        interaction_history = []
        for r in records:
            response_data = r["response"]
            transcript_date = r["transcript_date"].isoformat() if r["transcript_date"] else None
            if response_data:
                if isinstance(response_data, str):
                    try:
                        response_data = json.loads(response_data)
                    except json.JSONDecodeError:
                        response_data = None

                if isinstance(response_data, dict):
                    if "date" not in response_data and transcript_date:
                        try:
                            # Extract YYYY-MM-DD from ISO format
                            response_data["date"] = transcript_date.split("T")[0]
                        except Exception:
                            pass
                    interaction_history.append(response_data)

        return interaction_history

    except Exception as e:
        logger.error(f"Failed to retrieve interaction history for account ID {account_id}: {e}")
        return []


async def get_latest_context(account_id: int, client: str = "fusion_mfi_emi") -> dict | None:
    """Return the latest stored context for an account (narrative, account_status,
    combined_intelligence) from ai_account_latest_contexts, or None."""
    query = """
        SELECT narrative, account_status, combined_intelligence
        FROM ai_account_latest_contexts
        WHERE account_id = $1
    """
    conn = await _connect()
    try:
        row = await conn.fetchrow(query, account_id)
    finally:
        await conn.close()
    if row is None:
        return None
    combined_intelligence = row["combined_intelligence"]
    # asyncpg returns jsonb as str unless a codec is registered — handle both.
    if isinstance(combined_intelligence, str):
        try:
            combined_intelligence = json.loads(combined_intelligence)
        except Exception:
            combined_intelligence = None
    return {
        "narrative": row["narrative"],
        "account_status": row["account_status"],
        "combined_intelligence": combined_intelligence,
    }
