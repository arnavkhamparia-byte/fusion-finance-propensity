"""
Narrative agent ported from post_call_analytics_service for standalone
benchmarking (fusion_mfi_emi only). Reads history/context from the DB
read-only; the LLM call is routed through pipeline.llm_provider so both
Gemini and OpenAI models can be benchmarked. No caching, no DB writes.
"""

import os
import re
import argparse
import json
import asyncio
import logging
from typing import Optional
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from pipeline.db_context import get_recent_interactions, get_latest_context
from pipeline.context_utils import repair_and_parse_json, clean_repetitive_words
from pipeline.llm_provider import generate
from prompts.narrative_emi import NARRATIVE_PROMPT_STATIC, NARRATIVE_PROMPT_DYNAMIC_TEMPLATE
from prompts.narrative_emi_noaudio import NARRATIVE_PROMPT_STATIC_NOAUDIO

load_dotenv()

logger = logging.getLogger("NarrativeAgent")


class Commitment(BaseModel):
    type: str
    amount: Optional[float] = None
    due_date: Optional[str] = None
    made_on: Optional[str] = None
    outcome: Optional[str] = None


class NarrativeOutput(BaseModel):
    """Schema enforced on the model's response. Constrained decoding guarantees the
    output parses as JSON — fixes the unescaped-quote / literal-newline failures
    we hit when narrative text was long and multi-paragraph.

    `language` is detected from the audio and passed downstream to prompt_builder
    so the generated agent prompts target the correct call language. Not persisted.

    `commitments` is structured PTP/settlement tracking, only populated (and persisted)
    for clients whose narrative prompt requests it (fusion_mfi_explore, fusion_mfi_emi)."""
    narrative: str
    account_status: str
    language: str
    commitments: list[Commitment] = []


class NarrativeOutputNoAudio(BaseModel):
    """No-audio variant of the contract: `language` is removed from the model's
    output entirely — the pipeline is Hindi-only, so language is fixed
    deterministically in code (NO_AUDIO_LANGUAGE), never asked of the model."""
    narrative: str
    account_status: str
    commitments: list[Commitment] = []


# Language returned by update_account_narrative when use_audio=False.
# Hindi-only scope for the no-audio pipeline.
NO_AUDIO_LANGUAGE = "Hindi"


# Temporal words the narrative/account_status must never contain — absolute
# dates only, since pending/broken status is derived at call time.
_TEMPORAL_WORD_RE = re.compile(
    r"\b(tomorrow|yesterday|next week|day after|upcoming|date has passed|aaj|kal)\b",
    re.IGNORECASE,
)


def _filter_commitments(raw_commitments, account_id) -> list:
    """Keep only well-formed commitment entries (dict, valid type, parseable due_date)."""
    valid = []
    for entry in raw_commitments or []:
        if not isinstance(entry, dict):
            logger.warning(f"Narrative Agent: dropping non-dict commitment for Account {account_id}: {entry!r}")
            continue
        if entry.get("type") not in ("PTP", "SETTLEMENT"):
            logger.warning(f"Narrative Agent: dropping commitment with invalid type for Account {account_id}: {entry!r}")
            continue
        # PTP needs a due_date; SETTLEMENT needs made_on (the 7/10-day window
        # is derived from it at call time — due_date is optional there).
        anchor_field = "due_date" if entry["type"] == "PTP" else "made_on"
        try:
            datetime.strptime(entry.get(anchor_field), "%Y-%m-%d")
        except (TypeError, ValueError):
            logger.warning(f"Narrative Agent: dropping commitment with unparseable {anchor_field} for Account {account_id}: {entry!r}")
            continue
        # Only "kept"/"payment_claimed" are storable outcomes; "broken" (or anything
        # else the model invents) is derived at call time from due_date — normalize
        # it back to null so the voice side doesn't skip the entry.
        if entry.get("outcome") not in (None, "kept", "payment_claimed"):
            logger.warning(f"Narrative Agent: normalizing invalid outcome {entry.get('outcome')!r} to null for Account {account_id}")
            entry["outcome"] = None
        valid.append(entry)
    return valid


