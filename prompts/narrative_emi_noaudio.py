"""
No-audio variant of the EMI narrative prompt, for the Hindi-only pipeline
where the call recording is never passed to the narrative agent.

Derived from prompts.narrative_emi at import time via asserted replacements,
so it can never silently drift from the audio prompt. Exactly three changes:
  1. The newest history record is designated as the authoritative record of
     the call that just ended (it replaces the audio as the current-call source).
  2. The `language` output field is removed from the contract — language is
     fixed to Hindi deterministically in code, not detected by the model.
  3. `"language": "Hindi"` lines are removed from the few-shot examples.

The dynamic template is unchanged and re-exported for symmetry.
"""

from prompts.narrative_emi import (
    NARRATIVE_PROMPT_STATIC as _BASE_STATIC,
    NARRATIVE_PROMPT_DYNAMIC_TEMPLATE,
)


def _replace(text: str, old: str, new: str, count: int) -> str:
    found = text.count(old)
    if found != count:
        raise AssertionError(
            f"narrative_emi_noaudio: expected {count} occurrence(s) of {old[:60]!r}, "
            f"found {found} — base prompt changed, review the no-audio derivation."
        )
    return text.replace(old, new)


# 1. Designate the newest history record as the current call (audio replacement).
_LATEST_CALL_NOTE = """Up to 10 most recent call records, newest first. Weight recent calls more heavily.
THE NEWEST RECORD IS THE CALL THAT JUST ENDED. There is no call audio — that
newest record's fields (summary, call_sentiment, disposition, ptp_date, amount,
promise_made, commitment_strength) are the authoritative account of what
happened on the current call. Every reference in this prompt to "this call" or
"the current call" means that newest record. Base the narrative update on it;
do not invent details beyond what it and the previous narrative state."""

_STATIC = _replace(
    _BASE_STATIC,
    "Up to 10 most recent call records, newest first. Weight recent calls more heavily.",
    _LATEST_CALL_NOTE,
    count=1,
)

# 2. Remove the `language` field from the output contract.
_STATIC = _replace(
    _STATIC,
    """  "language": "<The predominant language spoken on the call audio. Single word.
    e.g. 'Hindi', 'English', 'Tamil', 'Marathi', 'Bengali', 'Gujarati', 'Kannada',
    'Telugu', 'Punjabi', 'Odia', 'Malayalam'. If the call is bilingual (e.g. Hinglish),
    return the dominant language. Detect from the audio, not the history.>",

""",
    "",
    count=1,
)

# 3. Remove the language line from the three few-shot examples.
_STATIC = _replace(
    _STATIC,
    """  "language": "Hindi",

""",
    "",
    count=3,
)

NARRATIVE_PROMPT_STATIC_NOAUDIO = _STATIC
