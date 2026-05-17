# Propensity Score System — Execution Plan

## Objective
Analyze 30 AI call recordings of loan recovery accounts, predict payment probability for each customer, rank them by likelihood to pay, and present results on a hosted dashboard accessible via a GitHub Pages link.

---

## Folder Structure (Final)

```
Propensity Score/
├── PLAN.md                          ← This file
├── .env                             ← Vertex AI credentials (not committed to git)
├── requirements.txt                 ← Python dependencies
├── .gitignore                       ← Excludes .env, recordings/, __pycache__
├── scripts/
│   ├── fetch_account_data.py        ← Step 1: Pull account history from DB
│   ├── prompts.py                   ← Extended Gemini prompt
│   └── analyze_recordings.py        ← Step 2: Gemini analysis + scoring engine
├── recordings/                      ← 30 MP3 files (NOT committed to git)
├── data/
│   ├── account_data.json            ← DB output for 30 accounts
│   └── propensity_results.json      ← Final scored + ranked results (committed)
└── dashboard/
    ├── index.html                   ← Main dashboard page
    ├── style.css                    ← Styling
    └── app.js                       ← Charts, filters, table logic
```

---

## Step-by-Step Execution Plan

---

### STEP 1 — Fetch Account Data from DB (`fetch_account_data.py`)

**What it does:**
- Reads all 30 loan numbers from filenames in `recordings/`
- Connects to `fusion_finance_mfi` PostgreSQL DB
- For each loan number, pulls:
  - From `account_details`: name, city, loan_amount, dpd_bucket, emi_amount, total_amount_pending, assigned_to_id
  - From `activity_taskactivity`: last 10 AI Call entries (disposition, sentiment, summary, call_duration, processed_at)
  - From `account_payments`: payment history (any payments made, last payment date, amount)
- Saves combined data to `data/account_data.json`

**Why account history matters:**
- An account that previously had "Agree To Senior Manager Call" twice is stronger signal
- Recent payment activity changes the score significantly
- Multiple "Refuse To Pay" in history lowers propensity even if latest call was positive

---

### STEP 2 — Extend the Gemini Prompt (`prompts.py`)

**What changes from your existing prompt:**
- Keep the full existing disposition logic (unchanged)
- Add a NEW section at the end that extracts propensity-specific signals:

```
New output fields added to JSON:
- commitment_strength:         "strong" | "moderate" | "weak" | "none"
- promise_made:                true | false
- promise_date:                "YYYY-MM-DD" | null
- barrier_type:                "financial" | "avoidance" | "dispute" | "hardship" | "none"
- engagement_level:            "high" | "medium" | "low"
- customer_initiated_resolution: true | false
- tone_shift:                  "improved" | "neutral" | "worsened"
  (did customer's tone change positively or negatively during the call?)
- specific_amount_discussed:   true | false
  (did customer mention a specific amount they can pay?)
```

**Total output: 12 fields** (5 original + 7 new)

---

### STEP 3 — Analyze Recordings + Score (`analyze_recordings.py`)

**What it does:**
1. Loads `data/account_data.json` (DB data for all 30 accounts)
2. For each MP3 in `recordings/`:
   - Reads audio file
   - Sends to Gemini 2.5 Flash via Vertex AI (reusing existing client setup)
   - Gets 12-field JSON response
   - Runs scoring formula (see below)
   - Saves full result
3. Outputs `data/propensity_results.json` with all 30 accounts scored and ranked

**Scoring Formula:**

