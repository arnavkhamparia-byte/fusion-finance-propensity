"""
Stage-1 EMI signal extractor ported from post_call_analytics_service
(pipeline/signal_extractor.py, fusion_mfi path) for standalone benchmarking.

Differences from production, by design:
  - The LLM call is routed through pipeline.llm_provider.generate (so any
    provider can be benchmarked). No Gemini CachedContent — the static prompt
    is sent as the system instruction, i.e. the production non-cached
    fallback path.
  - include_language=True (default) uses the derived language-variant prompt
    (prompts.signal_extractor_emi_language) and a SignalOutput subclass with
    a `language` field; include_language=False reproduces the production
    prompt/schema exactly.

All deterministic pre/post-processing is preserved verbatim from the source:
markdown-fence strip, non-object check, missing-field warning, Pydantic
round-trip default-fill, and the required-field gate that raises
ExtractionIncompleteError instead of silently patching the 8 required fields.
"""

import json
import logging
import os
from typing import Literal, Optional

from pydantic import ValidationError

from pipeline.llm_provider import generate
from pipeline.signals_emi import SignalOutput, REQUIRED_SIGNAL_FIELDS
from prompts.signal_extractor_emi_prompt import (
    SIGNAL_EXTRACTION_PROMPT_STATIC,
    SIGNAL_EXTRACTION_PROMPT_DYNAMIC_TEMPLATE,
)
from prompts.signal_extractor_emi_language import SIGNAL_EXTRACTION_PROMPT_STATIC_LANGUAGE

# Same GEMINI_TIMEOUT_S env-driven constant used in the production service.
GEMINI_TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT_S", "240"))

logger = logging.getLogger("SignalExtractorEMI")

LANGUAGES = (
    "Hindi", "English", "Tamil", "Marathi", "Bengali", "Gujarati",
    "Kannada", "Telugu", "Punjabi", "Odia", "Malayalam",
)


class SignalOutputWithLanguage(SignalOutput):
    """SignalOutput + detected call language. Subclass so the base schema in
    pipeline.signals_emi stays byte-identical to production. Optional with a
    None default: a missing language must never fail an otherwise-complete
    extraction (it is not one of the 8 required fields)."""
    language: Optional[Literal[
        "Hindi", "English", "Tamil", "Marathi", "Bengali", "Gujarati",
        "Kannada", "Telugu", "Punjabi", "Odia", "Malayalam",
    ]] = None


class ExtractionIncompleteError(Exception):
    """Raised when the model omitted one of the 8 required signal fields.
    Fix 1: these fields must never be silently default-filled — a missing
    required field means the extraction is incomplete and the caller should
    retry the call once."""


async def extract_signals(
    audio_bytes: bytes,
    mime_type: str,
    current_datetime: str,
    model: str = "gemini-2.5-flash",
    include_language: bool = True,
) -> dict:
    """
    Stage 1: extract raw factual signals from call audio. No classification.

    Returns a plain dict (SignalOutput[WithLanguage] fields) ready for
    emi_classifier.classify(). Raises on unrecoverable model errors — caller
    handles retry/logging, same as the production flow.
    """
    # Build the dynamic per-call text (just the datetime).
    dynamic_text = SIGNAL_EXTRACTION_PROMPT_DYNAMIC_TEMPLATE.format(
        current_datetime=current_datetime
    )

    if include_language:
        static_prompt = SIGNAL_EXTRACTION_PROMPT_STATIC_LANGUAGE
        schema_model = SignalOutputWithLanguage
    else:
        static_prompt = SIGNAL_EXTRACTION_PROMPT_STATIC
        schema_model = SignalOutput

    raw_text = await generate(
        provider_model=model,
        system=static_prompt,
        user_parts=[
            dynamic_text,
            {"audio_bytes": audio_bytes, "mime_type": mime_type},
        ],
        schema=schema_model.model_json_schema(),
        max_output_tokens=8000,
        timeout_s=GEMINI_TIMEOUT_S,
    )

    raw = raw_text.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)

    if not isinstance(parsed, dict):
        raise ValueError(f"Signal extraction returned non-object: {raw[:200]}")

    # Defensive default-fill: Gemini structured-output sometimes omits fields the model
    # considered "obviously false" (e.g. the entire hardship/dispute/telephony block),
    # which breaks the classifier's priority chain — every s.get(...) returns None and
    # the call falls through to "Unclear". Round-tripping through the Pydantic schema
    # forces every field to be present with its declared default (False / None / "none"
    # / [] / "" / "low" / "Good" / "Neutral" etc.), so the classifier sees a complete
    # signal dict regardless of what the LLM omitted.
    #
    # Fix 1: the 8 fields in REQUIRED_SIGNAL_FIELDS have NO default in SignalOutput —
    # they must NOT be silently patched. If the model omitted one, model_validate below
    # raises pydantic.ValidationError, which we convert to ExtractionIncompleteError so
    # the caller can retry the call once instead of getting a silently-wrong signal dict.
    missing = [f for f in schema_model.model_fields.keys() if f not in parsed]
    missing_required = [f for f in missing if f in REQUIRED_SIGNAL_FIELDS]
    if missing:
        logger.warning(
            "SignalExtractorEMI: LLM omitted %d field(s): %s (required-and-missing: %s)",
            len(missing), missing, missing_required,
        )
    try:
        signals = schema_model.model_validate(parsed).model_dump()
    except ValidationError as e:
        raise ExtractionIncompleteError(
            f"missing required field(s) {missing_required}: {e}"
        ) from e

    logger.debug("SignalExtractorEMI: Extracted Raw Signals:\n%s", json.dumps(signals, indent=2))
    logger.info(
        "SignalExtractorEMI: borrower=%s third_party=%s sm_call=%s pay_intent=%s disconnect=%s hardship=%s dispute=%s refusal=%s language=%s",
        signals.get("borrower_confirmed"), signals.get("third_party_answered"),
        signals.get("sm_call_agreed"), signals.get("payment_intent"),
        signals.get("abrupt_disconnect"), signals.get("hardship_detected"),
        signals.get("dispute_detected"), signals.get("refusal_detected"),
        signals.get("language"),
    )
    return signals
