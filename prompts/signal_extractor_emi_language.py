"""
Language variant of the EMI Stage-1 signal extraction prompt.

Derived from prompts.signal_extractor_emi_prompt at import time via asserted
replacements, so it can never silently drift from the production prompt
(which stays byte-identical in its own file). Exactly five changes:
  1. A "language" key is added to the output schema (before "summary").
  2. A field definition block for `language` is added: predominant spoken
     language of the call, single word from a fixed 11-language list;
     bilingual calls return the dominant language.
  3-5. The three "64 keys" counts become "65".

The dynamic template is unchanged and re-exported for symmetry.
"""

from prompts.signal_extractor_emi_prompt import (
    SIGNAL_EXTRACTION_PROMPT_STATIC as _BASE_STATIC,
    SIGNAL_EXTRACTION_PROMPT_DYNAMIC_TEMPLATE,
)


def _replace(text: str, old: str, new: str, count: int) -> str:
    found = text.count(old)
    if found != count:
        raise AssertionError(
            f"signal_extractor_emi_language: expected {count} occurrence(s) of {old[:60]!r}, "
            f"found {found} — base prompt changed, review the language derivation."
        )
    return text.replace(old, new)


# 1. Add the `language` key to the output schema, just before "summary".
_STATIC = _replace(
    _BASE_STATIC,
    """  "bot_naturalness": 1 | 2 | 3 | 4 | 5,
  "summary": "string"
}""",
    """  "bot_naturalness": 1 | 2 | 3 | 4 | 5,
  "language": "Hindi" | "English" | "Tamil" | "Marathi" | "Bengali" | "Gujarati" | "Kannada" | "Telugu" | "Punjabi" | "Odia" | "Malayalam",
  "summary": "string"
}""",
    count=1,
)

# 2. Add the field definition block for `language`, before the summary block.
_STATIC = _replace(
    _STATIC,
    "summary — factual English summary of the call, max 500 chars, no line breaks.",
    """language — the predominant spoken language of the call. Single word, exactly one of:
  Hindi / English / Tamil / Marathi / Bengali / Gujarati / Kannada / Telugu / Punjabi /
  Odia / Malayalam. If the call is bilingual (e.g. Hinglish), return the DOMINANT
  language. Detect from the audio itself, never from names or loan metadata.

────────────────────────────────────────────────────────────────────────────────
summary — factual English summary of the call, max 500 chars, no line breaks.""",
    count=1,
)

# 3-5. The schema now has 65 mandatory keys, not 64.
_STATIC = _replace(
    _STATIC,
    "OUTPUT SCHEMA — ALL 64 KEYS BELOW ARE MANDATORY IN EVERY RESPONSE.",
    "OUTPUT SCHEMA — ALL 65 KEYS BELOW ARE MANDATORY IN EVERY RESPONSE.",
    count=1,
)
_STATIC = _replace(
    _STATIC,
    "STEP 5 — Verify ALL 64 keys are in your JSON output.",
    "STEP 5 — Verify ALL 65 keys are in your JSON output.",
    count=1,
)
_STATIC = _replace(
    _STATIC,
    "Confirm all 64 schema keys are present",
    "Confirm all 65 schema keys are present",
    count=1,
)

SIGNAL_EXTRACTION_PROMPT_STATIC_LANGUAGE = _STATIC
