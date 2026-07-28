"""
Offline unit tests for the narrative_agent / prompt_builder migration.
No network, no DB, no LLM — exercises only the deterministic functions.

Run: venv python scripts/test_context_migration.py (from repo root)
"""

import os
import sys
import asyncio
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.narrative_agent import _filter_commitments, _commitment_key, _backstop_missing_ptp
from pipeline.prompt_builder import (
    _resolve_commitments_summary,
    _effective_served_blocks,
    _emi_defaults,
    build_prompt_blocks,
)

PASSED = 0


def check(name, cond):
    global PASSED
    assert cond, f"FAILED: {name}"
    PASSED += 1
    print(f"ok: {name}")


# --- _filter_commitments ---

def test_filter_commitments():
    raw = [
        "not a dict",
        {"type": "LOAN", "due_date": "2026-01-01"},                       # invalid type
        {"type": "PTP", "due_date": "01-01-2026"},                        # unparseable due_date
        {"type": "PTP", "due_date": None},                                # missing due_date
        {"type": "PTP", "amount": 500, "due_date": "2026-01-01", "outcome": "broken"},  # outcome normalized
        {"type": "SETTLEMENT", "amount": 900, "made_on": "2026-01-05"},   # valid (due_date optional)
        {"type": "SETTLEMENT", "amount": 900, "made_on": None},           # missing made_on
        {"type": "PTP", "amount": 700, "due_date": "2026-02-01", "outcome": "kept"},    # valid, outcome kept
    ]
    out = _filter_commitments(raw, 1)
    check("filter keeps 3 valid entries", len(out) == 3)
    check("filter normalizes invalid outcome to None", out[0]["outcome"] is None)
    check("filter keeps SETTLEMENT without due_date", out[1]["type"] == "SETTLEMENT")
    check("filter preserves 'kept' outcome", out[2]["outcome"] == "kept")
    check("filter on empty/None input", _filter_commitments(None, 1) == [] and _filter_commitments([], 1) == [])


# --- _commitment_key ---

def test_commitment_key():
    a = {"type": "PTP", "due_date": "2026-01-01", "amount": 500}
    b = {"type": "PTP", "due_date": "2026-01-01", "amount": "500"}
    c = {"type": "PTP", "due_date": "2026-01-01", "amount": 500.0}
    check("key normalizes int/str/float amount", _commitment_key(a) == _commitment_key(b) == _commitment_key(c) == ("PTP", "2026-01-01", 500.0))
    check("key keeps non-numeric amount as-is", _commitment_key({"type": "PTP", "due_date": "2026-01-01", "amount": "abc"}) == ("PTP", "2026-01-01", "abc"))
    check("key handles missing fields", _commitment_key({}) == (None, None, None))


# --- _backstop_missing_ptp ---

def test_backstop_missing_ptp():
    check("backstop no history", _backstop_missing_ptp([], [], 1) == [])
    check("backstop non-PTP latest", _backstop_missing_ptp([{"disposition": "RNR"}], [], 1) == [])

    # Latest call is a PTP the model omitted — appended from disposition JSON.
    hist = [{"disposition": "Promise To Pay", "ptp_date": "2026-08-01",
             "amount": "1200", "date": "Monday, July 27, 2026 03:00 PM"}]
    out = _backstop_missing_ptp(hist, [], 1)
    check("backstop appends missing PTP", out == [{
        "type": "PTP", "amount": 1200.0, "due_date": "2026-08-01",
        "made_on": "2026-07-27", "outcome": None,
    }])

    # Plain YYYY-MM-DD call date (history_retriever fallback format).
    hist2 = [{"disposition": "Promise To Pay", "ptp_date": "2026-08-01",
              "amount": 1200, "date": "2026-07-27"}]
    out2 = _backstop_missing_ptp(hist2, [], 1)
    check("backstop parses plain-date format", out2[0]["made_on"] == "2026-07-27")

    # Dedupe: same PTP already present (str amount) — not double-appended.
    existing = [{"type": "PTP", "amount": "1200", "due_date": "2026-08-01"}]
    out3 = _backstop_missing_ptp(hist, list(existing), 1)
    check("backstop dedupes on typed-amount key", out3 == existing)

    # Unparseable ptp_date — skipped, commitments unchanged.
    bad = [{"disposition": "Promise To Pay", "ptp_date": "soon", "amount": 100, "date": "2026-07-27"}]
    check("backstop skips unparseable ptp_date", _backstop_missing_ptp(bad, [], 1) == [])

    # Unparseable call date — skipped.
    bad2 = [{"disposition": "Promise To Pay", "ptp_date": "2026-08-01", "amount": 100, "date": "27/07/2026"}]
    check("backstop skips unparseable call date", _backstop_missing_ptp(bad2, [], 1) == [])


