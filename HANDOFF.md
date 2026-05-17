# Propensity Score Project — Session Handoff
**Created:** 2026-05-16
**Session closed by:** User (switching to PyCharm terminal)

---

## What This Project Does

Analyses AI call recordings of loan recovery accounts (Fusion Finance MFI) and predicts the probability of each customer making a payment. Customers are ranked 1–N by payment likelihood. Results are presented on a hosted GitHub Pages dashboard.

---

## Current Status

| Step | Status |
|---|---|
| Step 1 — Fetch DB data for 30 accounts | DONE |
| Step 2 — Gemini audio analysis + scoring | DONE (29/30 accounts) |
| Step 3 — Dashboard UI | NOT STARTED (waiting for design input) |
| Step 4 — GitHub Pages deployment | NOT STARTED |

**One recording skipped:** Loan `44658864972` — this loan number was not found in the DB. Likely a typo in the filename (11 digits, all others are 10). Verify and rename if needed.

---

## Project Folder Structure

```
/home/vk/Desktop/Propensity Score/
├── PLAN.md                        ← Full original plan document
├── HANDOFF.md                     ← This file
├── .env                           ← Credentials (NOT committed to git)
├── .gitignore                     ← Excludes .env, recordings/, __pycache__
├── requirements.txt               ← Python dependencies
├── scripts/
│   ├── fetch_account_data.py      ← Step 1: DB → data/account_data.json
│   ├── prompts.py                 ← Extended Gemini prompt (13-field output)
│   └── analyze_recordings.py     ← Step 2: Audio → Gemini → Score → ranked JSON
├── recordings/                    ← 30 MP3 files (named by loan_number)
│   └── *.mp3
└── data/
    ├── account_data.json          ← DB data for 29 accounts (gitignored)
    └── propensity_results.json    ← Final scored + ranked output (committed to git)
```

---

## Database

**Type:** PostgreSQL (AWS RDS)
**Host:** `otolmsstagedbinstance.cttxlpcdrmsq.ap-south-1.rds.amazonaws.com`
**Port:** `5432`
**Database:** `fusion_finance_mfi`
**User/Pass:** `readonly` / `readonly`
**Access:** Read-only. No write permissions.

**Key tables used:**
- `account_details` — loan info, DPD bucket, amounts
- `activity_taskactivity` — AI call history, dispositions, summaries
- `account_payments` — payment history

**Credentials also in:** `.env` file in the project folder.

---

## Gemini / Vertex AI Setup

**Provider:** Vertex AI (Google Cloud)
**Model:** `gemini-2.5-flash`
**Project:** `vertex-gemini-oto-cms`
**Location:** `us-central1`
**Credentials file:** `/home/vk/Downloads/gemini_live_pipecat/server/abc.json`

The Vertex AI client is identical to the one used in the existing production agent at:
`/home/vk/PycharmProjects/OTO-Servers/server_og/temp_disposition_agent/disposition_agent.py`

The prompt in `scripts/prompts.py` imports the base `DISPOSITION_PROMPT` from:
`/home/vk/PycharmProjects/OTO-Servers/server_og/temp_disposition_agent/prompt.py`
...and appends the `PROPENSITY_EXTENSION` (8 new fields) to it.

---

## How to Run the Backend Scripts

```bash
cd "/home/vk/Desktop/Propensity Score"

# Install dependencies (only needed once)
pip3 install -r requirements.txt --break-system-packages

# Step 1: Fetch DB data (re-run only if recordings change)
python3 scripts/fetch_account_data.py

# Step 2: Analyse recordings and generate scores
python3 scripts/analyze_recordings.py
```

---

## Gemini Prompt — What It Extracts

The prompt outputs **13 JSON fields** per recording:

**Original 5 (disposition analysis):**
1. `disposition` — one of 14 disposition values (e.g., Financial Hardship, Agree To Senior Manager Call)
2. `callback_date` — date for Call Back Requested disposition
3. `callback_time` — time for Call Back Requested disposition
4. `summary` — 300-char English summary of the call
5. `sentiment` — positive / neutral / negative

