# Propensity Score Project — Session Handoff
**Last updated:** 2026-05-18
**Project:** Fusion Finance MFI — AI Propensity Scoring

---

## What This Project Does

Analyses AI call recordings of loan recovery accounts (Fusion Finance MFI) and predicts the probability of each customer making a payment. Customers are ranked 1–N by payment likelihood. Results are presented on a live React dashboard hosted on GitHub Pages.

---

## Current Status — Everything Completed

| Component | Status | Notes |
|-----------|--------|-------|
| DB data fetch (30 accounts) | DONE | `scripts/fetch_account_data.py` |
| Gemini audio analysis (30/30) | DONE | `scripts/analyze_recordings.py` |
| Propensity scoring v2 | DONE | Bias-corrected weights (see below) |
| Data enrichment (DB contact fields) | DONE | `scripts/enrich_results.py` |
| React dashboard | DONE + LIVE | https://arnavkhamparia-byte.github.io/fusion-finance-propensity/ |
| GitHub Actions CI/CD | DONE | Auto-deploys on push to master |
| Prompt caching (explicit + implicit) | DONE | 72% cache hit rate on last run |
| Token logging per call | DONE | Logged in results JSON + terminal |

**Latest analysis results (v2 scoring, 30 accounts):**
- High tier: **14** | Medium: **3** | Low: **13**
- Avg score: ~54 | Top score: 80.5 | Lowest: 11.3
- Total tokens used: 421,598 | Cost: ₹1.11 | Saved via cache: ₹1.18

---

## Project Folder Structure

```
/home/vk/Desktop/Propensity Score/
├── HANDOFF.md                         ← This file
├── PLAN.md                            ← Original planning document
├── AI_Propensity_Scoring_PRD.md       ← PM document for manager
├── .env                               ← ALL credentials (NOT committed)
├── .gitignore
├── requirements.txt                   ← Python dependencies
├── package.json                       ← Node/React dependencies
├── vite.config.js                     ← base: '/fusion-finance-propensity/'
├── tailwind.config.js                 ← OTO dark theme color tokens
├── postcss.config.js
├── index.html                         ← Vite entry point
├── scripts/
│   ├── fetch_account_data.py          ← Pulls DB data for all accounts
│   ├── prompts.py                     ← PROPENSITY_PROMPT (13-field, ~9,520 tokens)
│   ├── analyze_recordings.py          ← Main pipeline: audio → Gemini → score
│   ├── enrich_results.py              ← Adds DB contact/reachability fields
│   └── process_single.py             ← One-off: process a single account
├── recordings/                        ← MP3 files named by loan_number (gitignored)
├── data/
│   ├── account_data.json              ← Raw DB data (gitignored — privacy)
│   └── propensity_results.json        ← Scored + ranked output (committed)
├── public/
│   └── data/
│       └── propensity_results.json    ← Copy served by Vite (same file)
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

---

## Gemini / Vertex AI Setup

**Model:** `gemini-2.5-flash`
**Provider:** Vertex AI (Google Cloud)
**Project:** `vertex-gemini-oto-cms`
**Location:** `us-central1`
**Credentials:** In `.env` as `GOOGLE_API_KEY` or falls back to Vertex AI service account

The prompt (`scripts/prompts.py`) is **self-contained** — it does NOT import from any external file. `BASE_DISPOSITION_PROMPT` + `PROPENSITY_EXTENSION` are both embedded directly in `prompts.py`.

---

## Scoring Formula — v2 (CURRENT, bias-corrected)

The v1 formula gave disposition 30% weight, causing a bias where "Agree To Senior Manager Call" always scored High regardless of genuine intent, and "Financial Hardship" always scored low regardless of engagement.

### v2 Weights

| Factor | Weight | Change from v1 | Source |
|--------|--------|----------------|--------|
| Commitment strength | **28%** | ↑ from 20% | LLM |
| Engagement level | **22%** | ↑ from 15% | LLM |
| Disposition | **10%** | ↓ from 30% | LLM |
| Sentiment | 10% | unchanged | LLM |
| History trend (last 8 calls) | 10% | unchanged | DB |
| DPD bucket | 10% | unchanged | DB |
| Call duration | 5% | unchanged | DB |

### Bonus Points
- `promise_made = true` → +5
- `customer_initiated_resolution = true` → +5
- `specific_amount_discussed = true` → +3
- `tone_shift = improved` → +5 / `worsened` → -3

### Cross-Signal Validation Rules (NEW in v2)

**Passive Yes Penalty:**
If `disposition = "Agree To Senior Manager Call"` AND `commitment_strength in (weak, none)` AND `engagement_level = low` → cap score at **60** (prevents passive yes-sayers from inflating to High tier)

**Engaged Hardship Boost:**
If `disposition = "Financial Hardship"` AND `engagement_level = high` AND `customer_initiated_resolution = true` → add **+10 bonus** (surfaces genuine hardship-but-willing customers)

### Disposition Score Map
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
```

