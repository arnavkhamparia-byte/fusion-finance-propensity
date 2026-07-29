"""
Offline unit tests for the disposition_agent migration.
No network, no DB, no LLM — exercises prompt fidelity and the deterministic
post-processing helpers only.

Run: venv python scripts/test_disposition_migration.py (from repo root)
"""

import os
import sys
import json
import asyncio
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE_PROMPT_FILE = "/home/vk/PycharmProjects/OTO Live/post_call_analytics_service/prompts/fusion_mfi_emi_disposition.py"

PASSED = 0


def check(name, cond):
    global PASSED
    assert cond, f"FAILED: {name}"
    PASSED += 1
    print(f"ok: {name}")


# --- 1. prompts import cleanly ---

import prompts.disposition_emi as base_mod
import prompts.disposition_emi_language as lang_mod

check("prompts.disposition_emi imports", True)
check("prompts.disposition_emi_language imports", True)


# --- 2. verbatim prompt matches the source constants exactly ---

src_ns = {}
with open(SOURCE_PROMPT_FILE, "r", encoding="utf-8") as f:
    exec(compile(f.read(), SOURCE_PROMPT_FILE, "exec"), src_ns)

check("DISPOSITION_PROMPT identical to source",
      base_mod.DISPOSITION_PROMPT == src_ns["DISPOSITION_PROMPT"])
check("DISPOSITION_PROMPT_STATIC identical to source",
      base_mod.DISPOSITION_PROMPT_STATIC == src_ns["DISPOSITION_PROMPT_STATIC"])
check("DISPOSITION_PROMPT_DYNAMIC_TEMPLATE identical to source",
      base_mod.DISPOSITION_PROMPT_DYNAMIC_TEMPLATE == src_ns["DISPOSITION_PROMPT_DYNAMIC_TEMPLATE"])


# --- 3. language variant contains the language field; base does not ---

check("base prompt has no language schema line",
      '"language": "string"' not in base_mod.DISPOSITION_PROMPT)
check("base prompt has no language example values",
      '"language":"Hindi"' not in base_mod.DISPOSITION_PROMPT)
check("language variant has the language schema line",
      '"language": "string",' in lang_mod.DISPOSITION_PROMPT_LANGUAGE)
check("language variant has 19-field requirement",
      "All 19 fields are MANDATORY" in lang_mod.DISPOSITION_PROMPT_LANGUAGE
      and "All 18 fields" not in lang_mod.DISPOSITION_PROMPT_LANGUAGE)
check("language rule (17.) present in variant",
      "17. language (MANDATORY)" in lang_mod.DISPOSITION_PROMPT_LANGUAGE)
check("validation gate covers language",
      "✓ language is a single word" in lang_mod.DISPOSITION_PROMPT_LANGUAGE
      and "✓ All 19 fields are present" in lang_mod.DISPOSITION_PROMPT_LANGUAGE)
check("all 6 few-shot examples gained language:Hindi",
      lang_mod.DISPOSITION_PROMPT_LANGUAGE.count('"language":"Hindi"') == 6)
check("static split of variant drops only the datetime line",
      "{current_datetime}" not in lang_mod.DISPOSITION_PROMPT_STATIC_LANGUAGE
      and '"language": "string",' in lang_mod.DISPOSITION_PROMPT_STATIC_LANGUAGE)

# The variant reduces to the base prompt when the language additions are reverted.
reverted = (
    lang_mod.DISPOSITION_PROMPT_LANGUAGE
    .replace('  "language": "string",\n', "")
    .replace("All 19 fields are MANDATORY", "All 18 fields are MANDATORY")
    .replace("\n\n17. language (MANDATORY)"
             "\n- The predominant spoken language of the call audio, as a SINGLE word"
             "\n- One of: Hindi, English, Tamil, Marathi, Bengali, Gujarati, Kannada, Telugu, Punjabi, Odia, Malayalam"
             "\n- If the call is bilingual (e.g. Hinglish), return the dominant language"
             "\n- Detect from the audio itself, not from names or locations mentioned", "")
    .replace("✓ language is a single word from the supported language list\n✓ All 19 fields are present",
             "✓ All 18 fields are present")
    .replace('"language":"Hindi",', "")
)
check("variant reverts exactly to the verbatim base prompt",
      reverted == base_mod.DISPOSITION_PROMPT)


# --- 4. agent module imports; schema contracts ---

from pipeline import disposition_agent as agent

check("pipeline.disposition_agent imports", True)
base_props = list(agent.DispositionOutput.model_json_schema()["properties"])
lang_props = list(agent.DispositionOutputLanguage.model_json_schema()["properties"])
check("base pydantic schema has 18 fields, no language",
      len(base_props) == 18 and "language" not in base_props)
check("language pydantic schema adds only language",
      len(lang_props) == 19 and "language" in lang_props
      and set(lang_props) - set(base_props) == {"language"})


# --- 5. metadata sections (formatting parity with intelligence_service) ---

meta = agent.build_metadata_sections()
check("disposition list section counts 21 dispositions",
      "EXACT MATCH ONLY — 21 DISPOSITIONS" in meta["disposition_list_section"]
      and "1.  Promise To Pay" in meta["disposition_list_section"]
      and "21.  Incoming Call Barred" in meta["disposition_list_section"])
check("sub-disposition table row formatting matches production",
      "| Promise To Pay               | Acceptable date - Cash Pick Up / Non Acceptable date - Cash Pick Up / Acceptable date - Online / Non Acceptable date - Online |"
      in meta["sub_disposition_list_section"])
