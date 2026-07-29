"""
Disposition agent ported from post_call_analytics_service for standalone
offline benchmarking (fusion_mfi_emi only). Reads a local audio file, runs the
single-pass EMI disposition prompt through pipeline.llm_provider (so both
Gemini and OpenAI models can be benchmarked), and returns the disposition
dict. No caching, no DB reads/writes, no narrative trigger.

Deterministic behavior preserved from the production run_disposition_audit:
  - telecall client-name normalization
  - SYSTEM_DATE env override / IST "date" stamping on the analysis
  - mime-type defaulting from the file extension
  - audio-duration detection (mutagen, then ffprobe) when not provided
  - short-call bypass (< 20s -> "Call Hang Up" / "Less Than 20 Secs", no LLM)
  - markdown fence stripping + repair_and_parse_json + dict check
  - clean_repetitive_words on summary, Manual Call remark appending,
    activity_type / remark recording

The production {disposition_list_section} / {sub_disposition_list_section} /
{disposition_priority_order_section} placeholders are filled from a snapshot
of the fusion_finance_mfi disposition/subdisposition tables, formatted with
the exact section-building code ported from intelligence_service.main.
"""

import os
import asyncio
import logging
import subprocess
import argparse
import json
from typing import Optional
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from pipeline.llm_provider import generate
from pipeline.context_utils import repair_and_parse_json, clean_repetitive_words
from prompts.disposition_emi import (
    DISPOSITION_PROMPT_STATIC,
    DISPOSITION_PROMPT_DYNAMIC_TEMPLATE,
)
from prompts.disposition_emi_language import DISPOSITION_PROMPT_STATIC_LANGUAGE

load_dotenv()

logger = logging.getLogger("DispositionAgent")

# Same GEMINI_TIMEOUT_S env-driven constant used in the production service.
GEMINI_TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT_S", "240"))


class DispositionOutput(BaseModel):
    """Schema enforced on the model's response — verbatim from the production
    disposition_agent. Field order matches the prompt's expected JSON layout."""
    disposition: str
    sub_disposition: Optional[str] = None
    callback_date: Optional[str] = None
    ptp_date: Optional[str] = None
    amount: Optional[str] = None
    call_sentiment: Optional[str] = None
    probability_of_payment: Optional[float] = None
    compliance_flag: str
    type_of_compliance_flag: Optional[str] = None
    network_quality: Optional[str] = None
    conversation_quality: Optional[str] = None
    type_of_customer: Optional[str] = None
    customer_attributes: list[str] = Field(default_factory=list)
    immediate_callback_needed: str
    commitment_strength: Optional[str] = None
    engagement_level: Optional[str] = None
    promise_made: Optional[str] = None
    summary: Optional[str] = None


class DispositionOutputLanguage(DispositionOutput):
    """Language-variant contract: adds the `language` field (predominant spoken
    language of the call audio, single word). Declared after all base fields so
    it lands next to `summary` in the serialized schema, matching the prompt."""
    language: Optional[str] = None


# ── EMI disposition metadata (snapshot of the fusion_finance_mfi DB) ─────────
# Snapshot taken 2026-07-29 from disposition / subdisposition tables
# (is_active = true, production ordering: "order" ASC, id ASC). Formatted at
# import time by the section builders below — ported verbatim from
# intelligence_service.main.get_dispositions_metadata — so the resulting prompt
# sections are byte-compatible with what production injects.
EMI_DISPOSITIONS = [
    # (id, name) in ("order" ASC, id ASC) order
    (3, "Promise To Pay"),
    (1, "Agree To Pay"),
    (4, "Payment Claimed"),
    (5, "Agree To Senior Manager Call"),
    (23, "Settlement Not Concluded"),
    (7, "Information Conveyed"),
    (6, "Call Back Requested"),
    (13, "Third Party Connect"),
    (8, "Financial Hardship"),
    (10, "Dispute"),
    (11, "Call Hang Up"),
    (9, "Unclear"),
    (12, "Refuse To Pay"),
    (14, "No Answer"),
    (15, "Busy"),
    (16, "Failed"),
    (17, "Invalid Number"),
    (18, "Disconnected"),
    (24, "Switched Off"),
    (25, "Not Reachable"),
    (26, "Incoming Call Barred"),
]

