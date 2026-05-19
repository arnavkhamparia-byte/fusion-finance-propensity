# Propensity Score Project — Session Handoff
**Last updated:** 2026-05-19
**Project:** Fusion Finance MFI — AI Propensity Scoring

---

## What This Project Does

Analyses AI call recordings of loan recovery accounts (Fusion Finance MFI) and predicts the probability of each customer making a payment. Customers are ranked 1–N by payment likelihood. Results are presented on a live React dashboard hosted on GitHub Pages.

---

## Current Status

| Component | Status | Notes |
|-----------|--------|-------|
| DB batch fetch (all accounts) | DONE | `scripts/fetch_account_data.py` — DISTINCT ON query, no LIMIT |
| Gemini audio analysis (4485 accounts) | DONE | 5 batches × ~1000 accounts, 4 concurrent workers |
| Propensity scoring v3 | DONE | Richer history signals, bias-corrected weights |
| S3 presigned URL pipeline | DONE | No local MP3 downloads — streams audio bytes from S3 |
| Recording mismatch handling | DONE | Pre-flight duration check + post-analysis mismatch detection |
| Data enrichment (DB contact fields) | DONE | `scripts/enrich_results.py` |
| Excel report generator | DONE | `scripts/generate_excel.py` — 3 sheets (High/Medium/Low) |
| React dashboard | DONE + LIVE | https://arnavkhamparia-byte.github.io/fusion-finance-propensity/ |
| GitHub Actions CI/CD | DONE | Auto-deploys on push to master |
| Prompt caching (explicit + implicit) | DONE | 2h TTL, auto-retry on expiry |
| Concurrent workers (4x) | DONE | ThreadPoolExecutor, incremental save, resume on crash |

**Latest analysis results (v3 scoring, 4485 accounts — Batch 1–5):**
- High tier: **1,578** | Medium: **1,028** | Low: **1,879**
- Batches: 1 (1022) → 2 (988) → 3 (989) → 4 (991) → 5 (495)

---

## Project Folder Structure

```
/home/vk/Desktop/Propensity Score/
├── HANDOFF.md                         ← This file
├── PLAN.md                            ← Original planning document
├── AI_Propensity_Scoring_PRD.md       ← PM document for manager
├── .env                               ← ALL credentials (NOT committed)
├── .gitignore
├── requirements.txt                   ← Python dependencies (includes boto3, requests)
├── package.json                       ← Node/React dependencies
├── vite.config.js                     ← base: '/fusion-finance-propensity/'
├── tailwind.config.js                 ← OTO dark theme color tokens
├── postcss.config.js
├── index.html                         ← Vite entry point
├── scripts/
│   ├── fetch_account_data.py          ← Step 1: Pulls DB data for all accounts via batch query
│   ├── prompts.py                     ← PROPENSITY_PROMPT (13-field, ~9,520 tokens)
│   ├── analyze_recordings.py          ← Step 2: Main pipeline — audio → Gemini → score
│   ├── enrich_results.py              ← Step 3: Adds DB contact/reachability fields
│   ├── generate_excel.py              ← Step 4: Generates tiered Excel report
│   └── process_single.py             ← One-off: process a single account manually
├── data/
│   ├── account_data.json              ← Raw DB data (gitignored — privacy)
│   ├── propensity_results.json        ← Scored + ranked output (committed)
│   └── propensity_report.xlsx         ← Excel report (gitignored)
│   └── propensity_report_batch5.xlsx  ← Batch 5 only Excel (gitignored)
├── public/
│   └── data/
│       └── propensity_results.json    ← Copy served by Vite (synced by enrich_results.py)
├── src/
│   ├── main.jsx                       ← ReactDOM + HashRouter entry
│   ├── App.jsx                        ← DataContext, routing, loading skeleton
│   ├── index.css                      ← Tailwind directives + base styles
│   ├── lib/
│   │   ├── utils.js                   ← fmtCur, tierBg, scoreColor, etc.
│   │   └── intelligence.js            ← recoveryNarrative, recommendedAction, etc.
│   ├── components/
│   │   └── Header.jsx
│   └── pages/
│       ├── Dashboard.jsx              ← KPI cards, charts, paginated table
│       └── AccountDetail.jsx          ← 4-tab detail page
└── .github/
    └── workflows/
        └── deploy.yml                 ← Build → push dist/ to gh-pages branch
```

---

## Database

**Type:** PostgreSQL (AWS RDS)
**Credentials:** All in `.env` file — `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS`
**Access:** Read-only