check("telecom dispositions have null sub-dispositions",
      "| No Answer                    | null (no sub-disposition)" in meta["sub_disposition_list_section"])
check("priority order marks highest and lowest",
      "1.  Promise To Pay               (highest priority)" in meta["disposition_priority_order_section"]
      and "21.  Incoming Call Barred         (lowest priority — Last Resort)" in meta["disposition_priority_order_section"])

# All three placeholders resolve — no stray braces left in the built prompt.
static_with_metadata = (
    lang_mod.DISPOSITION_PROMPT_STATIC_LANGUAGE
    .replace("{disposition_list_section}", meta["disposition_list_section"])
    .replace("{sub_disposition_list_section}", meta["sub_disposition_list_section"])
    .replace("{disposition_priority_order_section}", meta["disposition_priority_order_section"])
)
check("no unresolved placeholders in assembled static prompt",
      "{disposition_list_section}" not in static_with_metadata
      and "{sub_disposition_list_section}" not in static_with_metadata
      and "{disposition_priority_order_section}" not in static_with_metadata
      and "{current_datetime}" not in static_with_metadata)


# --- 6. deterministic helpers on synthetic inputs ---

# normalize_client
check("telecall alias maps to fusion_mfi_emi",
      agent.normalize_client("fusion_mfi_telecall") == "fusion_mfi_emi")
check("generic _telecall suffix stripped",
      agent.normalize_client("seed_fincap_telecall") == "seed_fincap")
check("plain client unchanged",
      agent.normalize_client("fusion_mfi_emi") == "fusion_mfi_emi")

# current_datetime_str with SYSTEM_DATE override
os.environ["SYSTEM_DATE"] = "Tuesday, June 03, 2026 11:30 AM"
check("SYSTEM_DATE override honored",
      agent.current_datetime_str() == "Tuesday, June 03, 2026 11:30 AM")
os.environ["SYSTEM_DATE"] = "2026-06-03"  # wrong format -> ignored
check("invalid SYSTEM_DATE falls back to IST now format",
      agent.current_datetime_str() != "2026-06-03"
      and "," in agent.current_datetime_str())
del os.environ["SYSTEM_DATE"]

# short_call_analysis
sca = agent.short_call_analysis("Tuesday, June 03, 2026 11:30 AM")
check("short-call bypass dict matches production shape",
      sca["disposition"] == "Call Hang Up"
      and sca["sub_disposition"] == "Less Than 20 Secs"
      and sca["compliance_flag"] == "No"
      and sca["immediate_callback_needed"] == "No"
      and sca["customer_attributes"] == []
      and sca["call_rating"] is None
      and sca["date"] == "Tuesday, June 03, 2026 11:30 AM"
      and len(sca) == 20)

# apply_summary_and_remark — 4 synthetic cases
a1 = agent.apply_summary_and_remark(
    {"summary": "pay pay pay pay pay soon"}, activity_type="AI Call", remark=None)
check("AI Call: repetitive summary cleaned, no remark key",
      "pay pay pay pay" not in a1["summary"]
      and a1["activity_type"] == "AI Call" and "remark" not in a1)

a2 = agent.apply_summary_and_remark(
    {"summary": "Customer will pay Rs 2500."}, activity_type="Manual Call", remark="  spoke to husband ")
check("Manual Call: remark appended to existing summary",
      a2["summary"] == "Customer will pay Rs 2500.\n\nremark: spoke to husband"
      and a2["activity_type"] == "Manual Call" and a2["remark"] == "  spoke to husband ")

a3 = agent.apply_summary_and_remark(
    {"summary": None}, activity_type="Manual Call", remark="no answer")
check("Manual Call: remark becomes summary when summary empty",
      a3["summary"] == "remark: no answer" and a3["remark"] == "no answer")

a4 = agent.apply_summary_and_remark(
    {"summary": "All good."}, activity_type="AI Call", remark=None)
check("AI Call without remark: summary untouched, activity_type stamped",
      a4["summary"] == "All good." and a4["activity_type"] == "AI Call")

# _audio_duration_s on garbage input -> 0.0 (no ffprobe-parseable audio)
with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
    tf.write(b"not really audio")
    garbage = tf.name
try:
    check("_audio_duration_s returns 0.0 on undecodable file",
          agent._audio_duration_s(garbage) == 0.0)
finally:
    os.unlink(garbage)

# analyze_call guards (async, but never reaches the network)
check("analyze_call returns None for missing audio",
      asyncio.run(agent.analyze_call("/nonexistent/audio.mp3")) is None)
try:
    asyncio.run(agent.analyze_call("/nonexistent/audio.mp3", client="fusion_mfi_explore"))
    check("analyze_call rejects non-EMI client", False)
except ValueError:
    check("analyze_call rejects non-EMI client", True)

# short-call end-to-end path (real temp file, duration provided -> no LLM call)
with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
    tf.write(b"\x00" * 128)
    tiny = tf.name
try:
    os.environ["SYSTEM_DATE"] = "Tuesday, June 03, 2026 11:30 AM"
    res = asyncio.run(agent.analyze_call(tiny, call_duration_s=12.0))
    check("analyze_call short-call bypass returns production dict offline",
          res is not None
          and res["disposition"] == "Call Hang Up"
          and res["sub_disposition"] == "Less Than 20 Secs"
          and res["date"] == "Tuesday, June 03, 2026 11:30 AM"
          and res["activity_type"] == "AI Call")
finally:
    del os.environ["SYSTEM_DATE"]
    os.unlink(tiny)

print(f"\nAll {PASSED} checks passed.")
