"""
Pipeline orchestrator.
Ties Stage 1 (signal extraction) + Stage 2 (classification) + Stage 3 (PoP).

Usage:
  from pipeline.run import process_call

  result = process_call(
      audio_bytes=...,
      mime="audio/mpeg",
      call_duration_s=95.0,
      current_datetime="Monday, June 09, 2026 10:30 AM",
      model="gemini-2.5-flash",
      use_vertex=True,
  )
"""

import os
from datetime import datetime, timezone, timedelta

from pipeline.classifier import classify
from pipeline.extractor  import extract_signals

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_TEXT  = os.path.join(BASE_DIR, "prompts", "signal_extraction.txt")
PROMPT_AUDIO = os.path.join(BASE_DIR, "prompts", "signal_extraction_audio.txt")


def _current_ist() -> str:
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST).strftime("%A, %B %d, %Y %I:%M %p")


def short_call_output() -> dict:
    """Production short-call bypass output (disposition_agent.py): <20s call, no LLM."""
    return {
        "disposition":              "Call Hung Up",
        "sub_disposition":          "Less Than 20 Secs",
        "callback_date":            None,
        "ptp_date":                 None,
        "amount":                   None,
        "call_sentiment":           None,
        "probability_of_payment":   None,
        "compliance_flag":          "No",
        "type_of_compliance_flag":  None,
        "network_quality":          None,
        "conversation_quality":     None,
        "type_of_customer":         None,
        "customer_attributes":      [],
        "immediate_callback_needed": "No",
        "commitment_strength":      None,
        "engagement_level":         None,
        "promise_made":             None,
        "summary":                  None,
    }


def process_call(
    audio_bytes: bytes,
    mime: str,
    call_duration_s: float,
    current_datetime: str = None,
    model: str = "gemini-2.5-flash",
    use_vertex: bool = True,
    prompt_file: str = None,
) -> dict:
    """
    Full pipeline: audio → signals → 18-field output.

    Args:
        audio_bytes:      Raw audio bytes (MP3 or WAV).
        mime:             MIME type — "audio/mpeg" or "audio/wav".
        call_duration_s:  Call duration in seconds (used for Call Hung Up sub-disposition).
        current_datetime: IST datetime string. Defaults to now.
        model:            LLM model name. GPT models use audio prompt; Gemini uses text prompt.
        use_vertex:       Use Vertex AI for Gemini (avoids free-tier limits).
        prompt_file:      Override default prompt file.

    Returns:
        dict with all 18 fields matching the existing output schema.
    """
    if current_datetime is None:
        current_datetime = _current_ist()

    # Short-call bypass — mirrors production: <20s calls skip the LLM entirely
    if 0.0 < call_duration_s < 20.0:
        return short_call_output()

    # Choose prompt: audio variant for GPT, text variant for Gemini/others
    if prompt_file is None:
        prompt_file = PROMPT_AUDIO if model.startswith("gpt-") else PROMPT_TEXT

    # Stage 1 — LLM signal extraction
    signals = extract_signals(
        audio_bytes=audio_bytes,
        mime=mime,
        prompt_file=prompt_file,
        current_datetime=current_datetime,
        model=model,
        use_vertex=use_vertex,
    )

    # Stage 2 + 3 — deterministic classification + PoP
    result = classify(signals, call_duration_s, current_datetime)

    return result