| Factor                        | Weight | Source       | Logic |
|-------------------------------|--------|--------------|-------|
| Disposition                   | 30%    | LLM          | Agree To SM Call=1.0, Requested Settlement=0.85, Financial Hardship=0.3 |
| Commitment Strength           | 20%    | LLM          | strong=1.0, moderate=0.6, weak=0.3, none=0.0 |
| Engagement Level              | 15%    | LLM          | high=1.0, medium=0.6, low=0.2 |
| Sentiment                     | 10%    | LLM          | positive=1.0, neutral=0.5, negative=0.1 |
| Historical Call Trend         | 10%    | DB history   | Multiple positive dispositions in history = higher score |
| DPD Bucket Risk               | 10%    | DB           | Lower DPD = higher score (SPOD bucket = low, 150+ = very low) |
| Call Duration                 | 5%     | DB           | <30s = low signal, 60-180s = optimal |

**Bonus signals (add to final score):**
- `promise_made = true` → +5 points
- `customer_initiated_resolution = true` → +5 points
- `specific_amount_discussed = true` → +3 points
- `tone_shift = improved` → +3 points

**Final score: 0–100**

**Priority Tiers:**
- High: 70–100
- Medium: 40–69
- Low: 0–39

---

### STEP 4 — Build the Dashboard (`dashboard/`)

**Hosted on GitHub Pages** — pure static HTML/CSS/JS, no server needed.
Data is embedded from `data/propensity_results.json`.

**Dashboard Sections:**

#### A. Summary Bar (top)
- Total accounts analysed
- High / Medium / Low tier counts
- Average propensity score

#### B. Ranked Accounts Table (main view)
Columns: Rank | Name | Loan Number | City | DPD Bucket | Loan Amount | Total Pending | Disposition | Score | Tier | Key Signal

- Sortable by any column
- Color-coded tier badges (Green=High, Yellow=Medium, Red=Low)
- Search/filter by name, city, disposition, tier

#### C. Charts (sidebar/below table)
- Bar chart: Score distribution across 30 accounts
- Donut chart: Disposition breakdown
- Bar chart: Tier breakdown (High/Medium/Low counts)

#### D. Account Detail Panel (click any row)
Expands to show full account history:
- All previous AI call dispositions (timeline)
- Payment history
- LLM analysis breakdown (all 12 fields)
- Scoring breakdown (which factors contributed what)

#### E. Export Button
- Download results as Excel file directly from browser

---

### STEP 5 — GitHub Pages Deployment

1. Initialize a git repo inside `Propensity Score/`
2. Create `.gitignore` to exclude: `.env`, `recordings/`, `__pycache__`
3. Commit: scripts, dashboard, data/propensity_results.json, requirements.txt
4. Push to a new GitHub repository
5. Enable GitHub Pages → Source: `dashboard/` folder or root `/docs`
6. Share the GitHub Pages URL — anyone with the link can view the dashboard

**Note:** The recordings (MP3 files) and `.env` are never committed — data privacy protected.

---

## Credentials Setup

Copy the following into `Propensity Score/.env`:

```
GOOGLE_APPLICATION_CREDENTIALS=/home/vk/Downloads/gemini_live_pipecat/server/abc.json
GCP_PROJECT_ID=vertex-gemini-oto-cms
GCP_LOCATION=us-central1

DB_HOST=otolmsstagedbinstance.cttxlpcdrmsq.ap-south-1.rds.amazonaws.com
DB_PORT=5432
DB_NAME=fusion_finance_mfi
DB_USER=readonly
DB_PASS=readonly
```

---

## Dependencies (`requirements.txt`)

```
google-genai>=1.57.0
asyncpg
python-dotenv
pandas
openpyxl
```

---

## Execution Order (Once Approved)

```
1. Create .env
2. pip install -r requirements.txt
3. python scripts/fetch_account_data.py     ← pulls DB data for 30 accounts
4. python scripts/analyze_recordings.py     ← sends audio to Gemini, scores, ranks
5. open dashboard/index.html                ← review locally
6. git init → push to GitHub → enable Pages
7. Share link
```

---

## What You Review After Step 4
- Does the ranking match your intuition about these 30 accounts?
- Are the scores calibrated correctly?
- Any accounts that feel over/under-scored?

Your feedback → we tune the weights → scale to all 4,555 accounts.