def _commitment_key(c: dict) -> tuple:
    """Dedupe key for a commitment, amount-type-normalized (int/str/float all
    compare equal) so merge/backstop logic doesn't double-append an entry the
    model already emitted with a differently-typed amount."""
    amount = c.get("amount")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        pass
    return (c.get("type"), c.get("due_date"), amount)


# Disposition->call-date format used by disposition_agent when it stamps
# `analysis["date"]` (see disposition_agent.py current_dt). history_retriever
# falls back to plain YYYY-MM-DD (from transcript_date) if `date` is missing.
_CALL_DATE_FORMATS = ("%A, %B %d, %Y %I:%M %p", "%Y-%m-%d")


def _parse_call_date(raw) -> Optional[str]:
    """Normalize a history record's "date" stamp to YYYY-MM-DD, or None."""
    if not raw:
        return None
    for fmt in _CALL_DATE_FORMATS:
        try:
            return datetime.strptime(str(raw), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(raw)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _backstop_missing_ptp(history_list: list, commitments: list, account_id) -> list:
    """Deterministic backstop: if the most recent call in history is a
    "Promise To Pay" disposition but the model failed to add a matching entry
    to `commitments`, add it here from the disposition JSON directly. Never
    raises — any parse failure is logged and the backstop is skipped."""
    if not history_list:
        return commitments
    latest = history_list[0]
    if not isinstance(latest, dict) or latest.get("disposition") != "Promise To Pay":
        return commitments

    try:
        due_date = datetime.fromisoformat(str(latest.get("ptp_date"))).strftime("%Y-%m-%d")
        amount = float(latest.get("amount"))
        made_on = None
        made_on_raw = latest.get("date")
        for fmt in _CALL_DATE_FORMATS:
            try:
                made_on = datetime.strptime(made_on_raw, fmt).strftime("%Y-%m-%d")
                break
            except (TypeError, ValueError):
                continue
        if made_on is None:
            raise ValueError(f"unparseable call date {made_on_raw!r}")
    except (TypeError, ValueError) as e:
        logger.warning(f"Narrative Agent: could not parse latest PTP for backstop, Account {account_id}: {e}")
        return commitments

    new_key = ("PTP", due_date, amount)
    if new_key in {_commitment_key(c) for c in commitments}:
        return commitments

    logger.warning(
        f"Narrative Agent: model omitted latest PTP (amount={amount}, due_date={due_date}) "
        f"from commitments for Account {account_id} — adding via deterministic backstop"
    )
    commitments.append({
        "type": "PTP",
        "amount": amount,
        "due_date": due_date,
        "made_on": made_on,
        "outcome": None,
    })
    return commitments


# Same GEMINI_TIMEOUT_S env-driven constant used in the production service.
GEMINI_TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT_S", "240"))


async def update_account_narrative(
    account_id: int,
    audio_path: str = None,
    audio_mime_type: str = "audio/mpeg",
    client: str = "fusion_mfi_emi",
    model: str = "gemini-2.5-flash",
    use_audio: bool = True,
    expected_call_date: str = None,
):
    """
    Fetches history and generates updated narrative/status using AI.
    Returns (narrative, account_status, history_list, language, commitments).
    Read-only — never writes to the database.

    use_audio=False runs the no-audio (Hindi-only) variant: audio_path is
    ignored, the newest history record stands in for the current call, the
    model is not asked for `language` (returned as NO_AUDIO_LANGUAGE), and
    everything else — guards, merge, backstop, caps — is identical.

    expected_call_date ("YYYY-MM-DD", optional, no-audio mode): freshness
    guard — warns if the newest history record is not from this date, i.e.
    the disposition row for the call being processed hasn't landed yet and
    the update would be built on stale history.
    """
    if client == "fusion_mfi_telecall":
        client = "fusion_mfi_emi"
    elif client and client.endswith("_telecall"):
        client = client.removesuffix("_telecall")

    logger.info(f"Narrative Agent: Starting post-call processing for Account ID {account_id}")

    # 1. Fetch interaction history
    history_list = await get_recent_interactions(account_id, client=client)
    if not history_list:
        logger.warning(f"Narrative Agent: No history found for Account ID {account_id}. Using default values.")
        history_str = "No previous interactions."
    else:
        history_str = json.dumps(history_list, indent=2, default=str)

    # Freshness guard (no-audio mode): the newest history record IS the current
    # call, so if it predates the call being processed the update is stale.
    if not use_audio and expected_call_date:
        latest_date = _parse_call_date(history_list[0].get("date")) if history_list else None
        if latest_date != expected_call_date:
            logger.warning(
                f"Narrative Agent: STALE HISTORY for Account {account_id} — newest history record "
                f"is dated {latest_date!r}, expected call date {expected_call_date!r}. The disposition "
                f"row for the current call may not have been written yet."
            )

    # 1b. Fetch previous narrative so the agent updates its own memory
    # instead of rebuilding from the 10-call history window each time.
    previous_narrative = "None — this is the first analysis for this account."
    previous_commitments_str = "None recorded."
    prev_commitments = []
    try:
        prev_ctx = await get_latest_context(account_id, client=client)
        if prev_ctx and prev_ctx.get("narrative"):
            previous_narrative = prev_ctx["narrative"]
        if prev_ctx and isinstance(prev_ctx.get("combined_intelligence"), dict):
            prev_commitments = prev_ctx["combined_intelligence"].get("commitments") or []
            if prev_commitments:
                previous_commitments_str = json.dumps(prev_commitments, indent=2)
    except Exception as e:
        logger.warning(f"Narrative Agent: could not fetch previous narrative: {e}")

    # 2. Generate updated values via LLM
    try:
        ist = timezone(timedelta(hours=5, minutes=30))
        system_date_str = os.getenv("SYSTEM_DATE")
        if system_date_str:
            try:
                datetime.strptime(system_date_str, "%A, %B %d, %Y %I:%M %p")
                current_date = system_date_str
            except Exception:
                current_date = datetime.now(ist).strftime("%A, %B %d, %Y %I:%M %p")
        else:
            current_date = datetime.now(ist).strftime("%A, %B %d, %Y %I:%M %p")

        # Prepare content parts
        content_parts = []

        if use_audio and audio_path and os.path.exists(audio_path):
            logger.info(f"Narrative Agent: Audio detected at {audio_path}. Adding to analysis.")
            mime_type = audio_mime_type or ("audio/mpeg" if audio_path.endswith(".mp3") else "audio/wav")
            def _read_file_sync(p: str) -> bytes:
                with open(p, "rb") as f:
                    return f.read()
            raw_bytes = await asyncio.to_thread(_read_file_sync, audio_path)
            content_parts.append({"audio_bytes": raw_bytes, "mime_type": mime_type})
            audio_context = "\n\nThis is the audio of the last call. Use it to gain deeper insights into the customer's sentiment and tone."
        elif use_audio:
            logger.warning("Narrative Agent: No audio found. Proceeding with text only.")
            audio_context = ""
        else:
            logger.info("Narrative Agent: no-audio mode — newest history record stands in for the current call.")
            audio_context = ""

        # 3. Call AI to generate new narrative, status and behavior
        logger.info(f"Narrative Agent: Calling {model} for Account {account_id}")

        dynamic_block = NARRATIVE_PROMPT_DYNAMIC_TEMPLATE.format(
            current_date=current_date,
            history_data=history_str,
            previous_narrative=previous_narrative,
            previous_commitments=previous_commitments_str,
        ) + audio_context

        # Single-prompt form (no caching for the benchmark) — same construction
        # as the production non-cached fallback path.
        static_prompt = NARRATIVE_PROMPT_STATIC if use_audio else NARRATIVE_PROMPT_STATIC_NOAUDIO
        output_schema = NarrativeOutput if use_audio else NarrativeOutputNoAudio
        content_parts.append(static_prompt.rstrip() + "\n\n---\n\n" + dynamic_block)

        resp_text = await generate(
            provider_model=model,
            system=None,
            user_parts=content_parts,
            schema=output_schema.model_json_schema(),
            max_output_tokens=8000,
            timeout_s=GEMINI_TIMEOUT_S,
        )

        # Handle potential markdown formatting in response
        resp_text = resp_text.strip()
        if resp_text.startswith("```"):
            # Extract content between ```json and ``` or just ``` and ```
            lines = resp_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            resp_text = "\n".join(lines).strip()

        try:
            result = repair_and_parse_json(resp_text)
            narrative = result.get("narrative", "")
            account_status = result.get("account_status", "")
            # No-audio mode is Hindi-only: language is fixed in code, never
            # read from the model (the field isn't in the schema there).
            language = (result.get("language", "") or "") if use_audio else NO_AUDIO_LANGUAGE
            commitments = _filter_commitments(result.get("commitments"), account_id)
            # Deterministic merge: the model may only ADD entries or UPDATE
            # outcomes — never erase history. Re-append any previous commitment
            # it dropped (keyed on type + due_date + amount).
            new_keys = {_commitment_key(c) for c in commitments}
            for prev_entry in _filter_commitments(prev_commitments, account_id):
                if _commitment_key(prev_entry) not in new_keys:
                    logger.warning(f"Narrative Agent: model dropped commitment {_commitment_key(prev_entry)} for Account {account_id} — re-appending")
                    commitments.append(prev_entry)

            # Deterministic backstop: catch a NEW PTP the model mentioned in
            # prose but failed to add to the commitments array.
            commitments = _backstop_missing_ptp(history_list, commitments, account_id)

            narrative = clean_repetitive_words(narrative)
            words = narrative.split()
            if len(words) > 400:
                logger.warning(f"Narrative Agent: narrative exceeded cap ({len(words)} words) — truncating to 350")
                narrative = " ".join(words[:350]) + " …[truncated]"

            temporal_matches = _TEMPORAL_WORD_RE.findall(narrative)
            if temporal_matches:
                logger.warning(f"Narrative Agent: relative-time wording in narrative for Account {account_id}: {temporal_matches}")
            account_status_temporal_matches = _TEMPORAL_WORD_RE.findall(account_status)
            if account_status_temporal_matches:
                logger.warning(f"Narrative Agent: relative-time wording in account_status for Account {account_id}: {account_status_temporal_matches}")
        except json.JSONDecodeError as je:
            logger.error(f"Narrative Agent: JSON Decode Error: {je}")
            logger.error(f"Narrative Agent: Raw AI response that failed: \n{resp_text}")
            return None, None, [], None, []

        logger.info(f"Narrative Agent: Detected call language: {language!r}")
        return narrative, account_status, history_list, language, commitments

    except Exception as e:
        logger.error(f"Narrative Agent: AI generation failed: {e}")
        return None, None, [], None, []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Run the Narrative Agent for a specific account.")
    parser.add_argument("--account_id", type=int, required=True, help="The Account ID to process.")
    parser.add_argument("--audio_path", type=str, help="Optional path to the latest call recording.")
    parser.add_argument("--model", type=str, default="gemini-2.5-flash", help="Model to benchmark (gemini* -> Vertex, else OpenAI).")
    parser.add_argument("--client", type=str, default="fusion_mfi_emi", help="Client name (fusion_mfi_emi only).")
    parser.add_argument("--no_audio", action="store_true",
                        help="No-audio (Hindi-only) mode: newest history record stands in for the current call.")
    parser.add_argument("--expected_call_date", type=str, default=None,
                        help="YYYY-MM-DD freshness guard for --no_audio: warn if the newest history record is older.")

    args = parser.parse_args()

    asyncio.run(update_account_narrative(
        args.account_id,
        audio_path=args.audio_path,
        client=args.client,
        model=args.model,
        use_audio=not args.no_audio,
        expected_call_date=args.expected_call_date,
    ))
