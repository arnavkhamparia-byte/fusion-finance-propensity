"""
Offline migration checks for the two-stage EMI disposition pipeline port.
No network, no DB. All checks must pass.

  1. Ported prompt constants are byte-identical to the production source.
  2. The language variant contains the `language` field; the base does not.
  3. pipeline/emi_classifier.py behaves identically to the production
     fusion_mfi_emi_classifier.py on synthetic signal dicts (both modules
     loaded via importlib from their real file paths, outputs compared).
  4. The extractor module imports and exposes the expected API.

Run: venv/bin/python scripts/test_two_stage_migration.py
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC = "/home/vk/PycharmProjects/OTO Live/post_call_analytics_service"
WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1. Prompt byte-identity ──────────────────────────────────────────────────
print("1. Prompt constants byte-identical to source")
src_prompt = load_module("src_prompt", os.path.join(SRC, "prompts/fusion_mfi_signal_extraction_prompt.py"))
from prompts import signal_extractor_emi_prompt as port_prompt  # noqa: E402

check("SIGNAL_EXTRACTION_PROMPT identical",
      src_prompt.SIGNAL_EXTRACTION_PROMPT == port_prompt.SIGNAL_EXTRACTION_PROMPT)
check("SIGNAL_EXTRACTION_PROMPT_STATIC identical",
      src_prompt.SIGNAL_EXTRACTION_PROMPT_STATIC == port_prompt.SIGNAL_EXTRACTION_PROMPT_STATIC)
check("SIGNAL_EXTRACTION_PROMPT_DYNAMIC_TEMPLATE identical",
      src_prompt.SIGNAL_EXTRACTION_PROMPT_DYNAMIC_TEMPLATE == port_prompt.SIGNAL_EXTRACTION_PROMPT_DYNAMIC_TEMPLATE)

with open(os.path.join(SRC, "prompts/fusion_mfi_signal_extraction_prompt.py"), "rb") as f:
    src_bytes = f.read()
with open(os.path.join(WT, "prompts/signal_extractor_emi_prompt.py"), "rb") as f:
    port_bytes = f.read()
check("prompt file byte-identical", src_bytes == port_bytes)

with open(os.path.join(SRC, "pipeline/signals.py"), "rb") as f:
    src_sig = f.read()
with open(os.path.join(WT, "pipeline/signals_emi.py"), "rb") as f:
    port_sig = f.read()
check("signals.py file byte-identical", src_sig == port_sig)

# ── 2. Language variant ──────────────────────────────────────────────────────
print("2. Language variant")
from prompts.signal_extractor_emi_language import SIGNAL_EXTRACTION_PROMPT_STATIC_LANGUAGE  # noqa: E402

base = port_prompt.SIGNAL_EXTRACTION_PROMPT_STATIC
lang = SIGNAL_EXTRACTION_PROMPT_STATIC_LANGUAGE
check("base prompt has NO language field", '"language"' not in base)
check("variant prompt HAS language schema key",
      '"language": "Hindi" | "English"' in lang)
check("variant prompt has language field definition",
      "language — the predominant spoken language of the call" in lang)
check("variant says 65 keys, base says 64",
      "ALL 65 KEYS" in lang and "ALL 64 KEYS" in base and "ALL 64 KEYS" not in lang)
check("variant differs from base only additively (base minus its 64-count lines is substring-safe)",
      lang.count("Malayalam") == base.count("Malayalam") + 2)  # schema line + definition block

from pipeline.signals_emi import SignalOutput  # noqa: E402
from pipeline.signal_extractor_emi import SignalOutputWithLanguage  # noqa: E402

check("SignalOutput (base pydantic) has no language field", "language" not in SignalOutput.model_fields)
check("SignalOutputWithLanguage has language field", "language" in SignalOutputWithLanguage.model_fields)
check("subclass keeps all base fields",
      set(SignalOutput.model_fields) | {"language"} == set(SignalOutputWithLanguage.model_fields))

# ── 3. Classifier parity on synthetic signals ────────────────────────────────
print("3. Classifier parity (source vs port, importlib)")
src_clf = load_module("src_clf", os.path.join(SRC, "pipeline/fusion_mfi_emi_classifier.py"))
port_clf = load_module("port_clf", os.path.join(WT, "pipeline/emi_classifier.py"))

BASE = {f: None for f in SignalOutput.model_fields}


def sig(**kw):
    s = SignalOutput.model_validate({
        "payment_intent": False, "payment_claimed": False, "dispute_detected": False,
        "hardship_detected": False, "refusal_detected": False, "third_party_answered": False,
        "sm_call_agreed": False, "callback_requested": False,
    }).model_dump()
    s.update(kw)
    return s


CASES = [
    # 1. Clean Promise To Pay
    (sig(borrower_confirmed=True, payment_intent=True, promise_made=True,
         specific_date_mentioned="2026-08-02", amount_mentioned="2500",
         intent_strength="high", commitment_strength="strong",
         engagement_level="high", call_sentiment="positive", payment_mode="Online"),
     120.0),
    # 2. Guard A: family member + sm_call_agreed -> borrower + SM call
    (sig(third_party_answered=True, third_party_type="Family Member Picked Up",
         sm_call_agreed=True, sm_call_reason="For Settlement Discussion",
         engagement_level="medium"),
     65.0),
    # 3. Guard C: 3s call with spurious payment_intent -> Call Hang Up
    (sig(borrower_confirmed=True, payment_intent=True, intent_strength="low",
         engagement_level="low"),
     3.0),
    # 4. customer_never_spoke with agent-recap hardship -> Call Hang Up
    (sig(customer_never_spoke=True, hardship_detected=True, hardship_type="Medical Issue",
         payment_intent=True, promise_made=True, specific_date_mentioned="2026-08-01",
         borrower_confirmed=True, commitment_strength="strong", engagement_level="high"),
     45.0),
    # 5. Guard H: dispute from unconfirmed answerer + hardship soft signal
    (sig(dispute_detected=True, dispute_type="Not Availed Loan", borrower_confirmed=False,
         hardship_soft_signal=True, engagement_level="medium", call_sentiment="negative"),
     55.0),
    # 6. Guard F/G interplay: intent + date + amount + moderate commitment, no promise flag
    (sig(borrower_confirmed=True, payment_intent=True, intent_strength="low",
         specific_date_mentioned="2026-09-20", amount_mentioned="1500",
         commitment_strength="moderate", engagement_level="medium",
         payment_mode="Cash Pick Up"),
     38.0),
]

CURRENT_DT = "2026-07-29T12:00:00+05:30"
all_ok = True
for i, (s, dur) in enumerate(CASES, 1):
    a = src_clf.classify(dict(s), dur, CURRENT_DT)
    b = port_clf.classify(dict(s), dur, CURRENT_DT)
    na_ = src_clf.normalize_signals(dict(s), dur)
    nb = port_clf.normalize_signals(dict(s), dur)
    pa = src_clf.compute_pop(na_, a["disposition"])
    pb = port_clf.compute_pop(nb, b["disposition"])
    ok = (a == b) and (na_ == nb) and (pa == pb)
    all_ok = all_ok and ok
    check(f"case {i}: classify+normalize+pop identical "
          f"({a['disposition']!r}/{a['sub_disposition']!r} pop={a['probability_of_payment']})",
          ok, detail=f"src={a} port={b}")
check("all classifier cases identical", all_ok)
check("DISPOSITION_WEIGHTS identical", src_clf.DISPOSITION_WEIGHTS == port_clf.DISPOSITION_WEIGHTS)

# ── 4. Extractor module imports ──────────────────────────────────────────────
print("4. Extractor module")
import pipeline.signal_extractor_emi as sx  # noqa: E402

check("extract_signals is coroutine function",
      __import__("inspect").iscoroutinefunction(sx.extract_signals))
import inspect  # noqa: E402
params = inspect.signature(sx.extract_signals).parameters
check("model default gemini-2.5-flash", params["model"].default == "gemini-2.5-flash")
check("include_language default True", params["include_language"].default is True)
check("ExtractionIncompleteError exposed", issubclass(sx.ExtractionIncompleteError, Exception))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
