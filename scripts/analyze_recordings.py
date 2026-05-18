"""
Step 2: Analyze recordings with Gemini and compute propensity scores.

For each account in account_data.json:
  1. Fetches audio bytes from S3 via presigned URL (no local MP3 files needed)
  2. Sends audio to Gemini 2.5 Flash (Vertex AI) with extended propensity prompt
  3. Parses 13-field JSON response
  4. Combines LLM output with DB account data
  5. Calculates propensity score (0–100) using weighted formula
  6. Ranks all accounts

Supports concurrent processing via --workers N (default 1, recommended 4).

Output: data/propensity_results.json
"""

import os
import re
import json
import time
import logging
import argparse
import threading
import requests
import boto3
import concurrent.futures
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
# Gemini client
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
# AWS S3 — presigned URL helper
# ─────────────────────────────────────────────────────────────────

class AwsConnection:
    def __init__(self):
        self.session = boto3.session.Session(
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            region_name=os.environ["AWS_REGION_NAME"],
        )

    def generate_pre_signed_url(self, key, bucket="ai-call-bucket", expiration=3600):
        s3_client = self.session.client("s3")
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expiration,
        )


def download_audio_from_s3(aws: AwsConnection, call_recording_url: str) -> tuple:
    """
    Generate a presigned URL for the S3 key and stream the audio bytes into memory.
    Returns (audio_bytes, mime_type). No files are written to disk.
    """
    presigned_url = aws.generate_pre_signed_url(key=call_recording_url)
    response = requests.get(presigned_url, timeout=60)
    response.raise_for_status()

    key_lower = call_recording_url.lower()
    if key_lower.endswith(".mp3"):
        mime_type = "audio/mpeg"
    elif key_lower.endswith(".wav"):
        mime_type = "audio/wav"
    elif key_lower.endswith(".ogg"):
        mime_type = "audio/ogg"
    else:
        mime_type = "audio/mpeg"

    return response.content, mime_type


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

STAGE1_DISPOSITIONS = {
    "not-connected", "no-answer", "Busy", "Failed",
    "Wrong Number", "Call Hang Up", "Connected",
}


def parse_dpd_score(dpd_bucket: str) -> float:
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
    if not duration_seconds:
        return 0.2
    d = int(duration_seconds)
    if d < 20:   return 0.10
    if d < 45:   return 0.25
    if d < 90:   return 0.55
    if d < 180:  return 0.85
    return 1.00


def contact_rate_score(call_history: list) -> float:
    if not call_history:
        return 0.3
    non_connected = {"no-answer", "Busy", "not-connected", "Failed", "Wrong Number"}
    recent = call_history[:10]
    connected = sum(1 for c in recent if c.get("disposition") not in non_connected)
    return connected / len(recent)


def disposition_trajectory_score(call_history: list) -> float:
    positive = {"Agree To Senior Manager Call", "Requested Settlement", "Call Back Requested"}
    negative = {"Refuse To Pay", "Dispute"}

    def group_score(calls):
        if not calls:
            return 0.4
        scores = []
        for c in calls:
            d = c.get("disposition", "")
            if d in positive:
                scores.append(1.0)
            elif d in negative:
                scores.append(0.0)
            else:
                scores.append(0.4)
        return sum(scores) / len(scores)

    recent_score = group_score(call_history[:3])
    prior_score  = group_score(call_history[3:6])

    if call_history[3:6]:
        trajectory = recent_score - prior_score
        adjusted   = recent_score + (trajectory * 0.3)
        return max(0.0, min(1.0, adjusted))

    return recent_score


def ptp_reliability_score(call_history: list) -> float:
    promise_like  = {"Agree To Senior Manager Call", "Requested Settlement", "Call Back Requested"}
    broken_signal = {"no-answer", "Busy", "not-connected", "Failed", "Refuse To Pay",
                     "Wrong Number", "Unclear"}

    total_promises  = 0
    broken_promises = 0

    for i in range(1, len(call_history)):
        disp = call_history[i].get("disposition", "")
        if disp in promise_like:
            total_promises += 1
            following_disp = call_history[i - 1].get("disposition", "")
            if following_disp in broken_signal:
                broken_promises += 1

    if total_promises == 0:
        return 0.5
    return 1.0 - (broken_promises / total_promises)


