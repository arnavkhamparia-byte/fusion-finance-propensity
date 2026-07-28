"""
prompt_builder.py — LLM-powered block customizer (fusion_mfi_emi only).
Ported from post_call_analytics_service/prompt_builder.py for standalone
Gemini-vs-OpenAI benchmarking. Reads narrative + account_status +
recent_history → selects correct block versions per scenario + generates
customer-specific addendums for key blocks. Result is returned to the
caller; persistence is handled by the benchmark harness (local JSON only).
"""

import os
import re
import copy
import json
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from pipeline.llm_provider import generate
from pipeline.context_utils import repair_and_parse_json
from prompts.prompt_builder_emi import PROMPT_BUILDER_SYSTEM

load_dotenv()

logger = logging.getLogger("PromptBuilder")

# Hard ceiling on any single LLM call.
GEMINI_TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT_S", "240"))

# EMI collection call blocks
EMI_ALL_BLOCKS = [
    "system_role", "identity_verification", "language_rules", "tone_principles",
    "emi_disclosure", "reason_handling", "ptp_collection", "payment_guidance",
    "few_shot_examples", "closing_phase"
]

EMI_ADDENDUM_BLOCKS = {
    "system_role", "ptp_collection", "reason_handling", "few_shot_examples"
}

# Known valid versions per EMI block — blocks not listed here only accept "fusion_emi_v1"
EMI_VALID_VERSIONS = {
    "emi_disclosure": {"fusion_emi_v1", "fusion_emi_v2", "fusion_emi_v3"},
    "reason_handling": {"fusion_emi_v1", "fusion_emi_v2"},
    "ptp_collection": {"fusion_emi_v1", "fusion_emi_v2", "fusion_emi_v3", "fusion_emi_first_break",
                       "fusion_emi_serial_v3", "fusion_emi_ptp_reminder", "fusion_emi_ptp_reminder_postbreak"},
}


class PromptBuilderOutput(BaseModel):
    version_decisions: dict[str, str] = Field(default_factory=dict)
    addendums: dict[str, str] = Field(default_factory=dict)
    strategy: dict[str, str] = Field(default_factory=dict)


def _strip_stale_addendum(blocks: dict, block_name: str) -> None:
    """Blank a stale builder addendum on `blocks[block_name]`, preserving any
    'Last miss reason ...' sentence (the broken-PTP scripts reference the reason
    given last time). Mirrors prompt_blocks/ptp_status.py's strip_stale_addendum
    in the voice repo — keep in sync. No-op if block/addendum is missing."""
    block = blocks.get(block_name)
    if not isinstance(block, dict):
        return
    addendum = block.get("addendum", "")
    if not addendum:
        return
    m = re.search(r"Last miss reason[^.]*\.", addendum)
    block["addendum"] = m.group(0) if m else ""


def _resolve_commitments_summary(commitments: list, today: datetime) -> str:
    """Deterministic PTP/settlement status summary computed in Python (not the LLM) so
    version selection can rely on ground truth rather than prose inference. `today` must
    be a naive/aware datetime whose date() is used for day-delta math."""
    if not commitments:
        return "No structured commitments on record."

    lines = []
    for c in commitments:
        outcome = c.get("outcome")
        amount = c.get("amount")
        ctype = c.get("type")

        if outcome:
            lines.append(f"₹{amount} commitment — outcome: {outcome}")
            continue

        if ctype == "PTP":
            due_date = c.get("due_date")
            if not due_date:
                continue
            try:
                due = datetime.strptime(due_date, "%Y-%m-%d").date()
            except Exception:
                continue
            delta = (due - today.date()).days
            if delta < 0:
                lines.append(f"PTP of ₹{amount} due {due_date} — OVERDUE by {abs(delta)} days")
            elif delta == 0:
                lines.append(f"PTP of ₹{amount} due {due_date} — due today")
            else:
                lines.append(f"PTP of ₹{amount} due {due_date} — due in {delta} days")

        elif ctype == "SETTLEMENT":
            made_on = c.get("made_on")
            if not made_on:
                continue
            try:
                made = datetime.strptime(made_on, "%Y-%m-%d").date()
            except Exception:
                continue
            elapsed = (today.date() - made).days
            if elapsed <= 7:
                lines.append(
                    f"Settlement offer of ₹{amount} made {made_on} — window open ({7 - elapsed} of 7 days left)"
                )
            elif elapsed <= 10:
                lines.append(f"Settlement offer of ₹{amount} made {made_on} — grace period (day {elapsed})")
            else:
                lines.append(
                    f"Settlement offer of ₹{amount} made {made_on} — EXPIRED {elapsed - 10} days past the 10-day max"
                )

    if not lines:
        return "No resolvable structured commitments (dates unparseable or missing)."
    return "\n".join(lines)


