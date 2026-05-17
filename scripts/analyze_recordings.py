"""
Step 2: Analyze recordings with Gemini and compute propensity scores.

For each MP3 in recordings/:
  1. Sends audio to Gemini 2.5 Flash (Vertex AI) with extended propensity prompt
  2. Parses 13-field JSON response
  3. Combines LLM output with DB account data (from account_data.json)
  4. Calculates propensity score (0–100) using weighted formula
  5. Ranks all accounts

Output: data/propensity_results.json
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load .env
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from prompts import PROPENSITY_PROMPT

# ─────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDINGS_DIR  = os.path.join(BASE_DIR, "recordings")
ACCOUNT_DATA    = os.path.join(BASE_DIR, "data", "account_data.json")
OUTPUT_FILE     = os.path.join(BASE_DIR, "data", "propensity_results.json")

# ─────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("PropensityAnalyzer")

# ─────────────────────────────────────────────────────────────────
# Gemini client  (mirrors existing disposition agent pattern)
# ─────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GOOGLE_API_KEY")
MODEL_NAME     = "gemini-2.5-flash"

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    client = genai.Client(
        vertexai=True,
        project=os.environ.get("GCP_PROJECT_ID", "vertex-gemini-oto-cms"),
        location=os.environ.get("GCP_LOCATION", "us-central1"),
    )

# ─────────────────────────────────────────────────────────────────
# Scoring helpers
# ─────────────────────────────────────────────────────────────────

DISPOSITION_SCORES = {
    "Agree To Senior Manager Call": 1.00,
    "Requested Settlement":         0.90,
    "Call Back Requested":          0.55,
    "Information Conveyed":         0.30,
    "Unclear":                      0.25,
    "Connected":                    0.20,
    "Call Hang Up":                 0.15,
    "Financial Hardship":           0.15,
    "Dispute":                      0.10,
    "Refuse To Pay":                0.05,
    "Busy":                         0.10,
    "Wrong Number":                 0.05,
    "not-connected":                0.05,
    "no-answer":                    0.10,
    "Failed":                       0.05,
}

COMMITMENT_SCORES  = {"strong": 1.0, "moderate": 0.6, "weak": 0.25, "none": 0.0}
ENGAGEMENT_SCORES  = {"high": 1.0, "medium": 0.55, "low": 0.15}
SENTIMENT_SCORES   = {"positive": 1.0, "neutral": 0.5, "negative": 0.1}
TONE_SHIFT_BONUS   = {"improved": 5, "neutral": 0, "worsened": -3}


def parse_dpd_score(dpd_bucket: str) -> float:
    """Convert DPD bucket string to a 0–1 score. Lower DPD = higher score."""
    if not dpd_bucket:
        return 0.3
    b = dpd_bucket.upper()
    if "SETTLEMENT NOT ALLOWED" in b:
        return 0.05
    if "SPOD" in b:
        return 0.12
    if ">365" in b or "365" in b:
        return 0.10
    match = re.search(r"(\d+)", b)
    if match:
        days = int(match.group(1))
        if days <= 30:   return 0.90
        if days <= 60:   return 0.75
        if days <= 90:   return 0.60
        if days <= 120:  return 0.45
        if days <= 150:  return 0.32
        if days <= 180:  return 0.20
        return 0.10
    return 0.30


def call_duration_score(duration_seconds) -> float:
    """Longer meaningful calls = higher signal. Very short = no conversation."""
    if not duration_seconds:
        return 0.2
    d = int(duration_seconds)
    if d < 20:   return 0.10   # near-silent / no answer
    if d < 45:   return 0.25   # very short
    if d < 90:   return 0.55   # short but real conversation
    if d < 180:  return 0.85   # good conversation length
    return 1.00                # long call — strong engagement


def history_trend_score(call_history: list) -> float:
    """
    Score based on disposition pattern in call history.
    Recent positive dispositions push score up; repeated negatives push down.
    """
    if not call_history:
        return 0.3

    positive = {"Agree To Senior Manager Call", "Requested Settlement", "Call Back Requested"}
    negative = {"Refuse To Pay"}

    weighted_score = 0.0
    total_weight   = 0.0

    for i, call in enumerate(call_history[:8]):  # look at last 8 calls
        weight = 1.0 / (i + 1)                   # most recent call weighs most
        disp   = call.get("disposition", "")
        if disp in positive:
            weighted_score += weight * 1.0
        elif disp in negative:
            weighted_score += weight * 0.0
        else:
            weighted_score += weight * 0.4
        total_weight += weight

    return weighted_score / total_weight if total_weight else 0.3


def calculate_propensity_score(llm: dict, account: dict, call_history: list, payments: list) -> dict:
    """
    Weighted formula combining LLM propensity signals + DB context.

    Weights:
      Disposition           30%
      Commitment strength   20%
      Engagement level      15%
      Sentiment             10%
      Call duration          5%
      History trend         10%
      DPD bucket            10%

    Bonus points (out of 100):
      promise_made                  +5
      customer_initiated_resolution +5
      specific_amount_discussed     +3
      tone_shift                    ±0–5
    """
    w_disp        = 0.30
    w_commitment  = 0.20
    w_engagement  = 0.15
    w_sentiment   = 0.10
    w_duration    = 0.05
    w_history     = 0.10
    w_dpd         = 0.10

    disp       = llm.get("disposition", "")
    s_disp     = DISPOSITION_SCORES.get(disp, 0.2)
    s_commit   = COMMITMENT_SCORES.get(llm.get("commitment_strength", "none"), 0.0)
    s_engage   = ENGAGEMENT_SCORES.get(llm.get("engagement_level", "low"), 0.15)
    s_sentiment= SENTIMENT_SCORES.get(llm.get("sentiment", "neutral"), 0.5)
    s_duration = call_duration_score(account.get("call_duration"))
    s_history  = history_trend_score(call_history)
    s_dpd      = parse_dpd_score(account.get("dpd_bucket", ""))

    base = (
        s_disp      * w_disp     +
        s_commit    * w_commitment +
        s_engage    * w_engagement +
        s_sentiment * w_sentiment  +
        s_duration  * w_duration   +
        s_history   * w_history    +
        s_dpd       * w_dpd
    ) * 100  # scale to 0–100

    # Bonus signals
    bonus = 0
    if llm.get("promise_made"):                    bonus += 5
    if llm.get("customer_initiated_resolution"):   bonus += 5
    if llm.get("specific_amount_discussed"):       bonus += 3
    bonus += TONE_SHIFT_BONUS.get(llm.get("tone_shift", "neutral"), 0)

    raw_score = min(round(base + bonus, 1), 100)

    # Tier assignment
    if raw_score >= 65:
        tier = "High"
    elif raw_score >= 40:
        tier = "Medium"
    else:
        tier = "Low"

    # Build human-readable key reasons
    reasons = []
    if llm.get("promise_made"):
        reasons.append(f"Promise to pay by {llm.get('promise_date') or 'specific date'}")
    if llm.get("customer_initiated_resolution"):
        reasons.append("Customer asked about resolution options")
    if llm.get("commitment_strength") in ("strong", "moderate"):
        reasons.append(f"{llm['commitment_strength'].capitalize()} commitment expressed")
    if llm.get("tone_shift") == "improved":
        reasons.append("Tone improved during call")
    if llm.get("specific_amount_discussed"):
        reasons.append("Specific payment amount discussed")
    if not reasons:
        reasons.append(disp or "No strong positive signal")

    return {
        "propensity_score": raw_score,
        "tier":             tier,
        "key_reasons":      reasons,
        "score_breakdown": {
            "disposition_score":   round(s_disp * w_disp * 100, 1),
            "commitment_score":    round(s_commit * w_commitment * 100, 1),
            "engagement_score":    round(s_engage * w_engagement * 100, 1),
            "sentiment_score":     round(s_sentiment * w_sentiment * 100, 1),
            "duration_score":      round(s_duration * w_duration * 100, 1),
            "history_score":       round(s_history * w_history * 100, 1),
            "dpd_score":           round(s_dpd * w_dpd * 100, 1),
            "bonus_points":        bonus,
        },
    }


# ─────────────────────────────────────────────────────────────────
# Gemini audio analysis
# ─────────────────────────────────────────────────────────────────

def analyze_audio(audio_path: str, current_dt: str) -> dict:
    """Send MP3 to Gemini and return parsed 13-field JSON."""
    mime_type = "audio/mpeg" if audio_path.lower().endswith(".mp3") else "audio/wav"

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    prompt_text = PROPENSITY_PROMPT.replace("{current_datetime}", current_dt)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            prompt_text,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    raw = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main():
    # ── Load DB data ─────────────────────────────────────────────
    if not os.path.exists(ACCOUNT_DATA):
        log.error(f"account_data.json not found at {ACCOUNT_DATA}")
        log.error("Run fetch_account_data.py first.")
        return

    with open(ACCOUNT_DATA) as f:
        account_data = json.load(f)

    log.info(f"Loaded DB data for {len(account_data)} accounts")

    # ── IST timestamp ─────────────────────────────────────────────
    ist = timezone(timedelta(hours=5, minutes=30))
    current_dt = datetime.now(ist).strftime("%A, %B %d, %Y %I:%M %p")

    # ── Process each recording ────────────────────────────────────
    recordings = sorted([
        f for f in os.listdir(RECORDINGS_DIR)
        if f.lower().endswith((".mp3", ".wav", ".ogg"))
    ])

    log.info(f"Found {len(recordings)} recordings to process\n")

    results = []
    errors  = []

    for idx, filename in enumerate(recordings, 1):
        loan_number = os.path.splitext(filename)[0]
        audio_path  = os.path.join(RECORDINGS_DIR, filename)

        log.info(f"[{idx}/{len(recordings)}] Processing {loan_number}")

        # DB data for this account
        db_entry = account_data.get(loan_number)
        if not db_entry:
            log.warning(f"  No DB data for {loan_number} — skipping")
            errors.append({"loan_number": loan_number, "error": "No DB data found"})
            continue

        acct         = db_entry["account"]
        call_history = db_entry["call_history"]
        payments     = db_entry["payments"]

        # ── Gemini analysis ──────────────────────────────────────
        try:
            llm_output = analyze_audio(audio_path, current_dt)
            log.info(f"  Disposition: {llm_output.get('disposition')} | "
                     f"Engagement: {llm_output.get('engagement_level')} | "
                     f"Commitment: {llm_output.get('commitment_strength')}")
        except Exception as e:
            log.error(f"  Gemini error: {e}")
            errors.append({"loan_number": loan_number, "error": str(e)})
            time.sleep(2)
            continue

        # ── Score ─────────────────────────────────────────────────
        # Merge call_duration from DB into acct dict for the scoring function
        if call_history:
            acct["call_duration"] = call_history[0].get("call_duration")

        scoring = calculate_propensity_score(llm_output, acct, call_history, payments)

        log.info(f"  Score: {scoring['propensity_score']} ({scoring['tier']})")

        # ── Build result record ───────────────────────────────────
        results.append({
            "loan_number":         loan_number,
            "name":                acct.get("name"),
            "city":                acct.get("city"),
            "loan_amount":         acct.get("loan_amount"),
            "emi_amount":          acct.get("emi_amount"),
            "total_amount_pending": acct.get("total_amount_pending"),
            "dpd_bucket":          acct.get("dpd_bucket"),
            "assigned_to_id":      acct.get("assigned_to_id"),
            # LLM fields
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
            # Scoring
            "propensity_score":    scoring["propensity_score"],
            "tier":                scoring["tier"],
            "key_reasons":         scoring["key_reasons"],
            "score_breakdown":     scoring["score_breakdown"],
            # History
            "total_calls":         len(call_history),
            "previous_dispositions": [c.get("disposition") for c in call_history[:5]],
            "total_payments":      len(payments),
            "last_payment_date":   payments[0]["payment_date"] if payments else None,
            # Meta
            "audio_file":          filename,
            "analysed_at":         datetime.now(ist).isoformat(),
        })

        # Small delay to respect Vertex AI rate limits
        time.sleep(1.5)

    # ── Rank by score ─────────────────────────────────────────────
    results.sort(key=lambda x: x["propensity_score"], reverse=True)
    for rank, r in enumerate(results, 1):
        r["rank"] = rank

    # ── Save output ───────────────────────────────────────────────
    output = {
        "generated_at": datetime.now(ist).isoformat(),
        "total_analysed": len(results),
        "total_errors": len(errors),
        "tier_summary": {
            "High":   sum(1 for r in results if r["tier"] == "High"),
            "Medium": sum(1 for r in results if r["tier"] == "Medium"),
            "Low":    sum(1 for r in results if r["tier"] == "Low"),
        },
        "accounts": results,
        "errors": errors,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    # ── Summary ───────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print(f"  PROPENSITY ANALYSIS COMPLETE")
    print("=" * 55)
    print(f"  Accounts analysed : {len(results)}")
    print(f"  Errors            : {len(errors)}")
    print(f"  High tier         : {output['tier_summary']['High']}")
    print(f"  Medium tier       : {output['tier_summary']['Medium']}")
    print(f"  Low tier          : {output['tier_summary']['Low']}")
    print(f"\n  Output saved to:")
    print(f"  {OUTPUT_FILE}")
    print("=" * 55)

    if errors:
        print(f"\n  Failed recordings:")
        for e in errors:
            print(f"    {e['loan_number']}: {e['error']}")


if __name__ == "__main__":
    main()
