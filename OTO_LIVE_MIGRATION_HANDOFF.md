# OTO Live Migration Handoff — Post-Call Analytics Candidate Stack

**For**: the agent session working in `/home/vk/PycharmProjects/OTO Live/post_call_analytics_service`
**From**: the benchmark session in `/home/vk/Desktop/Propensity Score/.claude/worktrees/migrate-narrative-promptbuilder` (branch `worktree-migrate-narrative-promptbuilder`, PR #2)
**Scope**: Phase 1 code changes ONLY. Do not implement shadow mode, cutover, dashboards, or CI — those are handled manually by the user. Do not deploy.

## What this migration is (context in two paragraphs)

The candidate stack was validated on 1000 production accounts (100-account run + 1000-account run, results in the benchmark repo's `data/full_cycle_1000_results.json`): commitments preserved 925/925, status-pattern agreement 83.4%, prompt-block version agreement 96.3%, cost −27% (~$0.0123/account), narrative+builder 3–4× faster. The changes: (1) the disposition stage (unchanged two-stage gemini-2.5-flash) additionally detects the call **language**; (2) the narrative agent stops receiving audio entirely and runs on **azure:gpt-5.4-mini**, taking language from the disposition output; (3) the prompt builder runs on **azure:gpt-5.4-mini** with a strict no-digit-amounts rule.

**Source of truth**: every prompt and logic change is already written and tested in the benchmark worktree (readable at the path above). Copy from those files; do NOT re-derive or paraphrase prompt text — benchmark validity depends on the deployed text being byte-identical to what was tested. The worktree's prompt variants were built from OTO Live production prompts via exact-match `_replace` derivations, so they already correspond line-for-line to this service's current prompts.

## Phase 0 (user-handled — read-only for you)

Secrets: the user will themselves add `AZURE_OPENAI_TARGET_URL` and `AZURE_OPENAI_API_KEY` to the service's environment/secret store. **Never** print, log, commit, or echo these values; reference them only via `os.environ`.

---

# Phase 1 — Code changes

## A. LLM provider layer (new file)

Copy `pipeline/llm_provider.py` from the benchmark worktree into the service (suggested: `post_call_analytics_service/llm_provider.py`). It is self-contained. What it provides:

- `generate(provider_model, system, user_parts, schema, max_output_tokens, timeout_s)` — routes `"gemini*"` → Vertex (google-genai, temperature=0.0/top_p=0.1/top_k=1/seed=42), `"azure:<deployment>"` → `AsyncAzureOpenAI` (endpoint/deployment/api-version parsed from `AZURE_OPENAI_TARGET_URL`; deployment-name precedence: model-name suffix > `AZURE_OPENAI_DEPLOYMENT` env > URL path; fallback api-version `2024-12-01-preview`), anything else → `AsyncOpenAI`.
- Two production-required fallbacks (keep both): retry-without-`temperature` when gpt-5.x rejects it; retry-without-`response_format` (appending `OUTPUT ONLY VALID JSON.` to the system prompt) when a model rejects json_schema.
- Usage tracking for cost logging: task-local via `reset_usage()` / `get_usage()` (concurrency-safe), plus the legacy `LAST_USAGE` module global. Optional in OTO Live; harmless to keep.

**Model selection must be config-driven** so rollback is a config flip, not a deploy:

```python
DISPO_MODEL = os.environ.get("PCA_DISPO_MODEL", "gemini-2.5-flash")
NARRATIVE_MODEL = os.environ.get("PCA_NARRATIVE_MODEL", "azure:gpt-5.4-mini")
BUILDER_MODEL = os.environ.get("PCA_BUILDER_MODEL", "azure:gpt-5.4-mini")
```

Wire the narrative agent and prompt builder call sites through `generate()` (or through the service's existing client factory if integrating there — preserve the routing and both fallbacks exactly).

## B. Disposition agent — add `language`, harden JSON failures

Model stays `gemini-2.5-flash`, two-stage (signal extractor → deterministic classifier). Classifier: **zero changes** (the benchmark port of `fusion_mfi_emi_classifier` was byte-identical and replay-validated 215/216 on production signals).

### B1. Signal-extractor prompt: language field

Apply the 5 exact-match derivations from the benchmark worktree file `prompts/signal_extractor_emi_language.py` to the production prompt in `prompts/fusion_mfi_signal_extraction_prompt.py`. That worktree file IS the derivation script — it contains each `_replace(old, new, count)` with asserted exact counts. Port the same pattern (a language-variant prompt built at import time from the base prompt, with asserts), or apply the edits directly; either way the resulting prompt text must equal the worktree's derived output byte-for-byte. The additions: a `language` key in the output contract (full language name in English, e.g. "Hindi", "Punjabi", "Tamil"; the dominant language actually spoken by the customer), plus the field in the example outputs.

### B2. Schema/validation

Mirror the worktree's `pipeline/signal_extractor_emi.py`: a `SignalOutputWithLanguage(SignalOutput)` Pydantic subclass adding `language: str`. Keep language OUT of the 8 required-signal completeness check (missing language must not raise `ExtractionIncompleteError`; it defaults later).

### B3. Persist language

Include `language` in the disposition output dict written to `ai_disposition_analytics` (inside the same JSON the service already stores — JSONB, no schema migration). It must end up readable from the history rows the narrative agent receives (top-level key of the stored output, alongside `disposition`/`sub_disposition`). This is the contract the narrative agent (C2) depends on.

### B4. Truncated-JSON retry (new hardening — fixes a 3.4%-of-calls failure class)

At the signal-extraction call site, catch JSON-decode failures and completeness failures and retry ONCE with a raised output-token limit:

```python
try:
    signals = await extract_signals(...)          # current max_output_tokens
except (json.JSONDecodeError, ExtractionIncompleteError):
    logger.warning("signal extraction failed, retrying with higher token limit")
    signals = await extract_signals(..., max_output_tokens=16000)  # 2x current
```

(In the 1000-account run, 34/1000 calls hit unterminated-JSON from Gemini; a same-limit retry rescued only some. The raised limit addresses truncation directly.)

## C. Narrative agent — no audio, azure:gpt-5.4-mini

### C1. Prompt

Apply the 4 exact-match derivations from worktree `prompts/narrative_emi_noaudio.py` to production's `prompts/fusion_mfi_emi_narrative_prompt.py`:
1. Latest-call designation: the block instructing that THE NEWEST HISTORY RECORD IS THE CALL THAT JUST ENDED (replaces the audio-based "the recording is the call" framing).
2. `language` field REMOVED from the LLM output contract (it becomes deterministic in code, C2).
3. The 3 example `language` lines removed from few-shot outputs.
4. STRICT tone-enum rule appended to STEP 6: tone MUST be exactly one of `default | firm | confrontational | urgent` (this eliminated all tone drift in benchmarks).

Copy the derivation file's text verbatim — same byte-identity requirement as B1.

### C2. Code changes in `narrative_agent.py`

- **Stop passing audio**: remove recording download/attachment for this flow; the LLM call sends text parts only.
- **Model**: `NARRATIVE_MODEL` (azure:gpt-5.4-mini) via the provider layer.
- **Language is code, not LLM** (this exact precedence, validated in benchmarks):

```python
NO_AUDIO_LANGUAGE = "Hindi"

language = NO_AUDIO_LANGUAGE
if history_list and history_list[0].get("language"):
    language = history_list[0]["language"]
```

  (`history_list[0]` = the just-ended call's disposition row, which now carries `language` from B3. ~32% of calls are non-Hindi — Punjabi/Tamil/Odia/Gujarati/Telugu/Marathi/Bengali — so the fallback should be rare once B ships.)
- **Freshness guard**: before calling the LLM, parse the newest history record's date (formats: `"%A, %B %d, %Y %I:%M %p"` and `"%Y-%m-%d"`) and compare to the processed call's date; log a `STALE HISTORY` warning on mismatch (warn-only, do not block). Reference implementation: `_parse_call_date()` + the guard in worktree `pipeline/narrative_agent.py`.
- **Tone lint** (cheap output check, warn-only): regex the returned tone value against the 4-value enum; log violations. Reference: `_TONE_RE` / `_VALID_TONES` in the same worktree file.
- Narrative output schema: use the no-language response schema (worktree `NarrativeOutputNoAudio`) so the contract matches the C1 prompt.

## D. Prompt builder — strict amounts, azure:gpt-5.4-mini

- **Prompt**: apply the derivation from worktree `prompts/prompt_builder_emi_strict.py` to production's `prompts/fusion_mfi_emi_prompt_builder_prompt.py` — the STRICT ZERO TOLERANCE amounts rule: rupee amounts must NEVER appear as digits anywhere in generated blocks (words only, in the block's language); digits remain allowed for dates and times; includes the final re-read-and-convert instruction. Byte-identical to the worktree text (this took digit violations from 3/69 to 0/71 in benchmarks).
- **Model**: `BUILDER_MODEL` (azure:gpt-5.4-mini) via the provider layer.
- No other logic changes.

## Scope guards (read before starting)

- **EMI flow only** (`fusion_mfi_emi`). Do NOT touch explore/rnr/settlement/msme/seed prompts or classifiers — not validated.
- Do not change the classifier, taxonomy, DB schema, or the voice agent's context-reading path.
- Do not remove the Gemini/audio code paths — both paths must stay resident and selectable via the `PCA_*_MODEL` env vars (rollback = config flip).
- Known open item (user-tracked, not yours): `emi_disclosure` block-version selection showed a directional bias (prod v3 → candidate v1, 85 cases/925); the user may supply additional selection criteria for that block later.

## Verification (offline, no LLM calls, run before committing)

The benchmark worktree has 4 offline suites; port/adapt the relevant asserts to OTO Live paths and run them:
- `scripts/test_two_stage_migration.py` (25 tests) — prompt byte-identity + language-variant derivations + classifier parity + extractor API
- `scripts/test_noaudio_variant.py` (13) — no-audio narrative prompt/schema/language logic
- `scripts/test_disposition_migration.py` (36), `scripts/test_context_migration.py` (43) — reference for derivation-assert patterns

Minimum bar: every `_replace` derivation asserts its exact match count; the language default/precedence logic and tone enum are unit-tested; the classifier replay on a few stored production signal rows still reproduces stored dispositions.
