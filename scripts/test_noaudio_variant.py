"""
Offline checks for the no-audio (Hindi-only) narrative agent variant.
Run: venv/bin/python scripts/test_noaudio_variant.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prompts.narrative_emi_noaudio import (  # noqa: E402  (asserted derivation runs at import)
    NARRATIVE_PROMPT_STATIC_NOAUDIO as P,
    NARRATIVE_PROMPT_DYNAMIC_TEMPLATE,
)
from prompts.narrative_emi import (  # noqa: E402
    NARRATIVE_PROMPT_STATIC as BASE,
    NARRATIVE_PROMPT_DYNAMIC_TEMPLATE as BASE_DYNAMIC,
)
from pipeline.narrative_agent import (  # noqa: E402
    NarrativeOutput,
    NarrativeOutputNoAudio,
    NO_AUDIO_LANGUAGE,
    _parse_call_date,
)

checks = 0


def ok(cond, msg):
    global checks
    assert cond, msg
    checks += 1
    print(f"ok: {msg}")


# --- prompt derivation ---
ok('"language"' not in P, "language output field removed from contract and examples")
ok("Detect from the audio" not in P, "audio-detection instruction removed")
ok("THE NEWEST RECORD IS THE CALL THAT JUST ENDED" in P, "latest-call designation present")
audio_lines = [l for l in P.splitlines() if "audio" in l.lower()]
ok(
    audio_lines == ["THE NEWEST RECORD IS THE CALL THAT JUST ENDED. There is no call audio — that"],
    "only deliberate audio mention remains",
)
ok(P.count('"language"') == 0 and BASE.count('"language": "Hindi"') == 3,
   "base prompt still has 3 example language lines; derived has none")
ok(NARRATIVE_PROMPT_DYNAMIC_TEMPLATE == BASE_DYNAMIC, "dynamic template unchanged")
# --- schema / agent wiring ---
ok(
    set(NarrativeOutput.model_fields) - set(NarrativeOutputNoAudio.model_fields) == {"language"},
    "no-audio schema differs from audio schema by exactly the language field",
)
ok(NO_AUDIO_LANGUAGE == "Hindi", "no-audio language fixed to Hindi")

# --- freshness-guard date parsing ---
ok(_parse_call_date("Tuesday, July 28, 2026 05:47 PM") == "2026-07-28", "disposition-format date parses")
ok(_parse_call_date("2026-07-28") == "2026-07-28", "plain date parses")
ok(_parse_call_date("2026-07-28T12:00:00+05:30") == "2026-07-28", "ISO timestamp parses")
ok(_parse_call_date("garbage") is None, "unparseable date returns None")
ok(_parse_call_date(None) is None, "None date returns None")

print(f"\nALL {checks} NO-AUDIO CHECKS PASSED")