EMI_SUBDISPOSITIONS = {
    # disposition_id -> allowed sub-disposition names (production ordering)
    1:  ["High Intent - Online", "Low Intent - Online", "High Intent - Cash Pick Up", "Low Intent - Cash Pick Up"],
    3:  ["Acceptable date - Cash Pick Up", "Non Acceptable date - Cash Pick Up", "Acceptable date - Online", "Non Acceptable date - Online"],
    4:  ["Partial Payment", "Full payment", "Not Sure of Amount"],
    5:  ["For Settlement Discussion", "For Other Payment Plan", "For Further Loan Details", "Other"],
    8:  ["Medical Issue", "Job Loss", "Business Loss", "Agriculture Loss", "Death in Family", "Other"],
    9:  ["Voice Mail", "Other"],
    10: ["Insurance Claim Related", "Amount Disputed", "Not Availed Loan", "Already Cleared the Loan", "Fraud Claim"],
    11: ["Less Than 20 Secs", "More Than 20 Sec"],
    12: ["Denied Debt", "Unwilling To Pay", "Abusive"],
    13: ["Family Member Picked Up", "Friend Or Neighbour Picked Up", "Do Not Know Borrower", "Borrower Died"],
    23: ["Needs Lower Settlement Amount", "Did Not Agree For Settlement", "Other"],
}


def build_metadata_sections(dispositions=EMI_DISPOSITIONS, sub_map=EMI_SUBDISPOSITIONS) -> dict:
    """Format the three prompt sections. Formatting logic is a verbatim port of
    intelligence_service.main.get_dispositions_metadata (DB fetch replaced by
    the snapshot constants above)."""
    count = len(dispositions)
    disp_list_lines = [f"{idx}.  {name}" for idx, (_id, name) in enumerate(dispositions, 1)]
    dispositions_formatted = "\n".join(disp_list_lines)

    disposition_list_section = f"""DISPOSITION LIST (EXACT MATCH ONLY — {count} DISPOSITIONS)

Select EXACTLY ONE from the list below. Use the EXACT spelling and capitalization shown:

{dispositions_formatted}

⚠️ Do NOT use any disposition outside this list."""

    table_rows = []
    for d_id, name in dispositions:
        allowed_subs = sub_map.get(d_id, [])
        subs_str = " / ".join(allowed_subs) if allowed_subs else "null (no sub-disposition)"
        table_rows.append(f"| {name:<28} | {subs_str:<104} |")
    table_content = "\n".join(table_rows)

    sub_disposition_list_section = f"""SUB-DISPOSITION LIST (PER DISPOSITION — EXACT MATCH ONLY)

Every disposition has a fixed set of allowed sub-dispositions. Select EXACTLY ONE sub-disposition from the allowed list for the matched disposition. Use EXACT spelling and capitalization.

If the correct sub-disposition genuinely cannot be determined from the call → set sub_disposition to null.

| Disposition                  | Allowed Sub-Dispositions                                                                                  |
|------------------------------|-----------------------------------------------------------------------------------------------------------|
{table_content}"""

    priority_lines = []
    for idx, (_id, name) in enumerate(dispositions, 1):
        if idx == 1:
            desc = " (highest priority)"
        elif idx == count:
            desc = " (lowest priority — Last Resort)"
        else:
            desc = ""
        priority_lines.append(f"{idx}.  {name:<28}{desc}")
    priority_formatted = "\n".join(priority_lines)

    disposition_priority_order_section = f"""DISPOSITION PRIORITY ORDER — FULL REFERENCE

Evaluate in this exact order. Stop at the first match:

{priority_formatted}"""

    return {
        "disposition_list_section": disposition_list_section,
        "sub_disposition_list_section": sub_disposition_list_section,
        "disposition_priority_order_section": disposition_priority_order_section,
    }