### Tiers
- High: score ≥ 65
- Medium: score 40–64
- Low: score < 40

---

## Prompt Caching

Both explicit and implicit caching are implemented in `analyze_recordings.py`.

**Explicit (primary):**
- `create_prompt_cache(prompt_text)` creates a named cache before the batch
- Cached content referenced by name in each API call
- Cached tokens billed at **25%** of normal input price
- TTL: 2 hours
- `delete_prompt_cache(cache_name)` cleans up after batch

**Implicit (fallback):**
- If explicit caching fails, prompt is sent FIRST (before audio) so Gemini's automatic prefix-detection applies
- No setup required

**Token logging:** Every call logs `input_tokens`, `cached_tokens`, `output_tokens`, `total_tokens`. Accumulative summary printed at end with cost and savings.

**NOTE:** There is a minor bug in `delete_prompt_cache` — the delete API call uses wrong argument format. Fix:
```python
# Wrong:
client.caches.delete(cache_name)
# Correct:
client.caches.delete(name=cache_name)
```

---

## Dashboard (React App)

**Live URL:** https://arnavkhamparia-byte.github.io/fusion-finance-propensity/
**GitHub Repo:** https://github.com/arnavkhamparia-byte/fusion-finance-propensity
**Branch:** `master` → GitHub Actions builds → pushes to `gh-pages` branch → GitHub Pages serves `gh-pages`

**Tech stack:** React 18 + Vite 5 + Tailwind CSS v3 + Recharts + Lucide React + React Router v6 (HashRouter for GitHub Pages compatibility)

**Dashboard features:**
- 4 KPI cards (All / High / Medium / Low) — clickable as tier filters
- 3 Recharts charts: tier donut, score distribution bar, top dispositions horizontal bar
- Paginated table (10/page) with search + sort on any column
- Clicking any row → Account Detail page (`/account/:loanNumber`)

**Account Detail (4 tabs):**
- **Contact & Loan** — contacts (primary, secondary, WhatsApp, email, co-applicant, references), reachability flags, full loan metadata
- **AI Analysis** — summary, key reasons, barrier advice, 13-field AI signals, score breakdown bars
- **Call History** — total calls, previous dispositions list
- **Payment Info** — payment status, PTP amount, DPD, late installment trend chart (3M/6M/12M)

**To update dashboard after re-analysis:**
```bash
cd "/home/vk/Desktop/Propensity Score"
python3 scripts/analyze_recordings.py       # re-analyse
python3 scripts/enrich_results.py           # add DB fields
npm run build                               # build React app
git add -A && git commit -m "..." && git push origin master
# GitHub Actions auto-deploys to gh-pages
```

---

## How to Run the Full Pipeline (Current — Manual MP3 Files)

```bash
cd "/home/vk/Desktop/Propensity Score"

# Step 1: Fetch DB data for all recordings
python3 scripts/fetch_account_data.py

# Step 2: Analyse recordings + score
python3 scripts/analyze_recordings.py

# Step 3: Enrich with DB contact fields
python3 scripts/enrich_results.py

# Step 4: Build + deploy dashboard
npm run build
git add data/propensity_results.json public/data/propensity_results.json
git commit -m "update scores"
git push origin master
```

---

## PENDING TASKS FOR NEXT SESSION

These are fully analysed and ready to implement. Do them in this order.

---

### Task 1 — Richer Account-Level History Signals

**Problem:** The current `history_trend_score()` only looks at disposition labels from last 8 calls with recency weighting. It misses important trajectory signals.

**What to add to the scoring formula:**

**a) Contact Rate (reachability trend)**
Out of the last N calls, what % actually connected (had a real conversation) vs. were no-answer/busy/not-connected? Low contact rate = harder to reach = lower propensity all else equal.

**b) Disposition Trajectory**
Compare dispositions from last 3 calls vs. 3 before that. If trajectory is improving (negative → neutral → positive), boost score. If declining, penalise. Current formula treats all history the same regardless of direction.

**c) Consecutive Negative Streak**
If the last 4+ calls are all connection failures or "Refuse To Pay", discount the current call's positive disposition — it's likely a one-off.