**Key tables:**
- `account_details` — loan info, DPD bucket, amounts, contact numbers
- `activity_taskactivity` — AI call history, dispositions, summaries, `call_recording_url`, `call_duration`
- `account_payments` — payment history
- `account_details` also has: `principal_outstanding`, `occupation`, `address`, `lender`, `bounce_amount`, `risk`, `payment_status`, `ptp_amount`, `late installment counts`, reachability flags

**Batch query (fetch_account_data.py):**
```sql
SELECT DISTINCT ON (ad.loan_number)
    ad.id AS account_id, ad.loan_number, ad.name, ad.city,
    ad.loan_amount, ad.dpd_bucket, ad.emi_amount, ad.total_amount_pending,
    ad.assigned_to_id, t.call_recording_url,
    t.disposition AS qualifying_disposition,
    t.call_duration AS qualifying_call_duration,
    t.processed_at AS latest_call_at
FROM activity_taskactivity t
JOIN account_details ad ON ad.id = t.account_id
WHERE t.activity_type = 'AI Call'
  AND t.disposition IN ('Agree To Senior Manager Call','Financial Hardship','Requested Settlement')
  AND ad.assigned_to_id IN (50, 68)
  AND t.processed_at >= CURRENT_DATE - INTERVAL '13 days'
ORDER BY ad.loan_number, t.processed_at DESC
```
- No LIMIT — fetches all accounts (~4500+)
- DISTINCT ON ensures one row per loan_number (most recent qualifying call)
- Slicing into batches is done in analyze_recordings.py via `--batch N --batch-size 1000`

---

## Gemini / Vertex AI Setup

**Model:** `gemini-2.5-flash`
**Provider:** Vertex AI (Google Cloud)
**Project:** `vertex-gemini-oto-cms`
**Location:** `us-central1`
**Credentials:** In `.env` — `GOOGLE_API_KEY` or Vertex AI service account

The prompt (`scripts/prompts.py`) is **self-contained** — does NOT import from external files.

---

## Scoring Formula — v3 (CURRENT)

### Weights

| Factor | Weight | Source |
|--------|--------|--------|
| Commitment strength | **28%** | LLM (from recording) |
| Engagement level | **22%** | LLM (from recording) |
| History trend | **15%** | DB (call history — 3 sub-signals) |
| Disposition | **10%** | LLM (from recording) |
| Sentiment | **10%** | LLM (from recording) |
| DPD bucket | **10%** | DB (account data) |
| Call duration | **5%** | DB (call metadata) |

### History Trend Sub-signals (15% combined)
Three sub-functions inside `history_trend_score()`:
- `contact_rate_score()` — % of calls that connected vs no-answer/busy/failed
- `disposition_trajectory_score()` — recent 3 calls vs previous 3 (improving/declining trend)
- `ptp_reliability_score()` — broken PTPs / total PTPs ratio

### Bonus Points (max +13)
- `promise_made = true` → +5
- `customer_initiated_resolution = true` → +5
- `specific_amount_discussed = true` → +3
- `tone_shift = improved` → +5 / `worsened` → -3
- `engaged_hardship` (Financial Hardship + high engagement + customer initiated) → +10

### Cross-Signal Validation Rules
**Passive Yes Penalty:** If `disposition = "Agree To Senior Manager Call"` AND `commitment in (weak, none)` AND `engagement = low` → cap score at **60**

**Engaged Hardship Boost:** If `disposition = "Financial Hardship"` AND `engagement = high` AND `customer_initiated_resolution = true` → +10 bonus

### Score Maps
```python
DISPOSITION_SCORES = {
    "Agree To Senior Manager Call": 1.00,
    "Requested Settlement":         0.90,
    "Call Back Requested":          0.55,
    "Information Conveyed":         0.30,
    "Unclear":                      0.25,
    "Connected":                    0.20,
    "Call Hang Up":                 0.15,
    "Financial Hardship":           0.15,
    "Dispute":                      0.10,
    "Busy":                         0.10,
    "no-answer":                    0.10,
    "Refuse To Pay":                0.05,
    "Wrong Number":                 0.05,
    "not-connected":                0.05,
    "Failed":                       0.05,
}
COMMITMENT_SCORES = {"strong": 1.0, "moderate": 0.6, "weak": 0.25, "none": 0.0}
ENGAGEMENT_SCORES = {"high": 1.0, "medium": 0.55, "low": 0.15}
SENTIMENT_SCORES  = {"positive": 1.0, "neutral": 0.5, "negative": 0.1}
TONE_SHIFT_BONUS  = {"improved": 5, "neutral": 0, "worsened": -3}
```