# --- _resolve_commitments_summary ---

def test_resolve_commitments_summary():
    today = datetime(2026, 7, 27)
    check("summary empty", _resolve_commitments_summary([], today) == "No structured commitments on record.")

    s = _resolve_commitments_summary([{"type": "PTP", "amount": 500, "due_date": "2026-07-20"}], today)
    check("summary PTP overdue", s == "PTP of ₹500 due 2026-07-20 — OVERDUE by 7 days")

    s = _resolve_commitments_summary([{"type": "PTP", "amount": 500, "due_date": "2026-07-27"}], today)
    check("summary PTP due today", s == "PTP of ₹500 due 2026-07-27 — due today")

    s = _resolve_commitments_summary([{"type": "PTP", "amount": 500, "due_date": "2026-07-30"}], today)
    check("summary PTP future", s == "PTP of ₹500 due 2026-07-30 — due in 3 days")

    s = _resolve_commitments_summary([{"type": "PTP", "amount": 500, "due_date": "2026-07-20", "outcome": "kept"}], today)
    check("summary outcome line", s == "₹500 commitment — outcome: kept")

    s = _resolve_commitments_summary([{"type": "SETTLEMENT", "amount": 900, "made_on": "2026-07-22"}], today)
    check("summary settlement window open", s == "Settlement offer of ₹900 made 2026-07-22 — window open (2 of 7 days left)")

    s = _resolve_commitments_summary([{"type": "SETTLEMENT", "amount": 900, "made_on": "2026-07-18"}], today)
    check("summary settlement grace", s == "Settlement offer of ₹900 made 2026-07-18 — grace period (day 9)")

    s = _resolve_commitments_summary([{"type": "SETTLEMENT", "amount": 900, "made_on": "2026-07-10"}], today)
    check("summary settlement expired", s == "Settlement offer of ₹900 made 2026-07-10 — EXPIRED 7 days past the 10-day max")

    s = _resolve_commitments_summary([{"type": "PTP", "amount": 500, "due_date": "bad"}], today)
    check("summary unresolvable", s == "No resolvable structured commitments (dates unparseable or missing).")


# --- _effective_served_blocks ---

def _blocks(version="fusion_emi_v1", addendum=""):
    return {"ptp_collection": {"version": version, "addendum": addendum},
            "few_shot_examples": {"version": "fusion_emi_v1", "addendum": addendum}}