def history_trend_score(call_history: list) -> tuple:
    if not call_history:
        return 0.3, {"contact_rate": 0.3, "disposition_trajectory": 0.3,
                     "ptp_reliability": 0.5, "consecutive_neg_streak": 0, "penalty": 0.0}

    cr   = contact_rate_score(call_history)
    dt   = disposition_trajectory_score(call_history)
    pr   = ptp_reliability_score(call_history)
    base = (cr + dt + pr) / 3

    penalty = 0.0

    STAGE1 = {"no-answer", "Busy", "not-connected", "Failed",
              "Wrong Number", "Call Hang Up", "Refuse To Pay"}
    streak = 0
    for c in call_history:
        if c.get("disposition") in STAGE1:
            streak += 1
        else:
            break
    if streak >= 4:
        penalty += 0.15

    positive = {"Agree To Senior Manager Call", "Requested Settlement", "Call Back Requested"}
    days_since_positive = None
    for c in call_history:
        if c.get("disposition") in positive:
            ts = c.get("processed_at")
            if ts:
                try:
                    dt_obj = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    if dt_obj.tzinfo is None:
                        dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                    days_since_positive = (datetime.now(timezone.utc) - dt_obj).days
                except Exception:
                    pass
            break
    if days_since_positive is not None:
        if days_since_positive > 60:
            penalty += 0.10
        elif days_since_positive > 30:
            penalty += 0.05

    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    recent_30d = []
    for c in call_history:
        ts = c.get("processed_at")
        if ts:
            try:
                dt_obj = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                if dt_obj >= cutoff:
                    recent_30d.append(c)
            except Exception:
                pass
    if len(recent_30d) >= 20:
        no_answer = sum(1 for c in recent_30d
                        if c.get("disposition") in {"no-answer", "Busy", "not-connected"})
        if no_answer / len(recent_30d) > 0.6:
            penalty += 0.10

    composite = max(0.0, min(1.0, base - penalty))
    sub_scores = {
        "contact_rate":           round(cr, 3),
        "disposition_trajectory": round(dt, 3),
        "ptp_reliability":        round(pr, 3),
        "consecutive_neg_streak": streak,
        "penalty":                round(penalty, 3),
    }
    return composite, sub_scores