**New 8 (propensity signals):**
6. `commitment_strength` — strong / moderate / weak / none
7. `promise_made` — true/false (did customer promise a specific payment)
8. `promise_date` — date if promise made
9. `barrier_type` — financial / avoidance / dispute / hardship / none
10. `engagement_level` — high / medium / low
11. `customer_initiated_resolution` — true/false
12. `tone_shift` — improved / neutral / worsened
13. `specific_amount_discussed` — true/false

---

## Scoring Formula

Produces a **0–100 propensity score** per account.

| Factor | Weight | Source |
|---|---|---|
| Disposition | 30% | LLM |
| Commitment strength | 20% | LLM |
| Engagement level | 15% | LLM |
| Sentiment | 10% | LLM |
| History trend (last 8 calls) | 10% | DB |
| DPD bucket | 10% | DB |
| Call duration | 5% | DB |

**Bonus points:**
- Promise made → +5
- Customer initiated resolution → +5
- Specific amount discussed → +3
- Tone shift improved → +5 / worsened → -3

**Tiers:**
- High: 65–100
- Medium: 40–64
- Low: 0–39

---

## Analysis Results Summary (29 accounts)

| Tier | Count |
|---|---|
| High | 2 |
| Medium | 10 |
| Low | 17 |

**Top ranked accounts (High tier):**
- Loan `7244867266` — Munni Jahan — Score: 72.7 (Connected, strong commitment)
- Loan `3134149764` — Guddi Bai — Score: 69.7 (Call Back Requested, high engagement)

**Notable observations:**
- Almost all 29 accounts are in "SPOD - 70% Waiver" DPD bucket (very overdue)
- Only 4 accounts are in 91-120 or 151-180 DPD (those scored higher)
- Most recordings resulted in "Financial Hardship" disposition
- 0 payments recorded for any of these 30 accounts in DB

**Full results at:** `data/propensity_results.json`

---

## Next Step — Dashboard (PENDING DESIGN DISCUSSION)

The dashboard will be:
- Pure static HTML/CSS/JS (no backend server)
- Hosted on **GitHub Pages** (shareable link)
- Reads from `data/propensity_results.json`

**Before building the dashboard, the user needs to be asked:**
1. Color scheme / brand colors (Fusion Finance colors?)
2. Should it show all accounts on one page or paginated?
3. Priority columns to show in the main table
4. Should clicking a row expand account history inline, or open a side panel?
5. Should there be a print/export PDF option in addition to Excel?
6. Should the dashboard show the call summary text prominently?
7. Dark mode or light mode preference?
8. Any specific chart types preferred (bar, donut, gauge, etc.)?

**The new Claude session should ask these questions FIRST before writing any HTML.**

---

## Loan Number Issue

Loan number `44658864972` in the recordings folder was **not found in the DB**.
All other loan numbers have 10 digits. This one has 11.
Possible causes: typo in filename, wrong recording downloaded, or test account.
Action: Check and rename or remove the file before re-running analysis.

---

## GitHub Repo (Not Yet Created)

The dashboard will be pushed to a new GitHub repository.
The `.gitignore` already excludes:
- `.env` (credentials)
- `recordings/` (audio files — privacy)
- `data/account_data.json` (raw DB data — privacy)

What WILL be committed:
- All scripts
- `data/propensity_results.json` (anonymised scoring output — needed by dashboard)
- Dashboard HTML/CSS/JS
- `requirements.txt`, `PLAN.md`

---

## Key File Paths

| Purpose | Path |
|---|---|
| Project root | `/home/vk/Desktop/Propensity Score/` |
| Env file | `/home/vk/Desktop/Propensity Score/.env` |
| Scored results | `/home/vk/Desktop/Propensity Score/data/propensity_results.json` |
| Account DB data | `/home/vk/Desktop/Propensity Score/data/account_data.json` |
| Existing production agent | `/home/vk/PycharmProjects/OTO-Servers/server_og/temp_disposition_agent/` |
| Vertex AI credentials | `/home/vk/Downloads/gemini_live_pipecat/server/abc.json` |
| Base disposition prompt | `/home/vk/PycharmProjects/OTO-Servers/server_og/temp_disposition_agent/prompt.py` |
