"""
Language variant of the EMI disposition prompt: identical to the production
prompt except that ONE new output field is added to the JSON contract —
"language", the predominant spoken language of the call audio (single word;
for bilingual calls, the dominant one).

Derived from prompts.disposition_emi at import time via asserted replacements,
so it can never silently drift from the verbatim production copy. Changes:
  1. "language" added to the OUTPUT JSON SCHEMA block (between promise_made
     and summary).
  2. Field count updated 18 → 19 in Field Requirements and the Final
     Validation Gate.
  3. A field rule (17. language) appended after the summary rule.
  4. A validation-gate line added for language.
  5. "language":"Hindi" inserted into all 6 few-shot JSON output examples
     (between promise_made and summary, matching the schema position).

The STATIC/DYNAMIC cacheable split is re-derived exactly the way the source
file derives it (drop the per-call datetime line from STATIC).
"""

from prompts.disposition_emi import (
    DISPOSITION_PROMPT as _BASE_PROMPT,
    DISPOSITION_PROMPT_DYNAMIC_TEMPLATE,
)


def _replace(text: str, old: str, new: str, count: int) -> str:
    found = text.count(old)
    if found != count:
        raise AssertionError(
            f"disposition_emi_language: expected {count} occurrence(s) of {old[:60]!r}, "
            f"found {found} — base prompt changed, review the language derivation."
        )
    return text.replace(old, new)


# 1. Add "language" to the OUTPUT JSON SCHEMA block.
_PROMPT = _replace(
    _BASE_PROMPT,
    """  "promise_made": "string",
  "summary": "string"
}""",
    """  "promise_made": "string",
  "language": "string",
  "summary": "string"
}""",
    count=1,
)

# 2. Field count 18 -> 19.
_PROMPT = _replace(
    _PROMPT,
    "All 18 fields are MANDATORY — never omit any field",
    "All 19 fields are MANDATORY — never omit any field",
    count=1,
)

# 3. Field rule for language, appended after the summary rule (rule 16).
_PROMPT = _replace(
    _PROMPT,
    """16. summary (MANDATORY)
- Maximum 300 characters (count characters including spaces)
- Must be a non-empty string, in English
- For EMI collection calls, include: EMI amount committed (if any), payment date promised (if any),
  payment method understood, customer emotional state
- Single sentence or two short sentences; no line breaks""",
    """16. summary (MANDATORY)
- Maximum 300 characters (count characters including spaces)
- Must be a non-empty string, in English
- For EMI collection calls, include: EMI amount committed (if any), payment date promised (if any),
  payment method understood, customer emotional state
- Single sentence or two short sentences; no line breaks

17. language (MANDATORY)
- The predominant spoken language of the call audio, as a SINGLE word
- One of: Hindi, English, Tamil, Marathi, Bengali, Gujarati, Kannada, Telugu, Punjabi, Odia, Malayalam
- If the call is bilingual (e.g. Hinglish), return the dominant language
- Detect from the audio itself, not from names or locations mentioned""",
    count=1,
)

# 4. Validation-gate line + field count 18 -> 19.
_PROMPT = _replace(
    _PROMPT,
    "✓ All 18 fields are present",
    """✓ language is a single word from the supported language list
✓ All 19 fields are present""",
    count=1,
)

# 5. Few-shot JSON examples: insert "language":"Hindi" between promise_made
# and summary, mirroring the schema position. The CORRECT OUTPUT example and
# Example 1 use promise_made "Yes" (2 occurrences); Examples 2-5 use "No" (4).
_PROMPT = _replace(
    _PROMPT,
    '"promise_made":"Yes","summary":',
    '"promise_made":"Yes","language":"Hindi","summary":',
    count=2,
)
_PROMPT = _replace(
    _PROMPT,
    '"promise_made":"No","summary":',
    '"promise_made":"No","language":"Hindi","summary":',
    count=4,
)

DISPOSITION_PROMPT_LANGUAGE = _PROMPT

# Cacheable split — identical derivation to the source file: STATIC drops the
# per-call datetime line; DYNAMIC re-emits it (re-exported unchanged above).
DISPOSITION_PROMPT_STATIC_LANGUAGE = DISPOSITION_PROMPT_LANGUAGE.replace(
    "Current System DateTime (AUTHORITY): {current_datetime}\n",
    ""
)
