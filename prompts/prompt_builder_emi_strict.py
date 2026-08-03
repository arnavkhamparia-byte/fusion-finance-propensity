"""
Strict-amounts variant of the EMI prompt builder system prompt.

Derived from prompts.prompt_builder_emi at import time via an asserted
replacement (same pattern as narrative_emi_noaudio), so it can never
silently drift from the production prompt. One change: the amounts-as-
spoken-words rule is hardened — benchmark run 1 showed gpt-5.4-mini
writing digit amounts ("Rs 2500") in 15/69 addendums vs the rule.
"""

from prompts.prompt_builder_emi import PROMPT_BUILDER_SYSTEM as _BASE


def _replace(text: str, old: str, new: str, count: int) -> str:
    found = text.count(old)
    if found != count:
        raise AssertionError(
            f"prompt_builder_emi_strict: expected {count} occurrence(s) of {old[:60]!r}, "
            f"found {found} — base prompt changed, review the strict derivation."
        )
    return text.replace(old, new)


_RULE = ('- Write amounts as spoken words in the customer\'s language — NO digits, NO "Rs" prefix, '
         'NO template placeholders. E.g., 2500 → "do hazaar paanch sau rupaye" (Hindi), or '
         'equivalent in the customer\'s language. This applies to all addendums, especially '
         'dialogue examples in few_shot_examples.')

_STRICT_RULE = _RULE + """
  STRICT — ZERO TOLERANCE: an addendum must not contain ANY digit characters (0-9) when
  expressing a rupee amount, anywhere — not in guidance prose, not in dialogue examples,
  not with "Rs", "₹", "INR", or standalone. Every rupee amount everywhere in every addendum
  is written ONLY as romanized Hindi words ("paanch hazaar chaar sau rupaye"). Digits remain
  acceptable ONLY for dates and times (e.g. "29 July 2026", "12 PM"), never for money.
  Before returning, re-read each addendum and convert any digit rupee amount to words."""

PROMPT_BUILDER_SYSTEM_STRICT = _replace(_BASE, _RULE, _STRICT_RULE, count=1)