def calculate_propensity_score(llm: dict, account: dict, call_history: list, payments: list) -> dict:
    """
    Weighted formula combining LLM propensity signals + DB context.

    Weights (v3 — richer history signals):
      Commitment strength   28%
      Engagement level      22%
      History trend         15%
      Disposition           10%
      Sentiment             10%
      DPD bucket            10%
      Call duration          5%
      Bonus                 +13 max
                           ────
                           100% base + bonus → capped at 100
    """
    w_disp        = 0.10
    w_commitment  = 0.28
    w_engagement  = 0.22
    w_sentiment   = 0.10
    w_duration    = 0.05
    w_history     = 0.15
    w_dpd         = 0.10

    disp       = llm.get("disposition", "")
    commitment = llm.get("commitment_strength", "none")
    engagement = llm.get("engagement_level", "low")

    s_disp      = DISPOSITION_SCORES.get(disp, 0.2)
    s_commit    = COMMITMENT_SCORES.get(commitment, 0.0)
    s_engage    = ENGAGEMENT_SCORES.get(engagement, 0.15)
    s_sentiment = SENTIMENT_SCORES.get(llm.get("sentiment", "neutral"), 0.5)
    s_duration  = call_duration_score(account.get("call_duration"))
    s_history, history_sub = history_trend_score(call_history)
    s_dpd       = parse_dpd_score(account.get("dpd_bucket", ""))

    base = (
        s_disp      * w_disp       +
        s_commit    * w_commitment +
        s_engage    * w_engagement +
        s_sentiment * w_sentiment  +
        s_duration  * w_duration   +
        s_history   * w_history    +
        s_dpd       * w_dpd
    ) * 100

    bonus = 0
    if llm.get("promise_made"):                   bonus += 5
    if llm.get("customer_initiated_resolution"):  bonus += 5
    if llm.get("specific_amount_discussed"):      bonus += 3
    bonus += TONE_SHIFT_BONUS.get(llm.get("tone_shift", "neutral"), 0)

    passive_yes = (
        disp == "Agree To Senior Manager Call"
        and commitment in ("weak", "none")
        and engagement == "low"
    )

    engaged_hardship = (
        disp == "Financial Hardship"
        and engagement == "high"
        and llm.get("customer_initiated_resolution", False)
    )

    if engaged_hardship:
        bonus += 10

    raw_score = min(round(base + bonus, 1), 100)
    if passive_yes:
        raw_score = min(raw_score, 60)

    if raw_score >= 65:
        tier = "High"
    elif raw_score >= 40:
        tier = "Medium"
    else:
        tier = "Low"

    reasons = []
    if passive_yes:
        reasons.append("Passive agreement — low commitment and engagement despite yes")
    if engaged_hardship:
        reasons.append("Financial hardship but proactively seeking resolution")
    if llm.get("promise_made"):
        reasons.append(f"Promise to pay by {llm.get('promise_date') or 'specific date'}")
    if llm.get("customer_initiated_resolution") and not engaged_hardship:
        reasons.append("Customer asked about resolution options")
    if commitment in ("strong", "moderate"):
        reasons.append(f"{commitment.capitalize()} commitment expressed")
    if llm.get("tone_shift") == "improved":
        reasons.append("Tone improved during call")
    if llm.get("specific_amount_discussed"):
        reasons.append("Specific payment amount discussed")
    if history_sub.get("consecutive_neg_streak", 0) >= 4:
        reasons.append(f"Warning: {history_sub['consecutive_neg_streak']} consecutive failed call attempts")
    if history_sub.get("ptp_reliability", 1.0) < 0.4:
        reasons.append("Low PTP reliability — past promises frequently not kept")
    if not reasons:
        reasons.append(disp or "No strong positive signal")

    return {
        "propensity_score": raw_score,
        "tier":             tier,
        "key_reasons":      reasons,
        "score_breakdown": {
            "disposition_score":      round(s_disp    * w_disp       * 100, 1),
            "commitment_score":       round(s_commit  * w_commitment * 100, 1),
            "engagement_score":       round(s_engage  * w_engagement * 100, 1),
            "sentiment_score":        round(s_sentiment * w_sentiment * 100, 1),
            "duration_score":         round(s_duration * w_duration  * 100, 1),
            "history_score":          round(s_history  * w_history   * 100, 1),
            "dpd_score":              round(s_dpd      * w_dpd       * 100, 1),
            "bonus_points":           bonus,
            "passive_yes_capped":     passive_yes,
            "engaged_hardship_boost": engaged_hardship,
            "history_sub_scores":     history_sub,
        },
    }


# ─────────────────────────────────────────────────────────────────
# Prompt caching
# ─────────────────────────────────────────────────────────────────

def create_prompt_cache(prompt_text: str) -> str | None:
    try:
        cache = client.caches.create(
            model=MODEL_NAME,
            config=types.CreateCachedContentConfig(
                contents=[prompt_text],
                ttl="7200s",
                display_name="propensity-prompt-cache",
            ),
        )
        log.info(f"Explicit prompt cache created → {cache.name}  (TTL: 2h, "
                 f"saves 75% on {len(prompt_text):,} char prompt per call)")
        return cache.name
    except Exception as e:
        log.warning(f"Explicit caching unavailable ({e}). Falling back to implicit caching.")
        return None


def delete_prompt_cache(cache_name: str) -> None:
    try:
        client.caches.delete(name=cache_name)
        log.info(f"Prompt cache deleted → {cache_name}")
    except Exception as e:
        log.warning(f"Could not delete cache {cache_name}: {e}")


# ─────────────────────────────────────────────────────────────────
# Gemini audio analysis
# ─────────────────────────────────────────────────────────────────