**d) Days Since Last Positive Disposition**
Freshness matters. A positive signal from 10 days ago is more valuable than one from 3 months ago.

**e) Promise-to-Pay Break Rate**
Count previous PTPs from call history (calls where `promise_made = true`). Divide broken PTPs by total PTPs. If a customer has broken 3 promises before, discount the current promise heavily.

**f) Call Frequency / Over-dialing Flag**
If 20+ calls in 30 days with mostly no-answers, the customer may be blocking. Flag this and reduce score.

**Implementation approach:**
- Decompose `history_trend_score()` into 3 sub-functions: `contact_rate_score()`, `disposition_trajectory_score()`, `ptp_reliability_score()`
- Increase history weight from 10% to 15%, reduce disposition from 10% to 5%
- These sub-signals use the `call_history` list already fetched by `fetch_account_data.py` — no new DB queries needed

---

### Task 2 — S3 Presigned URL Pipeline (Eliminate Manual MP3 Downloads)

**Problem:** Currently, MP3 files must be manually downloaded and placed in `recordings/` folder. This is not scalable.

**Solution:** Stream audio directly from S3 using presigned URLs.

**The DB query to use (fetches accounts + recording URLs):**
```sql
SELECT
    ad.id,
    ad.loan_number,
    t.contact_number,
    ad.name,
    ad.city,
    ad.loan_amount,
    ad.dpd_bucket,
    t.disposition,
    t.call_recording_url,
    ad.emi_amount,
    ad.total_amount_pending,
    t.processed_at,
    t.summary,
    ad.assigned_to_id
FROM activity_taskactivity t
JOIN account_details ad ON ad.id = t.account_id
WHERE t.activity_type = 'AI Call'
AND t.disposition IN ('Agree To Senior Manager Call', 'Financial Hardship', 'Requested Settlement')
AND ad.assigned_to_id IN (50, 68)
AND t.processed_at >= CURRENT_DATE - INTERVAL '13 days';
```

**AWS credentials to add to `.env`:**
```
AWS_ACCESS_KEY_ID=<fill in>
AWS_SECRET_ACCESS_KEY=<fill in>
AWS_REGION_NAME=<fill in>
```

**The AWS helper class (provided by user):**
```python
import boto3

class AwsConnection:
    def __init__(self):
        self.session = boto3.session.Session(
            aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
            region_name=os.environ['AWS_REGION_NAME'],
        )

    def generate_pre_signed_url(self, key, bucket="ai-call-bucket", expiration=3600):
        s3_client = self.session.client("s3")
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expiration,
        )
```

**How to call it:**
```python
aws = AwsConnection()
presigned_url = aws.generate_pre_signed_url(key=call_recording_url)
# call_recording_url is the value from activity_taskactivity.call_recording_url
```

**New pipeline flow:**
```
DB Query (with call_recording_url column)
    → For each account:
        1. aws.generate_pre_signed_url(key=call_recording_url) → HTTPS URL
        2. requests.get(presigned_url) → audio bytes in memory (no disk write)
        3. Send audio bytes to Gemini (already accepts bytes — no change to Gemini call)
    → Score → Enrich → Dashboard
```

**Files to change:**
- `fetch_account_data.py` → run the new DB query, store `call_recording_url` per account in `account_data.json`
- `analyze_recordings.py` → replace `os.listdir(RECORDINGS_DIR)` loop with loop over accounts from DB. Download audio from S3 instead of reading local file. `RECORDINGS_DIR` becomes unused.
- Add `boto3` and `requests` to `requirements.txt`
- The `recordings/` folder and local MP3 step become completely optional/legacy

---

### Task 3 — Handle Recording Mismatch (Busy/Call Hung Up appearing after re-analysis)

**Problem:** The DB query fetches accounts where the stored disposition is positive (e.g. "Agree To Senior Manager Call"). But when Gemini re-analyses the recording, it sometimes returns a Stage 1 disposition (Busy, no-answer, Call Hang Up) because:
1. The `call_recording_url` points to the wrong recording (data integrity issue in source system)
2. The original disposition was set by a different/less accurate model
3. The recording is from a different call attempt than the one that was classified
4. The agent manually overrode the disposition in the CRM

This causes garbage dispositions to appear in the dashboard for accounts that should be High priority.

**Solution — Two layers:**

**Layer 1: Pre-flight duration check (before sending to Gemini)**
- From `call_history[0]['call_duration']` (already fetched from DB)
- If `call_duration < 20 seconds` → skip Gemini entirely
- Use the DB-stored disposition for scoring instead
- Log as `recording_skipped_short_duration`
- This saves cost and prevents wasted API calls

