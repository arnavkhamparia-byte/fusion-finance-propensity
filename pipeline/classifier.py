"""
Stage 2 + 3: Deterministic classification and PoP computation.
Takes Stage 1 signal JSON + call_duration_s → complete 18-field output.
No LLM calls.
"""

from datetime import datetime, timezone, timedelta

# ── PoP weights ───────────────────────────────────────────────────────────────

DISPOSITION_WEIGHTS = {
    "Promise To Pay":               1.00,
    "Agree To Pay":                 0.80,  # overridden for low intent
    "Payment Claimed":              0.60,
    "Agree To Senior Manager Call": 0.55,
    "Settlement Not Concluded":     0.45,
    "Call Back Requested":          0.40,
    "Information Conveyed":         0.20,
    "Third Party Connect":          0.15,
    "Financial Hardship":           0.10,
    "Unclear":                      0.15,
    "Dispute":                      0.05,
    "Call Hung Up":                 0.05,
    "Refuse To Pay":                0.00,
    "No Answer":                    0.00,
    "Busy":                         0.00,
    "Failed":                       0.00,
    "Invalid Number":               0.00,
    "Disconnected":                 0.00,
}

COMMITMENT_SCORES = {"strong": 1.00, "moderate": 0.65, "weak": 0.30, "none": 0.00}
ENGAGEMENT_SCORES = {"high": 1.00, "medium": 0.55, "low": 0.15}
SENTIMENT_SCORES  = {"positive": 1.00, "neutral": 0.50, "negative": 0.00}
TONE_SCORES       = {"improved": 1.00, "neutral": 0.50, "worsened": 0.00}


def compute_pop(signals: dict, disposition: str) -> float:
    A = COMMITMENT_SCORES.get(signals.get("commitment_strength", "none"), 0.0)
    B = ENGAGEMENT_SCORES.get(signals.get("engagement_level", "low"), 0.15)
    C = DISPOSITION_WEIGHTS.get(disposition, 0.0)
    if disposition == "Agree To Pay":
        C = 0.80 if signals.get("intent_strength") == "high" else 0.55
    D = SENTIMENT_SCORES.get(signals.get("call_sentiment", "neutral"), 0.50)
    E = TONE_SCORES.get(signals.get("tone_shift", "neutral"), 0.50)

    base = A * 0.35 + B * 0.28 + C * 0.15 + D * 0.12 + E * 0.10

    bonus = 0.0
    if signals.get("promise_made"):       bonus += 0.05
    if signals.get("payment_intent") and not signals.get("specific_date_mentioned"):
        bonus += 0.05
    if signals.get("amount_mentioned"):   bonus += 0.03

    return round(min(base + bonus, 1.00), 2)


# ── PTP sub-disposition helper ────────────────────────────────────────────────

def _ptp_sub(specific_date: str, current_date_str: str) -> str:
    try:
        ptp  = datetime.strptime(specific_date, "%Y-%m-%d").date()
        curr = datetime.strptime(current_date_str[:10], "%Y-%m-%d").date()
        return "Acceptable Date" if (ptp - curr).days <= 15 else "Non Acceptable Date"
    except Exception:
        return "Non Acceptable Date"


def _iso_date(date_str: str) -> str:
    return f"{date_str}T00:00:00+05:30"


# ── Main classifier ───────────────────────────────────────────────────────────