def test_effective_served_blocks():
    as_of = datetime(2026, 7, 27)

    check("served: non-emi client passthrough",
          _effective_served_blocks("fusion_mfi_settlement", _blocks(), [{"type": "PTP", "due_date": "2026-07-01"}], as_of) == _blocks())
    check("served: no commitments passthrough",
          _effective_served_blocks("fusion_mfi_emi", _blocks(), [], as_of) == _blocks())

    def ptp(due, outcome=None):
        return {"type": "PTP", "amount": 100, "due_date": due, "outcome": outcome}

    # 1 broken pending PTP -> first_break
    out = _effective_served_blocks("fusion_mfi_emi", _blocks(), [ptp("2026-07-20")], as_of)
    check("served: 1 break -> first_break", out["ptp_collection"]["version"] == "fusion_emi_first_break")

    # 2 distinct broken due dates -> v2
    out = _effective_served_blocks("fusion_mfi_emi", _blocks(), [ptp("2026-07-20"), ptp("2026-07-22")], as_of)
    check("served: 2 breaks -> v2", out["ptp_collection"]["version"] == "fusion_emi_v2")

    # 3 distinct broken due dates -> serial_v3
    out = _effective_served_blocks("fusion_mfi_emi", _blocks(),
                                   [ptp("2026-07-18"), ptp("2026-07-20"), ptp("2026-07-22")], as_of)
    check("served: 3 breaks -> serial_v3", out["ptp_collection"]["version"] == "fusion_emi_serial_v3")

    # Upcoming, no prior break -> reminder
    out = _effective_served_blocks("fusion_mfi_emi", _blocks(), [ptp("2026-08-01")], as_of)
    check("served: upcoming -> reminder", out["ptp_collection"]["version"] == "fusion_emi_ptp_reminder")

    # Upcoming with a prior break -> reminder_postbreak
    out = _effective_served_blocks("fusion_mfi_emi", _blocks(), [ptp("2026-07-20"), ptp("2026-08-01")], as_of)
    check("served: upcoming postbreak -> reminder_postbreak",
          out["ptp_collection"]["version"] == "fusion_emi_ptp_reminder_postbreak")

    # fusion_emi_v3 never overridden, and its addendum survives broken state
    out = _effective_served_blocks("fusion_mfi_emi", _blocks("fusion_emi_v3", "addendum text"), [ptp("2026-07-20")], as_of)
    check("served: v3 never overridden", out["ptp_collection"]["version"] == "fusion_emi_v3")
    check("served: v3 keeps ptp_collection addendum", out["ptp_collection"]["addendum"] == "addendum text")

    # Broken state strips stale addendums, preserving the "Last miss reason ..." sentence
    blocks_in = _blocks(addendum="Some stale text. Last miss reason was medical emergency. More text.")
    out = _effective_served_blocks("fusion_mfi_emi", blocks_in, [ptp("2026-07-20")], as_of)
    check("served: broken strips addendum keeping miss reason",
          out["ptp_collection"]["addendum"] == "Last miss reason was medical emergency.")
    check("served: few_shot addendum stripped too",
          out["few_shot_examples"]["addendum"] == "Last miss reason was medical emergency.")
    check("served: input not mutated",
          blocks_in["ptp_collection"]["addendum"].startswith("Some stale text"))

    # Resolved (outcome set) PTPs are ignored -> no override
    out = _effective_served_blocks("fusion_mfi_emi", _blocks(), [ptp("2026-07-20", outcome="kept")], as_of)
    check("served: resolved PTP ignored", out["ptp_collection"]["version"] == "fusion_emi_v1")


# --- build_prompt_blocks first-call defaults path (no LLM) ---

def test_first_call_defaults():
    expected = _emi_defaults()
    check("_emi_defaults shape",
          expected == {b: {"version": "fusion_emi_v1", "addendum": ""} for b in [
              "system_role", "identity_verification", "language_rules", "tone_principles",
              "emi_disclosure", "reason_handling", "ptp_collection", "payment_guidance",
              "few_shot_examples", "closing_phase"]})

    for narrative in ("", None, "No narrative set.", "  __NO_HISTORY__  ", "None - fresh call"):
        result = asyncio.run(build_prompt_blocks(narrative, "status", [], client="fusion_mfi_emi"))
        check(f"defaults for narrative={narrative!r}", result == expected)


if __name__ == "__main__":
    test_filter_commitments()
    test_commitment_key()
    test_backstop_missing_ptp()
    test_resolve_commitments_summary()
    test_effective_served_blocks()
    test_first_call_defaults()
    print(f"\nALL {PASSED} CHECKS PASSED")
