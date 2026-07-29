"""
Stage 2: Deterministic classifier for fusion_mfi_emi flow (Randheer).
Taxonomy: 12 dispositions. HAS Agree To Senior Manager Call. NO Settlement Not Concluded.
"""

from datetime import datetime

# ── PoP weights ───────────────────────────────────────────────────────────────

DISPOSITION_WEIGHTS = {
    "Promise To Pay":               1.00,
    "Agree To Pay":                 0.80,  # overridden for low intent
    "Payment Claimed":              0.60,
    "Agree To Senior Manager Call": 0.40,
    "Call Back Requested":          0.40,
    "Information Conveyed":         0.20,
    "Third Party Connect":          0.15,
    "Financial Hardship":           0.10,
    "Unclear":                      0.15,
    "Dispute":                      0.05,
    "Call Hang Up":                 0.05,
    "Refuse To Pay":                0.00,
    "No Answer":                    0.00,
    "Busy":                         0.00,
    "Failed":                       0.00,
    "Invalid Number":               0.00,
    "Disconnected":                 0.00,
    "Switched Off":                 0.00,
    "Not Reachable":                0.00,
    "Incoming Call Barred":         0.00,
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


def _ptp_sub(specific_date: str, current_date_str: str, payment_mode: str = None) -> str:
    mode = payment_mode if payment_mode in ("Online", "Cash Pick Up") else "Online"
    try:
        ptp  = datetime.strptime(specific_date, "%Y-%m-%d").date()
        curr = datetime.strptime(current_date_str[:10], "%Y-%m-%d").date()
        date_label = "Acceptable date" if (ptp - curr).days <= 15 else "Non Acceptable date"
    except Exception:
        date_label = "Non Acceptable date"
    return f"{date_label} - {mode}"


def _iso_date(date_str: str) -> str:
    return f"{date_str}T00:00:00+05:30"


def normalize_signals(signals: dict, call_duration_s: float) -> dict:
    """
    Apply the Stage 2 guard-rail normalizations to raw Stage 1 signals and return
    a patched copy. Used by both classify() (for the priority chain) and the
    caller's compute_call_rating() (so the rating gate sees post-guard signals,
    not the raw Stage 1 dict).
    """
    s = dict(signals)

    # ── Guard rails ───────────────────────────────────────────────────────────

    # Guard A: Family Member + sm_call_agreed → really the borrower. A family member
    # accepting the SM callback on the borrower's behalf counts on its own — no
    # additional dispute/payment/hardship signal is required.
    if (s.get("third_party_answered") and s.get("sm_call_agreed")
            and s.get("third_party_type") == "Family Member Picked Up"):
        s["third_party_answered"] = False
        s["borrower_confirmed"]   = True

    # Guard B: no_answer on >=20s call → treat as hang-up.
    if s.get("no_answer") and call_duration_s >= 20:
        s["no_answer"]         = False
        s["abrupt_disconnect"] = True

    # Guard C: dead-air sanity — <5s call cannot have meaningful conversation.
    _telecom = (s.get("no_answer") or s.get("busy_signal") or s.get("call_failed")
                or s.get("invalid_number") or s.get("voicemail_detected")
                or s.get("switched_off") or s.get("not_reachable") or s.get("incoming_call_barred"))
    if call_duration_s < 5 and not _telecom:
        s["payment_intent"]        = False
        s["promise_made"]          = False
        s["sm_call_agreed"]        = False
        s["payment_claimed"]       = False
        s["dispute_detected"]      = False
        s["hardship_detected"]     = False
        s["hardship_soft_signal"]  = False
        s["refusal_detected"]      = False
        s["callback_requested"]    = False
        s["settlement_negotiated"] = False
        s["abrupt_disconnect"]     = True

    # Guard E: weak polite intent on a callback request is a callback, not an agreement —
    # demote it so the Call Back Requested gate (which also carries callback_datetime) wins.
    if (s.get("callback_requested") and s.get("borrower_confirmed")
            and not s.get("promise_made") and not s.get("specific_date_mentioned")
            and s.get("intent_strength") in (None, "none", "low")
            and not s.get("third_party_answered")):
        s["payment_intent"] = False

    # Guard F — promise_made backstop (ported from the explore classifier's Guard H).
    # Stage 1 often emits payment_intent=true with a specific_date AND amount AND
    # strong/moderate commitment, but promise_made=false. The boolean is unreliable in
    # the LLM. If the structural commitment is clearly there, the Promise To Pay gate
    # must fire — patch promise_made.
    if (not s.get("promise_made")
            and s.get("payment_intent")
            and s.get("specific_date_mentioned")
            and s.get("amount_mentioned")
            and s.get("commitment_strength") in ("strong", "moderate")
            and not s.get("third_party_answered")):
        s["promise_made"] = True

    # Guard G — uncorroborated payment intent on an abruptly-dropped short call is not an
    # agreement. A lone amount fragment ("140") with non-high intent strength, no promise,
    # no date, on a sub-40s call that either dropped abruptly or had low engagement, is the
    # agent's interpretation, not the customer's commitment. Exclusion check (!= "high") so
    # invented out-of-vocabulary strengths can never slip past. Demote payment_intent so the
    # chain falls through to Call Hang Up instead of Agree To Pay.
    if (s.get("payment_intent")
            and s.get("intent_strength") != "high"
            and not s.get("promise_made")
            and not s.get("specific_date_mentioned")
            and call_duration_s < 40
            and (s.get("abrupt_disconnect") or s.get("engagement_level") == "low")):
        s["payment_intent"]  = False
        s["intent_strength"] = "none"

    # Guard H — a dispute requires the borrower. A loan denial from an unconfirmed
    # answerer ("never took a loan") is the wrong-number pattern (Third Party /
    # Do Not Know Borrower), not a Dispute. Demote so the Dispute gate cannot fire
    # on third-party or unidentified denials.
    if s.get("dispute_detected") and not s.get("borrower_confirmed"):
        s["dispute_detected"] = False
        s["dispute_type"]     = None

    # Guard: customer never spoke — the call connected but only the agent is audible.
    # Stage 1 sometimes derives hardship / PTP signals from the agent's recap of the
    # previous call; an agent recap is context, not evidence. Strip every speech-derived
    # outcome flag so nothing above Call Hang Up can fire. Telecom states keep their own
    # flags — this guard never applies to them.
    if s.get("customer_never_spoke") and not _telecom:
        s["borrower_confirmed"]    = False
        s["third_party_answered"]  = False
        s["payment_intent"]        = False
        s["promise_made"]          = False
        s["sm_call_agreed"]        = False
        s["payment_claimed"]       = False
        s["dispute_detected"]      = False
        s["hardship_detected"]     = False
        s["hardship_soft_signal"]  = False
        s["refusal_detected"]      = False
        s["callback_requested"]    = False
        s["settlement_negotiated"] = False
        s["specific_date_mentioned"] = None
        s["amount_mentioned"]        = None
        s["callback_datetime"]       = None
        s["intent_strength"]         = "none"
        s["commitment_strength"]     = "none"
        s["engagement_level"]        = "low"
        s["call_sentiment"]          = "neutral"
        s["tone_shift"]              = "neutral"
    else:
        s["customer_never_spoke"] = False

    return s


def classify(signals: dict, call_duration_s: float, current_datetime: str) -> dict:
    """
    Deterministically classify a fusion_mfi_emi call from Stage 1 signals.
    Returns the complete 18-field output JSON.
    """
    s = normalize_signals(signals, call_duration_s)
    current_date = current_datetime[:10]

    disposition     = None
    sub_disposition = None
    callback_date   = None
    ptp_date        = None

    # ── Priority chain ────────────────────────────────────────────────────────

    # 0. Voicemail
    if s.get("voicemail_detected"):
        disposition     = "Unclear"
        sub_disposition = "Voice Mail"

    # 0.5. Customer never spoke — connected call where only the agent is audible
    # (e.g. agent recaps previous context, customer stays silent) → Call Hang Up.
    elif s.get("customer_never_spoke"):
        disposition     = "Call Hang Up"
        sub_disposition = "Less Than 20 Secs" if call_duration_s < 20 else "More Than 20 Sec"

    # 1. Promise To Pay
    elif (s.get("payment_intent") and s.get("specific_date_mentioned")
          and s.get("promise_made") and not s.get("third_party_answered")):
        disposition     = "Promise To Pay"
        sub_disposition = _ptp_sub(s["specific_date_mentioned"], current_date, s.get("payment_mode"))
        ptp_date        = _iso_date(s["specific_date_mentioned"])

    # 2. Agree To Pay
    elif (s.get("payment_intent") and not s.get("specific_date_mentioned")
          and not s.get("sm_call_agreed") and not s.get("third_party_answered")):
        disposition     = "Agree To Pay"
        mode = s.get("payment_mode") if s.get("payment_mode") in ("Online", "Cash Pick Up") else "Online"
        intent = "High Intent" if s.get("intent_strength") == "high" else "Low Intent"
        sub_disposition = f"{intent} - {mode}"

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

    # 5. Call Back Requested (Settlement Not Concluded does not exist in EMI flow)
    elif (s.get("callback_requested") and s.get("borrower_confirmed")
          and not s.get("third_party_answered")):
        disposition     = "Call Back Requested"
        sub_disposition = None
        callback_date   = s.get("callback_datetime")

    # 6. Information Conveyed
    elif (s.get("borrower_confirmed") and not s.get("third_party_answered")
          and not s.get("payment_intent") and not s.get("dispute_detected")
          and not s.get("hardship_detected") and not s.get("hardship_soft_signal")
          and not s.get("abrupt_disconnect") and not s.get("voicemail_detected")
          and not s.get("refusal_detected")
          and s.get("engagement_level") == "low"):
        disposition     = "Information Conveyed"
        sub_disposition = None

    # 7. Third Party Connect
    elif s.get("third_party_answered"):
        disposition     = "Third Party Connect"
        sub_disposition = s.get("third_party_type") or "Family Member Picked Up"

    # 8. Financial Hardship
    elif s.get("hardship_detected") or s.get("hardship_soft_signal"):
        disposition     = "Financial Hardship"
        sub_disposition = s.get("hardship_type") or "Other"

    # 9. Dispute
    elif s.get("dispute_detected"):
        disposition     = "Dispute"
        sub_disposition = s.get("dispute_type")

    # 10. Refuse To Pay
    elif s.get("refusal_detected"):
        disposition     = "Refuse To Pay"
        sub_disposition = s.get("refusal_type") or "Unwilling To Pay"

    # 11. Call Hang Up
    elif s.get("abrupt_disconnect") or s.get("disconnected_immediately"):
        disposition     = "Call Hang Up"
        sub_disposition = "Less Than 20 Secs" if call_duration_s < 20 else "More Than 20 Sec"

    elif s.get("no_answer"):
        disposition     = "No Answer"
        sub_disposition = None

    elif s.get("busy_signal"):
        disposition     = "Busy"
        sub_disposition = None

    elif s.get("call_failed"):
        disposition     = "Failed"
        sub_disposition = None

    elif s.get("invalid_number"):
        disposition     = "Invalid Number"
        sub_disposition = None

    elif s.get("disconnected_immediately"):
        disposition     = "Disconnected"
        sub_disposition = None

    elif s.get("switched_off"):
        disposition     = "Switched Off"
        sub_disposition = None

    elif s.get("not_reachable"):
        disposition     = "Not Reachable"
        sub_disposition = None

    elif s.get("incoming_call_barred"):
        disposition     = "Incoming Call Barred"
        sub_disposition = None

    elif (call_duration_s < 20 and s.get("engagement_level") == "low"
          and not s.get("voicemail_detected") and not s.get("no_answer")
          and not s.get("busy_signal") and not s.get("call_failed")
          and not s.get("invalid_number")):
        disposition     = "Call Hang Up"
        sub_disposition = "Less Than 20 Secs"

    else:
        disposition     = "Unclear"
        sub_disposition = "Voice Mail" if s.get("voicemail_detected") else "Other"

    # Guard D: sub-disposition taxonomy validation
    VALID_SUBS = {
        "Promise To Pay":               {"Acceptable date - Online", "Acceptable date - Cash Pick Up",
                                         "Non Acceptable date - Online", "Non Acceptable date - Cash Pick Up"},
        "Agree To Pay":                 {"High Intent - Online", "High Intent - Cash Pick Up",
                                         "Low Intent - Online", "Low Intent - Cash Pick Up"},
        "Payment Claimed":              {"Full payment", "Partial Payment", "Not Sure of Amount"},
        "Agree To Senior Manager Call": {"For Settlement Discussion", "For Other Payment Plan",
                                         "For Further Loan Details", "Other"},
        "Third Party Connect":          {"Family Member Picked Up", "Friend Or Neighbour Picked Up",
                                         "Do Not Know Borrower", "Borrower Died"},
        "Financial Hardship":           {"Medical Issue", "Job Loss", "Business Loss",
                                         "Agriculture Loss", "Death in Family", "Other"},
        "Dispute":                      {"Insurance Claim Related", "Amount Disputed",
                                         "Not Availed Loan", "Already Cleared the Loan", "Fraud Claim"},
        "Call Hang Up":                 {"Less Than 20 Secs", "More Than 20 Sec"},
        "Unclear":                      {"Voice Mail", "Other"},
        "Refuse To Pay":                {"Denied Debt", "Unwilling To Pay", "Abusive"},
    }
    SUB_DEFAULTS = {
        "Payment Claimed": "Not Sure of Amount", "Agree To Senior Manager Call": "Other",
        "Third Party Connect": "Family Member Picked Up", "Financial Hardship": "Other",
        "Unclear": "Other", "Agree To Pay": "Low Intent - Online",
        "Promise To Pay": "Non Acceptable date - Online",
        "Dispute": "Amount Disputed", "Refuse To Pay": "Unwilling To Pay",
    }
    if disposition in VALID_SUBS:
        if sub_disposition not in VALID_SUBS[disposition]:
            sub_disposition = SUB_DEFAULTS.get(disposition)
    else:
        sub_disposition = None

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