def classify(signals: dict, call_duration_s: float, current_datetime: str) -> dict:
    """
    Deterministically classify a call from Stage 1 signals.
    Returns the complete 18-field output JSON.
    """
    s = signals  # shorthand
    current_date = current_datetime[:10]

    disposition    = None
    sub_disposition = None
    callback_date  = None
    ptp_date       = None

    # ── Stage 2 guard rails — correct known Stage-1 signal errors ──────────────
    s = dict(s)  # work on a copy so we never mutate the caller's signals

    # Guard A: a "Family Member" who AGREED TO A SENIOR MANAGER CALL is really the
    # borrower — a relative cannot commit to discussing the borrower's loan with a
    # senior manager. A family member accepting the SM callback on the borrower's
    # behalf counts on its own — no additional dispute/payment/hardship signal required.
    if (s.get("third_party_answered") and s.get("sm_call_agreed")
            and s.get("third_party_type") == "Family Member Picked Up"):
        s["third_party_answered"] = False
        s["borrower_confirmed"]   = True

    # Guard B: "no answer" on a call long enough to have connected (>=20s) is
    # contradictory — something happened, then the line cut. Treat as a hang-up.
    if s.get("no_answer") and call_duration_s >= 20:
        s["no_answer"]         = False
        s["abrupt_disconnect"] = True

    # Guard C: dead-air sanity (1030861190) — a call under 5 seconds physically
    # cannot contain identity confirmation, hardship discussion, or a payment
    # commitment. Stage 1 sometimes hallucinates a conversation on dead air;
    # strip all conversational signals and treat as an immediate hang-up.
    _telecom = (s.get("no_answer") or s.get("busy_signal") or s.get("call_failed")
                or s.get("invalid_number") or s.get("voicemail_detected"))
    if call_duration_s < 5 and not _telecom:
        s["payment_intent"]   = False
        s["promise_made"]     = False
        s["sm_call_agreed"]   = False
        s["payment_claimed"]  = False
        s["dispute_detected"] = False
        s["hardship_detected"] = False
        s["hardship_soft_signal"] = False
        s["callback_requested"] = False
        s["settlement_negotiated"] = False
        s["abrupt_disconnect"] = True

    # Guard H — promise_made backstop. Stage 1 often emits payment_intent=true with a
    # specific_date AND amount AND strong/moderate commitment, but promise_made=false. The
    # boolean is unreliable in the LLM. If the structural commitment is clearly there, the
    # Promise To Pay gate must fire — patch promise_made.
    if (not s.get("promise_made")
            and s.get("payment_intent")
            and s.get("specific_date_mentioned")
            and s.get("amount_mentioned")
            and s.get("commitment_strength") in ("strong", "moderate")
            and not s.get("third_party_answered")):
        s["promise_made"] = True

    # Guard G: weak polite intent on a callback request is a callback, not an agreement —
    # demote it so the Call Back Requested gate (which also carries callback_datetime) wins.
    if (s.get("callback_requested") and s.get("borrower_confirmed")
            and not s.get("promise_made") and not s.get("specific_date_mentioned")
            and s.get("intent_strength") in (None, "none", "low")
            and not s.get("third_party_answered")):
        s["payment_intent"] = False

    # ── Priority chain ────────────────────────────────────────────────────────

    # 0. Voicemail — agent reached voicemail. Never a real outcome or third party.
    if s.get("voicemail_detected"):
        disposition     = "Unclear"
        sub_disposition = "Voice Mail"

    # 1. Promise To Pay
    elif (s.get("payment_intent") and s.get("specific_date_mentioned")
            and s.get("promise_made") and not s.get("third_party_answered")):
        disposition     = "Promise To Pay"
        sub_disposition = _ptp_sub(s["specific_date_mentioned"], current_date)
        ptp_date        = _iso_date(s["specific_date_mentioned"])

    # 2. Agree To Pay
    elif (s.get("payment_intent") and not s.get("specific_date_mentioned")
          and not s.get("sm_call_agreed") and not s.get("third_party_answered")):
        disposition     = "Agree To Pay"
        sub_disposition = "High Intent" if s.get("intent_strength") == "high" else "Low Intent"

    # 3. Payment Claimed
    elif (s.get("payment_claimed") and not s.get("sm_call_agreed")
          and not s.get("third_party_answered")):
        disposition     = "Payment Claimed"
        sub_disposition = s.get("payment_claimed_type")

    # 4. Agree To Senior Manager Call
    elif s.get("sm_call_agreed") and not s.get("third_party_answered"):
        disposition     = "Agree To Senior Manager Call"
        sub_disposition = s.get("sm_call_reason")
        callback_date   = s.get("callback_datetime")

    # 5. Settlement Not Concluded
    elif (s.get("settlement_negotiated") and not s.get("sm_call_agreed")
          and not s.get("payment_intent") and not s.get("third_party_answered")):
        disposition     = "Settlement Not Concluded"
        sub_disposition = s.get("settlement_outcome")

    # 6. Call Back Requested
    elif (s.get("callback_requested") and s.get("borrower_confirmed")
          and not s.get("third_party_answered")):
        disposition   = "Call Back Requested"
        sub_disposition = None
        callback_date = s.get("callback_datetime")

    # 7. Information Conveyed — borrower present, passively listened, call ended normally
    elif (s.get("borrower_confirmed") and not s.get("third_party_answered")
          and not s.get("payment_intent") and not s.get("dispute_detected")
          and not s.get("hardship_detected") and not s.get("hardship_soft_signal")
          and not s.get("abrupt_disconnect") and not s.get("voicemail_detected")
          and s.get("engagement_level") == "low"):
        disposition     = "Information Conveyed"
        sub_disposition = None

    # 8. Third Party Connect
    elif s.get("third_party_answered"):
        disposition     = "Third Party Connect"
        sub_disposition = s.get("third_party_type") or "Family Member Picked Up"

    # 9. Financial Hardship
    elif s.get("hardship_detected") or s.get("hardship_soft_signal"):
        disposition     = "Financial Hardship"
        sub_disposition = s.get("hardship_type") or "Other"

    # 10. Dispute
    elif s.get("dispute_detected"):
        disposition     = "Dispute"
        sub_disposition = s.get("dispute_type")

    # 11. Call Hung Up
    elif s.get("abrupt_disconnect") or s.get("disconnected_immediately"):
        disposition     = "Call Hung Up"
        sub_disposition = "Less Than 20 Secs" if call_duration_s < 20 else "More Than 20 Secs"

    # 14. No Answer
    elif s.get("no_answer"):
        disposition     = "No Answer"
        sub_disposition = None

    # 15. Busy
    elif s.get("busy_signal"):
        disposition     = "Busy"
        sub_disposition = None

    # 16. Failed
    elif s.get("call_failed"):
        disposition     = "Failed"
        sub_disposition = None

    # 17. Invalid Number
    elif s.get("invalid_number"):
        disposition     = "Invalid Number"
        sub_disposition = None

    # 18. Disconnected — only when call connected then immediately dropped with zero exchange
    elif s.get("disconnected_immediately"):
        disposition     = "Disconnected"
        sub_disposition = None

    # Short-call hang-up — very short call (<20s), low engagement, no meaningful
    # signal → the line cut before anything happened. NOT Unclear.
    elif (call_duration_s < 20 and s.get("engagement_level") == "low"
          and not s.get("voicemail_detected") and not s.get("no_answer")
          and not s.get("busy_signal") and not s.get("call_failed")
          and not s.get("invalid_number")):
        disposition     = "Call Hung Up"
        sub_disposition = "Less Than 20 Secs"

    # 12. Unclear (default fallback)
    else:
        disposition     = "Unclear"
        sub_disposition = "Voice Mail" if s.get("voicemail_detected") else "Other"

    # Guard D: sub-disposition taxonomy validation (1113162457) — Stage 1 can emit
    # an out-of-enum value (e.g. a Dispute sub inside payment_claimed_type). Coerce
    # any invalid sub to the disposition's safe default.
    VALID_SUBS = {
        "Promise To Pay":               {"Acceptable Date", "Non Acceptable Date"},
        "Agree To Pay":                 {"High Intent", "Low Intent"},
        "Payment Claimed":              {"Full Payment", "Partial Payment", "Not Sure Of Amount"},
        "Agree To Senior Manager Call": {"For Settlement Discussion", "For Other Payment Plan",
                                         "For Further Loan Details", "Other"},
        "Settlement Not Concluded":     {"Needs Lower Settlement Amount",
                                         "Did Not Agree For Settlement", "Other"},
        "Third Party Connect":          {"Family Member Picked Up", "Friend Or Neighbour Picked Up",
                                         "Do Not Know Borrower", "Borrower Died"},
        "Financial Hardship":           {"Medical Issue", "Job Loss", "Business Loss",
                                         "Agriculture Loss", "Death In Family", "Other"},
        "Dispute":                      {"Insurance Claim Related", "Amount Disputed",
                                         "Not Availed Loan", "Already Cleared The Loan", "Fraud Claim"},
        "Call Hung Up":                 {"Less Than 20 Secs", "More Than 20 Secs"},
        "Unclear":                      {"Voice Mail", "Other"},
        "Refuse To Pay":                {"Denied Debt", "Unwilling To Pay", "Abusive"},
    }
    SUB_DEFAULTS = {
        "Payment Claimed": "Not Sure Of Amount", "Agree To Senior Manager Call": "Other",
        "Settlement Not Concluded": "Other", "Third Party Connect": "Family Member Picked Up",
        "Financial Hardship": "Other", "Unclear": "Other", "Agree To Pay": "Low Intent",
        "Dispute": "Amount Disputed", "Refuse To Pay": "Unwilling To Pay",
    }
    if disposition in VALID_SUBS:
        if sub_disposition not in VALID_SUBS[disposition]:
            sub_disposition = SUB_DEFAULTS.get(disposition)
    else:
        sub_disposition = None  # null-sub dispositions (Info Conveyed, Call Back, telecom states)

    pop = compute_pop(s, disposition)

    return {
        "disposition":              disposition,
        "sub_disposition":          sub_disposition,
        "callback_date":            callback_date,
        "ptp_date":                 ptp_date,
        "amount":                   s.get("amount_mentioned"),
        "call_sentiment":           s.get("call_sentiment", "neutral"),
        "probability_of_payment":   pop,
        "compliance_flag":          "Yes" if s.get("compliance_violation") else "No",
        "type_of_compliance_flag":  s.get("compliance_type") if s.get("compliance_violation") else None,
        "network_quality":          s.get("network_quality", "Good"),
        "conversation_quality":     s.get("conversation_quality", "Good"),
        "type_of_customer":         s.get("type_of_customer", "Neutral"),
        "customer_attributes":      s.get("customer_attributes", []),
        "immediate_callback_needed": "Yes" if s.get("immediate_callback_needed") else "No",
        "commitment_strength":      s.get("commitment_strength", "none"),
        "engagement_level":         s.get("engagement_level", "low"),
        "promise_made":             "Yes" if s.get("promise_made") else "No",
        "summary":                  s.get("summary", ""),
    }