# ── Deterministic helpers ported from run_disposition_audit ─────────────────

IST = timezone(timedelta(hours=5, minutes=30))


def normalize_client(client: str) -> str:
    """Telecall alias normalization — verbatim from production."""
    if client == "fusion_mfi_telecall":
        client = "fusion_mfi_emi"
    elif client and client.endswith("_telecall"):
        client = client.removesuffix("_telecall")
    return client


def current_datetime_str(now=None) -> str:
    """SYSTEM_DATE env override (validated against the production format),
    else IST now — verbatim logic from production."""
    system_date_str = os.getenv("SYSTEM_DATE")
    if system_date_str:
        try:
            datetime.strptime(system_date_str, "%A, %B %d, %Y %I:%M %p")
            return system_date_str
        except Exception:
            pass
    now = now or datetime.now(IST)
    return now.strftime("%A, %B %d, %Y %I:%M %p")


def _audio_duration_s(path: str) -> float:
    """Verbatim from production: mutagen first, then ffprobe, else 0.0."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(path)
        if audio is not None and audio.info is not None:
            return float(audio.info.length)
    except Exception as e:
        logger.debug(f"Mutagen duration detection failed: {e}")

    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL,
        )
        return float(out.strip())
    except Exception:
        return 0.0


def short_call_analysis(current_dt: str) -> dict:
    """The exact analysis dict production emits for calls < 20s (LLM bypassed)."""
    return {
        "disposition": "Call Hang Up",
        "sub_disposition": "Less Than 20 Secs",
        "callback_date": None,
        "ptp_date": None,
        "amount": None,
        "call_sentiment": None,
        "probability_of_payment": None,
        "compliance_flag": "No",
        "type_of_compliance_flag": None,
        "network_quality": None,
        "conversation_quality": None,
        "type_of_customer": None,
        "customer_attributes": [],
        "immediate_callback_needed": "No",
        "commitment_strength": None,
        "engagement_level": None,
        "promise_made": None,
        "summary": None,
        "call_rating": None,
        "date": current_dt,
    }


def apply_summary_and_remark(analysis: dict, activity_type: str = "AI Call",
                             remark: Optional[str] = None) -> dict:
    """Verbatim post-processing from production: clean the summary, append a
    Manual Call remark, and always record activity_type / remark."""
    if isinstance(analysis, dict):
        if "summary" in analysis and analysis["summary"]:
            analysis["summary"] = clean_repetitive_words(analysis["summary"])
            if activity_type == "Manual Call" and remark and remark.strip():
                analysis["summary"] = analysis["summary"].strip() + f"\n\nremark: {remark.strip()}"
        elif activity_type == "Manual Call" and remark and remark.strip():
            analysis["summary"] = f"remark: {remark.strip()}"

        # Always record activity_type and remark in the analysis output
        analysis["activity_type"] = activity_type
        if remark:
            analysis["remark"] = remark
    return analysis


# ── Main entry point ─────────────────────────────────────────────────────────

async def analyze_call(
    audio_path: str,
    audio_mime_type: str = "audio/mpeg",
    model: str = "gemini-2.5-flash",
    include_language: bool = True,
    client: str = "fusion_mfi_emi",
    call_duration_s: Optional[float] = None,
    activity_type: str = "AI Call",
    remark: Optional[str] = None,
) -> dict | None:
    """Analyze one call recording and return the disposition dict (the same
    dict production stores as combined_json), or None on failure.

    include_language=True runs the language-variant prompt (adds a "language"
    field to the contract); False runs the verbatim production prompt.
    Offline/read-only: no DB, no caching, no narrative trigger.
    """
    client = normalize_client(client)
    if client != "fusion_mfi_emi":
        raise ValueError(f"analyze_call is ported for fusion_mfi_emi only, got {client!r}")

    logger.info(f"Disposition Agent: Starting audit for {audio_path}")

    if not audio_path or not os.path.exists(audio_path):
        logger.error("Disposition Agent: No valid audio input found.")
        return None

    current_dt = current_datetime_str()

    try:
        mime_type = audio_mime_type or ("audio/mpeg" if audio_path.endswith(".mp3") else "audio/wav")

        def _read_file_sync(p: str) -> bytes:
            with open(p, "rb") as f:
                return f.read()
        audio_bytes = await asyncio.to_thread(_read_file_sync, audio_path)

        if call_duration_s is None:
            call_duration_s = await asyncio.to_thread(_audio_duration_s, audio_path)
            if call_duration_s == 0.0:
                logger.error("Disposition Agent: duration detection failed (ffprobe missing or bad audio) — short-call guards will misfire")
            else:
                logger.info(f"Disposition Agent: Detected audio duration: {call_duration_s:.2f}s")
        else:
            logger.info(f"Disposition Agent: Using provided audio duration: {call_duration_s:.2f}s")

        if 0.0 < call_duration_s < 20.0:
            logger.info(f"Disposition Agent: Call duration {call_duration_s:.2f}s is < 20s. Bypassing LLM agents.")
            analysis = short_call_analysis(current_dt)
        else:
            metadata = build_metadata_sections()
            static_prompt = DISPOSITION_PROMPT_STATIC_LANGUAGE if include_language else DISPOSITION_PROMPT_STATIC
            output_schema = DispositionOutputLanguage if include_language else DispositionOutput

            static_with_metadata = (
                static_prompt
                .replace("{disposition_list_section}", metadata["disposition_list_section"])
                .replace("{sub_disposition_list_section}", metadata["sub_disposition_list_section"])
                .replace("{disposition_priority_order_section}", metadata["disposition_priority_order_section"])
            )
            # Production also substitutes {history_data} here; the EMI dynamic
            # template contains only {current_datetime}, so it's a no-op —
            # kept for fidelity with the production replace chain.
            dynamic_block = (
                DISPOSITION_PROMPT_DYNAMIC_TEMPLATE
                .replace("{current_datetime}", current_dt)
                .replace("{history_data}", "No previous interactions.")
            )

            # Non-cached production construction: audio part first, then the
            # full prompt (static + "---" + dynamic).
            full_prompt = static_with_metadata.rstrip() + "\n\n---\n\n" + dynamic_block

            logger.info(f"Disposition Agent: Calling {model} (include_language={include_language})")
            resp_text = await generate(
                provider_model=model,
                system=None,
                user_parts=[
                    {"audio_bytes": audio_bytes, "mime_type": mime_type},
                    full_prompt,
                ],
                schema=output_schema.model_json_schema(),
                max_output_tokens=8000,
                timeout_s=GEMINI_TIMEOUT_S,
            )

            raw_text = resp_text.replace("```json", "").replace("```", "").strip()
            analysis = repair_and_parse_json(raw_text)

            if not isinstance(analysis, dict):
                raise ValueError(f"AI response is not a JSON object: {raw_text}")

            analysis["date"] = current_dt

        logger.info(f"Disposition Agent: AI matched disposition: {analysis.get('disposition')}")
        analysis = apply_summary_and_remark(analysis, activity_type=activity_type, remark=remark)
        return analysis

    except Exception as e:
        logger.error(f"Disposition Agent: AI Analysis failed: {e}")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Run the ported EMI disposition agent on a local recording.")
    parser.add_argument("--audio_path", type=str, required=True)
    parser.add_argument("--model", type=str, default="gemini-2.5-flash", help="Model to benchmark (gemini* -> Vertex/Gemini, else OpenAI).")
    parser.add_argument("--no_language", action="store_true", help="Use the verbatim production prompt (no language field).")
    parser.add_argument("--client", type=str, default="fusion_mfi_emi")
    args = parser.parse_args()

    result = asyncio.run(analyze_call(
        args.audio_path,
        model=args.model,
        include_language=not args.no_language,
        client=args.client,
    ))
    print(json.dumps(result, indent=2, ensure_ascii=False))