def _effective_served_blocks(client: str, previous_blocks: dict | None, commitments: list | None,
                              as_of_date: datetime) -> dict:
    """Reconstruct the block versions actually SERVED on the last call, not just what was
    stored. The voice agent applies deterministic dial-time guards that can override the
    stored version depending on live PTP state — so what's persisted in prompt_blocks can
    differ from what the customer actually heard. Mirrors the guards in
    fusion_contextual_prompt_emi.py (ptp_collection guard — tiered by broken_count:
    first_break <=1, v2 ==2, serial_v3 >=3, reminder/reminder_postbreak for upcoming;
    fusion_emi_v3 never overridden) in the voice repo — keep in sync with those.

    Also mirrors the voice repo's addendum-staleness handling (strip_stale_addendum in
    prompt_blocks/ptp_status.py, applied both on the version flip and via the
    state-contradiction guard): when the mirrored state is broken, a stored addendum
    predates the miss and is stripped down to any preserved "Last miss reason ..."
    sentence. Voice decides staleness there via builder_predates_break(generated_at,
    days), fail-safing to "stale" when generated_at is missing/unparseable. This function
    is not given the prior payload's generated_at timestamp, so it always takes that
    fail-safe branch and strips on broken state — the conservative choice.

    Returns a COPY of previous_blocks with the guard(s) applied; never mutates the input.
    `as_of_date` must be the LAST call's date (the guard's clock at call time), not today.
    """
    blocks = copy.deepcopy(previous_blocks) if previous_blocks else {}
    if not commitments or client != "fusion_mfi_emi":
        return blocks

    as_of = as_of_date.date() if isinstance(as_of_date, datetime) else as_of_date

    # Mirror resolve_commitments()'s PTP-only clock (commitments-first path; the
    # interactions fallback isn't replicated — commitments are the analytics-side ground truth).
    ptp_pending = []
    for entry in commitments:
        if not isinstance(entry, dict) or entry.get("type") != "PTP":
            continue
        if entry.get("outcome"):  # "kept"/"payment_claimed"/anything non-null → resolved, skip
            continue
        due_date = entry.get("due_date")
        if not due_date:
            continue
        try:
            due = datetime.strptime(due_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        ptp_pending.append((due, entry))

    ptp_state, broken_count = None, 0
    if ptp_pending:
        broken_count = len({due for due, _ in ptp_pending if due < as_of})
        latest_due, _ = max(ptp_pending, key=lambda pair: pair[0])
        ptp_state = "broken" if latest_due < as_of else "upcoming"

    version = blocks.get("ptp_collection", {}).get("version")
    _overridable = (None, "", "fusion_emi_v1", "fusion_emi_first_break", "fusion_emi_v2",
                    "fusion_emi_serial_v3", "fusion_emi_ptp_reminder", "fusion_emi_ptp_reminder_postbreak")
    if ptp_state == "broken" and broken_count >= 3 and version in _overridable:
        blocks.setdefault("ptp_collection", {})["version"] = "fusion_emi_serial_v3"
    elif ptp_state == "broken" and broken_count == 2 and version in _overridable:
        blocks.setdefault("ptp_collection", {})["version"] = "fusion_emi_v2"
    elif ptp_state == "broken" and broken_count <= 1 and version in (
            None, "", "fusion_emi_v1", "fusion_emi_ptp_reminder", "fusion_emi_ptp_reminder_postbreak"):
        blocks.setdefault("ptp_collection", {})["version"] = "fusion_emi_first_break"
    elif ptp_state == "upcoming" and broken_count >= 1 and version in _overridable:
        blocks.setdefault("ptp_collection", {})["version"] = "fusion_emi_ptp_reminder_postbreak"
    elif ptp_state == "upcoming" and version in _overridable:
        blocks.setdefault("ptp_collection", {})["version"] = "fusion_emi_ptp_reminder"

    if ptp_state == "broken" and blocks.get("ptp_collection", {}).get("version") != "fusion_emi_v3":
        _strip_stale_addendum(blocks, "ptp_collection")
        _strip_stale_addendum(blocks, "few_shot_examples")

    return blocks


async def build_prompt_blocks(
    narrative: str,
    account_status: str,
    recent_history: list,
    client: str = "fusion_mfi_emi",
    default_language: str = "Hindi",
    previous_blocks: dict | None = None,
    commitments: list | None = None,
    model: str = "gemini-2.5-flash",
) -> dict | None:
    """
    Selects block versions + generates customer-specific addendums via LLM.
    Returns the prompt_blocks dict, or None on failure.
    """
    if client == "fusion_mfi_telecall":
        client = "fusion_mfi_emi"
    elif client and client.endswith("_telecall"):
        client = client.removesuffix("_telecall")

    logger.info("Prompt Builder: Starting")

    # First-ever call — no narrative yet, use defaults without an LLM call
    no_history_indicators = ["", "__no_history__", "no narrative set.", "no narrative.",
                             "error retrieving narrative.", "none - fresh call"]
    if not narrative or narrative.strip().lower() in no_history_indicators:
        prompt_blocks = _emi_defaults()
        logger.info("Prompt Builder: No history — returning EMI defaults")
        return prompt_blocks

    history_str = json.dumps(recent_history, indent=2, default=str) if recent_history else "No prior interactions."

    ist = timezone(timedelta(hours=5, minutes=30))
    system_date_str = os.getenv("SYSTEM_DATE")
    if system_date_str:
        try:
            today_dt = datetime.strptime(system_date_str, "%A, %B %d, %Y %I:%M %p")
            current_date_str = system_date_str
        except Exception:
            today_dt = datetime.now(ist)
            current_date_str = today_dt.strftime("%A, %B %d, %Y %I:%M %p")
    else:
        today_dt = datetime.now(ist)
        current_date_str = today_dt.strftime("%A, %B %d, %Y %I:%M %p")

    user_message = f"""TODAY'S DATE: {current_date_str}
CUSTOMER LANGUAGE (detected from the most recent call audio by the narrative agent): {default_language}

NARRATIVE:
{narrative}

ACCOUNT STATUS:
{account_status}

RECENT HISTORY:
{history_str}"""

    if previous_blocks:
        # The guard(s) ran at LAST call time, keyed off PTP state as of THAT call — not
        # today — so derive as_of_date from the most recent call in recent_history
        # (ORDER BY created_at DESC → index 0), falling back to today if unparseable.
        last_call_date = today_dt
        if recent_history and isinstance(recent_history[0], dict):
            raw_date = recent_history[0].get("transcript_date")
            if raw_date:
                try:
                    last_call_date = datetime.fromisoformat(str(raw_date))
                except ValueError:
                    pass
        served_blocks = _effective_served_blocks(client, previous_blocks, commitments, last_call_date)
        prev_versions = {k: v.get("version") for k, v in served_blocks.items()
                         if isinstance(v, dict) and "version" in v}
        prev_strategy = served_blocks.get("_strategy") or {}
        user_message += (
            "\n\nLAST CALL'S PRESCRIPTION (what was served on the most recent call):\n"
            f"Block versions: {json.dumps(prev_versions)}\n"
            f"Strategy: {json.dumps(prev_strategy)}"
        )

    if commitments:
        user_message += (
            "\n\nSTRUCTURED COMMITMENTS (ground truth for PTP/settlement status — prefer this over prose inference):\n"
            f"{json.dumps(commitments, indent=2, default=str)}\n\n"
            f"As of processing time ({current_date_str}):\n"
            f"{_resolve_commitments_summary(commitments, today_dt)}"
        )

    logger.info(f"Prompt Builder: Using prompt '{client}_prompt_builder' for client '{client}' with model '{model}'")

    try:
        resp_text = await generate(
            provider_model=model,
            system=PROMPT_BUILDER_SYSTEM,
            user_parts=[user_message],
            schema=PromptBuilderOutput.model_json_schema(),
            max_output_tokens=8000,
            timeout_s=GEMINI_TIMEOUT_S,
        )

        resp_text = resp_text.strip()
        if resp_text.startswith("```"):
            lines = resp_text.splitlines()
            lines = lines[1:] if lines[0].startswith("```") else lines
            lines = lines[:-1] if lines and lines[-1].startswith("```") else lines
            resp_text = "\n".join(lines).strip()

        result = repair_and_parse_json(resp_text)
        version_decisions = result.get("version_decisions", {})
        addendums = result.get("addendums", {})

        for k, v in addendums.items():
            if isinstance(v, str) and len(v) > 900:
                logger.warning(f"Prompt Builder: addendum for '{k}' is {len(v)} chars — truncating to 900")
                cut = v[:900]
                # cut at the last completed line/sentence so dialogue examples never end mid-thought
                boundary = max(cut.rfind("\n"), cut.rfind("। "), cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind('।"'), cut.rfind('."'), cut.rfind('?"'), cut.rfind('!"'))
                addendums[k] = cut[:boundary + 1].rstrip() if boundary > 300 else cut.rsplit(" ", 1)[0] + " …"

        prompt_blocks = {}
        blocks_list = EMI_ALL_BLOCKS
        addendum_set = EMI_ADDENDUM_BLOCKS
        default_version = "fusion_emi_v1"

        for block in blocks_list:
            version_val = version_decisions.get(block, default_version)
            prompt_blocks[block] = {
                "version": version_val,
                "addendum": addendums.get(block, "") if block in addendum_set else ""
            }

        for block in blocks_list:
            v = prompt_blocks[block]["version"]
            valid = EMI_VALID_VERSIONS.get(block, {default_version})
            if v not in valid:
                logger.warning(f"Prompt Builder: unknown version '{v}' for block '{block}' — coercing to {default_version}")
                prompt_blocks[block]["version"] = default_version

        logger.info(
            f"Prompt Builder: Done — system_role={prompt_blocks['system_role']['version']}, "
            f"ptp_collection={prompt_blocks['ptp_collection']['version']}"
        )
        return prompt_blocks

    except Exception as e:
        logger.error(f"Prompt Builder: Failed: {e}")
        return None


def _emi_defaults() -> dict:
    """Default EMI structure for first-ever EMI collection calls (no LLM needed)."""
    result = {}
    for block in EMI_ALL_BLOCKS:
        result[block] = {"version": "fusion_emi_v1", "addendum": ""}
    return result
