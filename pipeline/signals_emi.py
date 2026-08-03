"""
SignalOutput — Pydantic schema for Stage-1 signal extraction.
Drop-in replacement for DispositionOutput as the Gemini response_schema in
disposition_agent.py. Field set matches prompts/signal_extraction.txt exactly.
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field

# Fix 1 (schema gate): these 8 fields are REQUIRED — no default. If the model omits
# one, Pydantic validation fails loudly instead of silently patching in a default,
# so the caller can retry the call once (see signal_extractor.ExtractionIncompleteError).
REQUIRED_SIGNAL_FIELDS = (
    "payment_intent", "payment_claimed", "dispute_detected", "hardship_detected",
    "refusal_detected", "third_party_answered", "sm_call_agreed", "callback_requested",
)


class SignalOutput(BaseModel):
    # ── Who was on the call ──────────────────────────────────────────────
    borrower_confirmed: bool = False
    third_party_answered: bool
    third_party_type: Optional[Literal["Family Member Picked Up", "Friend Or Neighbour Picked Up", "Do Not Know Borrower", "Borrower Died"]] = None

    # ── Payment signals ──────────────────────────────────────────────────
    payment_intent: bool
    intent_strength: Literal["high", "low", "none"] = "none"
    specific_date_mentioned: Optional[str] = None   # YYYY-MM-DD
    amount_mentioned: Optional[str] = None
    promise_made: bool = False
    payment_claimed: bool
    payment_claimed_type: Optional[Literal["Full payment", "Partial Payment", "Not Sure of Amount"]] = None

    # ── Outcome signals ──────────────────────────────────────────────────
    sm_call_agreed: bool
    sm_call_reason: Optional[Literal["For Settlement Discussion", "For Other Payment Plan", "For Further Loan Details", "Other"]] = None
    callback_requested: bool
    callback_datetime: Optional[str] = None  # YYYY-MM-DDThh:mm:ss+05:30
    settlement_negotiated: bool = False
    settlement_outcome: Optional[Literal["Needs Lower Settlement Amount", "Did Not Agree For Settlement", "Other"]] = None

    # ── Reason signals ───────────────────────────────────────────────────
    dispute_detected: bool
    dispute_type: Optional[Literal["Insurance Claim Related", "Amount Disputed", "Not Availed Loan", "Already Cleared the Loan", "Fraud Claim"]] = None
    hardship_detected: bool
    hardship_type: Optional[Literal["Medical Issue", "Job Loss", "Business Loss", "Agriculture Loss", "Death in Family", "Other"]] = None
    hardship_soft_signal: bool = False
    refusal_detected: bool
    refusal_type: Optional[Literal["Denied Debt", "Unwilling To Pay", "Abusive"]] = None

    # ── Telephony / call-ending signals ─────────────────────────────────
    customer_never_spoke: bool = False   # call connected, only the agent is audible — customer produced no meaningful speech
    abrupt_disconnect: bool = False
    voicemail_detected: bool = False
    no_answer: bool = False
    busy_signal: bool = False
    call_failed: bool = False
    invalid_number: bool = False
    disconnected_immediately: bool = False
    switched_off: bool = False
    not_reachable: bool = False
    incoming_call_barred: bool = False

    # ── Payment mode ─────────────────────────────────────────────────────
    payment_mode: Optional[Literal["Online", "Cash Pick Up"]] = None

    # ── Soft attributes ──────────────────────────────────────────────────
    commitment_strength: Literal["strong", "moderate", "weak", "none"] = "none"
    engagement_level: Literal["high", "medium", "low"] = "low"
    tone_shift: Literal["improved", "neutral", "worsened"] = "neutral"
    call_sentiment: Literal["positive", "negative", "neutral"] = "neutral"
    type_of_customer: Literal["Co-operative", "Neutral", "Non Co-operative"] = "Neutral"
    network_quality: Literal["Excellent", "Good", "Poor"] = "Good"
    conversation_quality: Literal["Excellent", "Good", "Poor"] = "Good"
    customer_attributes: list[str] = Field(default_factory=list)
    compliance_violation: bool = False
    compliance_type: Optional[Literal["Legal", "RBI", "Police", "Other"]] = None
    immediate_callback_needed: bool = False

    # ── Bot QA signals — judge the BOT/agent only, never the customer ───
    # Tier-1 criticals (any true → call_rating 0)
    bot_hallucination: bool = False        # bizarre / off-persona / abusive bot speech
    bot_dead_air: bool = False             # bot silent >5s with no "are you there?" prompt
    bot_looping: bool = False              # same phrase repeated more than twice
    bot_mid_sentence_drop: bool = False    # crash / call drop while bot mid-sentence
    bot_pressed_after_crisis: bool = False # kept collecting after severe crisis disclosure
    bot_data_leakage: bool = False         # loan details disclosed to unauthorized third party
    # Tier-2 majors (each true → −2)
    bot_ignored_buying_signal: bool = False  # ran hang-up script when customer asked resolution question
    bot_trapped_user: bool = False           # kept pushing after clear "I don't want to talk now"
    bot_argued: bool = False                 # debated / combative with the customer
    bot_dialect_failure: bool = False        # stuck on "can't hear you" due to accent/slang
    bot_unauthorized_commitment: bool = False  # offered waiver / settlement on its own
    bot_premature_termination: bool = False  # hung up before any resolution / PTP / callback
    bot_tone_deaf: bool = False              # demanded money right after crisis mention
    bot_false_promise: bool = False          # promised to resolve legal / police matters
    bot_illogical_callback_time: bool = False  # nonsensical callback vs stated availability
    # Gate + showcase
    escalation_offered: bool = False       # bot offered senior/human callback for out-of-scope query
    bot_naturalness: int = 3               # 1-5 conversational flow / human-ness

    # ── Free text ────────────────────────────────────────────────────────
    summary: str = ""