def analyze_audio(audio_bytes: bytes, mime_type: str, current_dt: str,
                  cache_name: str | None = None) -> tuple[dict, dict]:
    """
    Send audio bytes to Gemini and return (parsed JSON, token_usage dict).
    120-second timeout — raises TimeoutError if Gemini hangs.
    """
    audio_part  = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    prompt_text = PROPENSITY_PROMPT.replace("{current_datetime}", current_dt)

    if cache_name:
        def _call():
            return client.models.generate_content(
                model=MODEL_NAME,
                contents=[audio_part],
                config=types.GenerateContentConfig(
                    cached_content=cache_name,
                    response_mime_type="application/json",
                ),
            )
    else:
        def _call():
            return client.models.generate_content(
                model=MODEL_NAME,
                contents=[prompt_text, audio_part],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_call)
        try:
            response = future.result(timeout=120)
        except concurrent.futures.TimeoutError:
            raise TimeoutError("Gemini API call timed out after 120s")

    usage = response.usage_metadata
    cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0
    token_usage = {
        "input_tokens":  usage.prompt_token_count     if usage else None,
        "cached_tokens": cached_tokens,
        "output_tokens": usage.candidates_token_count if usage else None,
        "total_tokens":  usage.total_token_count      if usage else None,
    }

    raw = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(raw), token_usage


# ─────────────────────────────────────────────────────────────────
# Single-account processor  (called by each worker thread)
# ─────────────────────────────────────────────────────────────────