### Tiers
- High: score ≥ 65
- Medium: score 40–64
- Low: score < 40

---

## Recording Mismatch Handling

**Pre-flight check:** If `qualifying_call_duration < 20s` → skip Gemini, use DB disposition, set `recording_skipped_short_duration: true`

**Post-analysis mismatch:** If Gemini returns a Stage 1 disposition (Busy/no-answer/not-connected/Call Hang Up/Wrong Number/Failed) but DB has a positive disposition → set `recording_mismatch: true`, override with DB disposition for scoring

```python
STAGE1_DISPOSITIONS = {
    "not-connected", "no-answer", "Busy", "Failed",
    "Wrong Number", "Call Hang Up", "Connected",
}
```

**Important fix:** Always use `qualifying_disposition` and `qualifying_call_duration` from the batch query row — NOT `call_history[0]`, which may be a newer unrelated call.

---

## Concurrent Workers

`analyze_recordings.py` supports `--workers N` (default 1, recommended 4):
```bash
python3 scripts/analyze_recordings.py --batch 1 --workers 4
```

**Thread-safety implementation:**
- `save_lock = threading.Lock()` protects file writes and result list
- `cache_ref = [cache_name]` mutable list allows workers to clear cache on expiry
- `acct_copy = dict(acct)` prevents shared dict mutation
- Incremental save after every account (safe to Ctrl+C and resume)
- Resume: on startup, loads existing `propensity_results.json` and skips already-processed loan_numbers

---

## Prompt Caching

**Explicit (primary):** Named cache created before batch, 2h TTL, cached tokens billed at 25%
**Implicit (fallback):** Prompt sent before audio for Gemini prefix detection
**Expiry handling:** If cache expires mid-batch (400 INVALID_ARGUMENT), clears `cache_ref[0] = None` and retries with implicit caching automatically

---

## How to Run the Full Pipeline

```bash
cd "/home/vk/Desktop/Propensity Score"
source venv/bin/activate

# Step 1: Fetch DB data (run once — no LIMIT, fetches all ~4500 accounts)
python3 scripts/fetch_account_data.py

# Step 2: Analyse recordings in batches (4 workers, 1000 accounts per batch)
python3 scripts/analyze_recordings.py --batch 1 --workers 4
python3 scripts/analyze_recordings.py --batch 2 --workers 4
python3 scripts/analyze_recordings.py --batch 3 --workers 4
python3 scripts/analyze_recordings.py --batch 4 --workers 4
python3 scripts/analyze_recordings.py --batch 5 --workers 4
# Resume after crash: just re-run same command — skips already processed accounts

# Step 3: Enrich with DB contact fields + sync to public/data/
python3 scripts/enrich_results.py

# Step 4: Build + deploy dashboard
npm run build
git add public/data/
git commit -m "Update results: X accounts"
git push origin master
# GitHub Actions auto-deploys to gh-pages

# Step 5: Generate Excel report
python3 scripts/generate_excel.py
# Output: data/propensity_report.xlsx (3 sheets: High/Medium/Low)
```

---

## Dashboard

**Live URL:** https://arnavkhamparia-byte.github.io/fusion-finance-propensity/
**GitHub Repo:** https://github.com/arnavkhamparia-byte/fusion-finance-propensity
**Branch:** `master` → GitHub Actions → `gh-pages` branch → GitHub Pages

**Tech stack:** React 18 + Vite 5 + Tailwind CSS v3 + Recharts + Lucide React + React Router v6 (HashRouter)

**Dashboard features:**
- 4 KPI cards (All / High / Medium / Low) — clickable tier filters
- 3 Recharts charts: tier donut, score distribution bar, top dispositions horizontal bar
- Paginated table (10/page) with search + sort
- Mismatch warning badge (orange) and short-duration skip badge (grey) in Disposition column

**Account Detail (4 tabs):**
- **Contact & Loan** — contacts (primary, secondary, WhatsApp, co-applicant, references), reachability flags, loan metadata
- **AI Analysis** — summary, key reasons, 13-field AI signals, score breakdown bars, recording mismatch banner
- **Call History** — total calls, previous dispositions list
- **Payment Info** — payment status, PTP amount, DPD, late installment trend chart

---