**Layer 2: Post-analysis mismatch detection (after Gemini returns)**
- After Gemini returns a Stage 1 disposition (Busy, no-answer, not-connected, Call Hang Up, Wrong Number, Failed) for an account the DB tagged as positive:
  - Set a flag `recording_mismatch: true` in the result record
  - Use the **DB disposition** (not Gemini's) for scoring
  - Log the mismatch: `MISMATCH: DB=Agree To Senior Manager Call, Gemini=Busy`
  - Still score the account using DB disposition so it doesn't disappear from the dashboard

**Dashboard change:**
- If `recording_mismatch = true`, show a small warning badge on the account row and detail page: "⚠ Recording mismatch — score based on DB disposition"
- This gives the collections team full transparency

**Implementation in `analyze_recordings.py`:**
```python
STAGE1_DISPOSITIONS = {
    "not-connected", "no-answer", "Busy", "Failed",
    "Wrong Number", "Call Hang Up", "Connected"
}

# After Gemini analysis:
db_disposition = call_history[0].get("disposition") if call_history else None
gemini_disposition = llm_output.get("disposition")
recording_mismatch = (
    gemini_disposition in STAGE1_DISPOSITIONS
    and db_disposition not in STAGE1_DISPOSITIONS
    and db_disposition is not None
)
if recording_mismatch:
    log.warning(f"  MISMATCH: DB={db_disposition}, Gemini={gemini_disposition} — using DB disposition")
    llm_output["disposition"] = db_disposition  # override for scoring
```

---

## Environment Variables (.env file)

The `.env` file at project root contains all credentials. Current keys:
```
# Database
DB_HOST=...
DB_PORT=5432
DB_NAME=fusion_finance_mfi
DB_USER=readonly
DB_PASS=readonly

# Google / Vertex AI
GOOGLE_API_KEY=...   (or uses Vertex AI service account)
GCP_PROJECT_ID=vertex-gemini-oto-cms
GCP_LOCATION=us-central1

# AWS S3 — TO BE ADDED for Task 2
AWS_ACCESS_KEY_ID=<fill in>
AWS_SECRET_ACCESS_KEY=<fill in>
AWS_REGION_NAME=<fill in>
```

---

## GitHub

**Repo:** `arnavkhamparia-byte/fusion-finance-propensity`
**Default branch:** `master` (source code)
**Pages branch:** `gh-pages` (built React app — auto-populated by GitHub Actions)
**Pages URL:** https://arnavkhamparia-byte.github.io/fusion-finance-propensity/

GitHub Actions workflow (`.github/workflows/deploy.yml`):
- Triggers on push to `master`
- Runs `npm ci` + `npm run build`
- Pushes `dist/` to `gh-pages` branch via `peaceiris/actions-gh-pages@v3`

---

## Known Bugs / Minor Issues

1. **Cache delete bug** in `analyze_recordings.py` → `delete_prompt_cache()`:
   ```python
   # Current (wrong):
   client.caches.delete(cache_name)
   # Fix:
   client.caches.delete(name=cache_name)
   ```
   Non-critical — cache expires automatically after 2 hours. Fix alongside Task 2.

2. **`propensity_results.json` has `token_usage` per account** — these fields should be excluded from the public dashboard data file (`public/data/`) for cleanliness. Can be filtered in `enrich_results.py`.

---

## Key Decisions Already Made (Do Not Revisit)

- **React + Vite + Tailwind** chosen over plain HTML (user chose "Path B")
- **HashRouter** used (required for GitHub Pages SPA routing)
- **No Action Queue** feature (explicitly removed by user)
- **Removed from UI:** Settlement amount, Savings if pay today, Interest per day, Bureau/CIBIL score, Late installments 3m/6m/12m (from KPI tier cards), Call recording URL, NACH status
- **Disposition weight reduced from 30% → 10%** (bias correction — do not revert)
- **Tier thresholds:** High ≥ 65, Medium ≥ 40, Low < 40 (keep as-is)
- **Prompt is self-contained in `prompts.py`** — does NOT import from external production agent files

---

## Node.js Compatibility Note

Node.js version on this machine: **v18.19.1**
`npm create vite` fails on Node 18 (requires Node 20+). `package.json` was written manually with pinned compatible versions. Do not try to scaffold with `create-vite` — just edit files directly.

---

*Handoff updated: 2026-05-18 — ready for new session*