def process_single_account(loan_number, db_entry, aws, current_dt, cache_ref, ist):
    """
    Process one account end-to-end: pre-flight → S3 download → Gemini → score.

    cache_ref is a mutable list [cache_name_or_None] so that any worker can
    clear it on cache expiry and all subsequent workers switch to implicit caching.

    Returns (result_dict, token_usage_dict, error_str_or_None).
    """
    acct                     = db_entry["account"]
    call_history             = db_entry["call_history"]
    payments                 = db_entry["payments"]
    recording_key            = db_entry.get("call_recording_url")
    qualifying_disposition   = db_entry.get("qualifying_disposition")
    qualifying_call_duration = db_entry.get("qualifying_call_duration")

    if not recording_key:
        return None, None, "No call_recording_url in DB"

    # ── Layer 1: Pre-flight duration check ───────────────────────
    call_duration           = qualifying_call_duration
    recording_skipped_short = False
    recording_mismatch      = False

    if call_duration is not None and int(call_duration) < 20:
        log.info(f"  [{loan_number}] SKIPPED: {call_duration}s < 20s — "
                 f"using qualifying disposition '{qualifying_disposition}'")
        llm_output = {
            "disposition":                   qualifying_disposition,
            "sentiment":                     "neutral",
            "commitment_strength":           "none",
            "engagement_level":              "low",
            "promise_made":                  False,
            "promise_date":                  None,
            "barrier_type":                  None,
            "customer_initiated_resolution": False,
            "tone_shift":                    "neutral",
            "specific_amount_discussed":     False,
            "summary": (f"Recording skipped — call duration {call_duration}s is under the "
                        f"20-second threshold. Score based on qualifying disposition "
                        f"'{qualifying_disposition}'."),
        }
        token_usage = {"input_tokens": 0, "cached_tokens": 0,
                       "output_tokens": 0, "total_tokens": 0}
        recording_skipped_short = True

    else:
        # ── Download audio from S3 ────────────────────────────────
        try:
            audio_bytes, mime_type = download_audio_from_s3(aws, recording_key)
            log.info(f"  [{loan_number}] Downloaded {len(audio_bytes):,} bytes ({mime_type})")
        except Exception as e:
            return None, None, f"S3 download failed: {e}"

        # ── Gemini analysis ───────────────────────────────────────
        try:
            llm_output, token_usage = analyze_audio(
                audio_bytes, mime_type, current_dt, cache_ref[0])
        except Exception as e:
            # Cache expiry: clear shared cache_ref and retry with implicit caching
            if "expired" in str(e).lower() and cache_ref[0]:
                log.warning(f"  [{loan_number}] Cache expired — switching to implicit caching, retrying")
                cache_ref[0] = None
                try:
                    llm_output, token_usage = analyze_audio(
                        audio_bytes, mime_type, current_dt, None)
                except Exception as retry_e:
                    return None, None, str(retry_e)
            else:
                return None, None, str(e)

        # ── Layer 2: Post-analysis mismatch detection ─────────────
        gemini_disposition = llm_output.get("disposition")
        recording_mismatch = (
            gemini_disposition in STAGE1_DISPOSITIONS
            and qualifying_disposition not in STAGE1_DISPOSITIONS
            and qualifying_disposition is not None
        )
        if recording_mismatch:
            log.warning(f"  [{loan_number}] MISMATCH: qualifying={qualifying_disposition}, "
                        f"Gemini={gemini_disposition} — overriding")
            llm_output["disposition"] = qualifying_disposition

    # ── Score ─────────────────────────────────────────────────────
    acct_copy = dict(acct)   # avoid mutating shared dict across threads
    if call_history:
        acct_copy["call_duration"] = call_history[0].get("call_duration")

    scoring = calculate_propensity_score(llm_output, acct_copy, call_history, payments)

    result = {
        "loan_number":           loan_number,
        "token_usage":           token_usage,
        "name":                  acct.get("name"),
        "city":                  acct.get("city"),
        "loan_amount":           acct.get("loan_amount"),
        "emi_amount":            acct.get("emi_amount"),
        "total_amount_pending":  acct.get("total_amount_pending"),
        "dpd_bucket":            acct.get("dpd_bucket"),
        "assigned_to_id":        acct.get("assigned_to_id"),
        # LLM fields
        "disposition":           llm_output.get("disposition"),
        "sentiment":             llm_output.get("sentiment"),
        "summary":               llm_output.get("summary"),
        "commitment_strength":   llm_output.get("commitment_strength"),
        "promise_made":          llm_output.get("promise_made"),
        "promise_date":          llm_output.get("promise_date"),
        "barrier_type":          llm_output.get("barrier_type"),
        "engagement_level":      llm_output.get("engagement_level"),
        "customer_initiated_resolution": llm_output.get("customer_initiated_resolution"),
        "tone_shift":            llm_output.get("tone_shift"),
        "specific_amount_discussed": llm_output.get("specific_amount_discussed"),
        # Scoring
        "propensity_score":      scoring["propensity_score"],
        "tier":                  scoring["tier"],
        "key_reasons":           scoring["key_reasons"],
        "score_breakdown":       scoring["score_breakdown"],
        # History
        "total_calls":           len(call_history),
        "previous_dispositions": [c.get("disposition") for c in call_history[:5]],
        "total_payments":        len(payments),
        "last_payment_date":     payments[0]["payment_date"] if payments else None,
        # Meta
        "s3_recording_key":                  recording_key,
        "recording_mismatch":                recording_mismatch,
        "recording_skipped_short_duration":  recording_skipped_short,
        "analysed_at":                       datetime.now(ist).isoformat(),
    }

    return result, token_usage, None


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main():
    # ── CLI arguments ─────────────────────────────────────────────
    parser = argparse.ArgumentParser(description="Analyse recordings and compute propensity scores.")
    parser.add_argument("--batch", type=int, default=None,
                        help="Batch number (1-based). --batch 1 processes first 1000 accounts.")
    parser.add_argument("--batch-size", type=int, default=1000,
                        help="Accounts per batch (default: 1000).")
    parser.add_argument("--workers", type=int, default=1,
                        help="Concurrent worker threads (default: 1, recommended: 4).")
    args = parser.parse_args()

    # ── Load DB data ──────────────────────────────────────────────
    if not os.path.exists(ACCOUNT_DATA):
        log.error(f"account_data.json not found at {ACCOUNT_DATA}")
        log.error("Run fetch_account_data.py first.")
        return

    with open(ACCOUNT_DATA) as f:
        account_data = json.load(f)

    log.info(f"Loaded DB data for {len(account_data)} accounts total")

    # ── AWS S3 client ─────────────────────────────────────────────
    aws = AwsConnection()
    log.info("AWS S3 client initialised")

    # ── IST timestamp ─────────────────────────────────────────────
    ist = timezone(timedelta(hours=5, minutes=30))
    current_dt = datetime.now(ist).strftime("%A, %B %d, %Y %I:%M %p")

    all_loan_numbers = sorted(account_data.keys())

    # ── Batch slicing ─────────────────────────────────────────────
    if args.batch is not None:
        start = (args.batch - 1) * args.batch_size
        end   = start + args.batch_size
        loan_numbers = all_loan_numbers[start:end]
        log.info(f"Batch {args.batch}: accounts {start + 1}–{min(end, len(all_loan_numbers))} "
                 f"of {len(all_loan_numbers)} total ({len(loan_numbers)} in this batch)")
    else:
        loan_numbers = all_loan_numbers
        log.info(f"Processing all {len(loan_numbers)} accounts")

    log.info(f"Workers: {args.workers}\n")

    # ── Resume: skip already-processed accounts ───────────────────
    already_done               = {}
    existing_errors_on_resume  = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            existing_output = json.load(f)
        already_done               = {a["loan_number"]: a for a in existing_output.get("accounts", [])}
        existing_errors_on_resume  = existing_output.get("errors", [])
        skippable = [ln for ln in loan_numbers if ln in already_done]
        if skippable:
            log.info(f"Resuming: {len(skippable)} accounts already processed — skipping them")
            loan_numbers = [ln for ln in loan_numbers if ln not in already_done]

    if not loan_numbers:
        log.info("All accounts in this batch are already processed.")
        return

    # ── Create prompt cache once for the entire batch ─────────────
    prompt_text = PROPENSITY_PROMPT.replace("{current_datetime}", current_dt)
    cache_name  = create_prompt_cache(prompt_text)

    # cache_ref is a mutable list so worker threads can clear it on expiry
    cache_ref = [cache_name]

    results  = []
    errors   = []
    save_lock = threading.Lock()

    # Token accumulators (updated inside save_lock — no separate lock needed)
    total_input_tokens  = 0
    total_cached_tokens = 0
    total_output_tokens = 0
    total_tokens_all    = 0
    completed_count     = [0]   # mutable so closure can increment

    def _incremental_save():
        """Merge new results with already_done and write to disk. Call inside save_lock."""
        all_so_far = list(already_done.values()) + results
        all_so_far.sort(key=lambda x: x["propensity_score"], reverse=True)
        for _rank, _r in enumerate(all_so_far, 1):
            _r["rank"] = _rank
        _partial = {
            "generated_at":   datetime.now(ist).isoformat(),
            "total_analysed": len(all_so_far),
            "total_errors":   len(existing_errors_on_resume) + len(errors),
            "tier_summary": {
                "High":   sum(1 for r in all_so_far if r["tier"] == "High"),
                "Medium": sum(1 for r in all_so_far if r["tier"] == "Medium"),
                "Low":    sum(1 for r in all_so_far if r["tier"] == "Low"),
            },
            "accounts": all_so_far,
            "errors":   existing_errors_on_resume + errors,
        }
        os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(_partial, f, indent=2, default=str)

    # ── Concurrent processing ─────────────────────────────────────
    log.info(f"Starting {args.workers}-worker concurrent processing of "
             f"{len(loan_numbers)} accounts...\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_single_account,
                ln, account_data[ln], aws, current_dt, cache_ref, ist
            ): ln
            for ln in loan_numbers
        }

        for future in concurrent.futures.as_completed(futures):
            loan_number = futures[future]

            try:
                result, token_usage, error = future.result()
            except Exception as exc:
                result, token_usage, error = None, None, str(exc)

            with save_lock:
                completed_count[0] += 1
                count = completed_count[0]

                if error or result is None:
                    log.error(f"[{count}/{len(loan_numbers)}] FAILED {loan_number}: {error}")
                    errors.append({"loan_number": loan_number, "error": error})
                else:
                    log.info(f"[{count}/{len(loan_numbers)}] {loan_number} — "
                             f"Score: {result['propensity_score']} ({result['tier']}) | "
                             f"Disposition: {result.get('disposition')} | "
                             f"Engagement: {result.get('engagement_level')} | "
                             f"Commitment: {result.get('commitment_strength')}")
                    results.append(result)

                    # Accumulate token counts
                    if token_usage and token_usage.get("total_tokens"):
                        total_input_tokens  += token_usage.get("input_tokens")  or 0
                        total_cached_tokens += token_usage.get("cached_tokens") or 0
                        total_output_tokens += token_usage.get("output_tokens") or 0
                        total_tokens_all    += token_usage.get("total_tokens")  or 0

                _incremental_save()

    # ── Clean up prompt cache ─────────────────────────────────────
    if cache_ref[0]:
        delete_prompt_cache(cache_ref[0])

    # ── Token summary ─────────────────────────────────────────────
    avg_tokens     = round(total_tokens_all / max(len(loan_numbers), 1))
    PRICE_INPUT    = 0.075
    PRICE_CACHED   = 0.01875
    PRICE_OUTPUT   = 0.30
    uncached_input = total_input_tokens - total_cached_tokens
    cost_usd       = (uncached_input      / 1_000_000 * PRICE_INPUT  +
                      total_cached_tokens / 1_000_000 * PRICE_CACHED +
                      total_output_tokens / 1_000_000 * PRICE_OUTPUT)
    cost_no_cache  = (total_input_tokens  / 1_000_000 * PRICE_INPUT  +
                      total_output_tokens / 1_000_000 * PRICE_OUTPUT)

    token_summary = {
        "total_input_tokens":    total_input_tokens,
        "total_cached_tokens":   total_cached_tokens,
        "total_output_tokens":   total_output_tokens,
        "total_tokens":          total_tokens_all,
        "avg_tokens_per_call":   avg_tokens,
        "cache_mode":            "explicit" if cache_name else "implicit",
        "estimated_cost_usd":    round(cost_usd, 6),
        "estimated_cost_inr":    round(cost_usd * 83, 4),
        "saved_vs_no_cache_usd": round(cost_no_cache - cost_usd, 6),
    }

    # ── Final save with full token summary ────────────────────────
    all_results = list(already_done.values()) + results
    all_results.sort(key=lambda x: x["propensity_score"], reverse=True)
    for rank, r in enumerate(all_results, 1):
        r["rank"] = rank

    output = {
        "generated_at":   datetime.now(ist).isoformat(),
        "total_analysed": len(all_results),
        "total_errors":   len(existing_errors_on_resume) + len(errors),
        "tier_summary": {
            "High":   sum(1 for r in all_results if r["tier"] == "High"),
            "Medium": sum(1 for r in all_results if r["tier"] == "Medium"),
            "Low":    sum(1 for r in all_results if r["tier"] == "Low"),
        },
        "token_summary": token_summary,
        "accounts": all_results,
        "errors":   existing_errors_on_resume + errors,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    # ── Summary ───────────────────────────────────────────────────
    total_batches = -(-len(all_loan_numbers) // args.batch_size) if args.batch else 1
    print("\n" + "=" * 60)
    print(f"  PROPENSITY ANALYSIS COMPLETE")
    print("=" * 60)
    if args.batch:
        print(f"  Batch             : {args.batch} of {total_batches}")
    print(f"  Workers           : {args.workers}")
    print(f"  Accounts in file  : {len(all_results)} (cumulative)")
    print(f"  This batch        : {len(loan_numbers)} processed")
    print(f"  Errors            : {len(errors)}")
    print(f"  High tier         : {output['tier_summary']['High']}")
    print(f"  Medium tier       : {output['tier_summary']['Medium']}")
    print(f"  Low tier          : {output['tier_summary']['Low']}")
    print(f"\n  TOKEN CONSUMPTION  [{token_summary['cache_mode']} caching]")
    print(f"  Total input tokens  : {total_input_tokens:,}")
    print(f"  └─ Cached tokens    : {total_cached_tokens:,}  (billed at 25%)")
    print(f"  Total output tokens : {total_output_tokens:,}")
    print(f"  Total tokens        : {total_tokens_all:,}")
    print(f"  Avg tokens/call     : {avg_tokens:,}")
    print(f"  Estimated cost      : ${cost_usd:.4f}  (~₹{cost_usd * 83:.2f})")
    print(f"  Saved vs no cache   : ${cost_no_cache - cost_usd:.4f}  "
          f"(~₹{(cost_no_cache - cost_usd) * 83:.2f})")
    print(f"\n  Output saved to:")
    print(f"  {OUTPUT_FILE}")
    print("=" * 60)

    if errors:
        print(f"\n  Failed accounts ({len(errors)}):")
        for e in errors:
            print(f"    {e['loan_number']}: {e['error']}")


if __name__ == "__main__":
    main()