## Excel Report (generate_excel.py)

- 3 sheets: High / Medium / Low Propensity
- 25 columns: Rank, Loan Number, Customer Name, City, Propensity Score, DPD Bucket, Loan Amount, Principal Outstanding, EMI Amount, Primary Contact, WhatsApp, Co-Applicant Name, Co-Applicant Contact, Reference Contact 1, Reference Contact 2, Disposition, Commitment Strength, Engagement Level, Promise Made, Promise Date, Sentiment, Key Reasons, AI Summary, Recording Mismatch, Analysed At
- Color-coded headers (green/orange/red per tier), score-gradient row fills, frozen header row
- Output: `data/propensity_report.xlsx`

To generate Excel for a specific batch only:
```python
# Get batch N loan numbers from account_data.json (sorted keys, 1000 per batch)
# Filter propensity_results.json for those loan_numbers
# Pass filtered data to generate_excel.py with custom INPUT_FILE and OUTPUT_FILE
```

---

## Environment Variables (.env file)

```
# Database
DB_HOST=...
DB_PORT=5432
DB_NAME=...
DB_USER=...
DB_PASS=...

# Google / Vertex AI
GOOGLE_API_KEY=...
GCP_PROJECT_ID=vertex-gemini-oto-cms
GCP_LOCATION=us-central1

# AWS S3
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION_NAME=...
```

---

## GitHub

**Repo:** `arnavkhamparia-byte/fusion-finance-propensity`
**Default branch:** `master` (source code)
**Pages branch:** `gh-pages` (built React app — auto-populated by GitHub Actions)
**Pages URL:** https://arnavkhamparia-byte.github.io/fusion-finance-propensity/

---

## Key Decisions Already Made (Do Not Revisit)

- **React + Vite + Tailwind** chosen over plain HTML
- **HashRouter** used (required for GitHub Pages SPA routing)
- **No Action Queue** feature (explicitly removed)
- **Disposition weight reduced from 30% → 10%** (bias correction — do not revert)
- **Tier thresholds:** High ≥ 65, Medium ≥ 40, Low < 40 (keep as-is)
- **Prompt is self-contained in `prompts.py`** — does NOT import from external production agent files
- **qualifying_disposition / qualifying_call_duration** must come from batch query row, NOT call_history[0]
- **No LIMIT in fetch_account_data.py** — batching done in analyze_recordings.py via --batch flag

---

## Node.js Compatibility Note

Node.js version on this machine: **v18.19.1**
`npm create vite` fails on Node 18 (requires Node 20+). `package.json` was written manually with pinned compatible versions. Do not scaffold with `create-vite` — edit files directly.

---

## Pending Tasks / Next Steps

### 1. Call-Quality Score in Disposition Prompt Output
The user wants to add a **recording-only propensity score** (no DB history) to the Gemini prompt output. This would be a score derived purely from what the LLM hears in the call.

**Proposed weights (recording-only, no DB signals):**

| Signal | Weight | LLM Field |
|--------|--------|-----------|
| Commitment Strength | 35% | `commitment_strength` |
| Engagement Level | 28% | `engagement_level` |
| Disposition | 15% | `disposition` |
| Sentiment | 12% | `sentiment` |
| Tone Shift | 10% | `tone_shift` |

Bonus: promise_made (+5), customer_initiated_resolution (+5), specific_amount_discussed (+3)
Tiers: High ≥ 65, Medium ≥ 40, Low < 40

**Signal definitions:**
- **Commitment Strength** — How firmly the customer commits to paying. Strong = clear promise with date/amount. Moderate = willing but vague. Weak = hesitant. None = no commitment.
- **Engagement Level** — How actively the customer participates. High = asking questions, discussing options. Medium = responding but passive. Low = monosyllabic or evasive.
- **Disposition** — The outcome label of the call (Requested Settlement, Financial Hardship, Agree To Senior Manager Call, etc.)
- **Sentiment** — Overall emotional tone. Positive = cooperative/hopeful. Neutral = flat. Negative = angry/dismissive.
- **Tone Shift** — Whether attitude changed during the call. Improved = started resistant, ended cooperative. Worsened = opposite. Neutral = no change.

**Status:** Design discussed, not yet implemented. To implement: add `call_quality_score` field to Gemini prompt output schema in `prompts.py`, compute it inside `process_single_account()` before the full propensity score calculation.

---

*Handoff updated: 2026-05-19 — 4485 accounts processed across 5 batches*
