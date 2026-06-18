<p><a target="_blank" href="https://app.eraser.io/workspace/hsDlg3dpdZh3to4ZMfH7" id="edit-in-eraser-github-link"><img alt="Edit in Eraser" src="https://firebasestorage.googleapis.com/v0/b/second-petal-295822.appspot.com/o/images%2Fgithub%2FOpen%20in%20Eraser.svg?alt=media&token=968381c8-a7e7-472a-8ed6-4a6626da5501"></a></p>

# Forex Dashboard — DBtronics

A MetaTrader 5 (MT5) account monitoring system that fetches live trading account data, logs daily performance metrics to Google Sheets, and serves a web-based dashboard for visualisation.

> **Platform support:** MT5 only. MT4 support is not available at this time.

---

## Repository Structure

```
Forex_Dashboard_DBtronics/
├── API_Fetch_Data/
│   ├── api_metatrader5.py          # Original MT5 data fetcher (CSV-based)
│   └── api_metatrader5_updated.py  # Updated MT5 data fetcher (Google Sheets + SMS)
├── templates/
│   ├── index2.html                 # Main Flask dashboard template (active)
│   ├── index.html                  # Alternative JS-driven layout (inactive)
│   └── account.html                # Per-account detail view (inactive)
├── static/
│   └── styles.css                  # Dashboard table styling
├── TransactionHistory.py           # MT5 deal history exporter (Google Sheets)
├── TraderMetrics.py                # Trader performance metrics (Google Sheets, on-demand)
├── UI_flask.py                     # Flask web application
├── requirements.txt                # Python dependencies
├── .env                            # Twilio credentials and SMS recipients (excluded from git)
├── cron.log                        # Runtime log (auto-generated, excluded from git)
└── .gitignore
```

---

## Components

### 1. `API_Fetch_Data/api_metatrader5.py` — Original Data Fetcher

The original script that fetches MT5 account data and writes it to a local CSV file for the Flask dashboard to consume.

**How it works:**
- Reads MT5 login credentials (`Login`, `Password`, `Server`) from a local `api_credentials.csv` file stored in the NextCloud root directory (outside this repository for security)
- Loops through all accounts and logs into each one via the MT5 Python API
- Fetches account metrics: Balance, Equity, Margin, Free Margin, Floating PnL
- Retrieves server time via the EURUSD tick and converts to US/Mountain local time
- Calculates `% Difference Balance` and `% Difference Equity` using start-of-day values stored in the credentials file
- Writes all data to `api_web.csv` every 5 seconds in a continuous loop

**Trigger:** Runs continuously until manually stopped (`Ctrl+C`)

**Output:** `api_web.csv` — read by `UI_flask.py` for the dashboard

**Note:** This script is kept for reference. Active use has moved to `api_metatrader5_updated.py`.

---

### 2. `API_Fetch_Data/api_metatrader5_updated.py` — Updated Data Fetcher (Active)

The current production script. Replaces the local CSV credentials file with Google Sheets, writes daily performance data back to Google Sheets, sends SMS notifications via Twilio, and is designed to run on a scheduled basis via Windows Task Scheduler.

**How it works:**

Credentials are read from the **`Account`** sheet in the **`STS Database`** Google Spreadsheet. Column positions are resolved dynamically from the header row, so reordering columns in the sheet will not break the script. Only accounts with `Status = Active` are processed. Each account's `ID`, `Password`, `Server`, and `Type` are used.

For each active account, the script fetches the current `Balance` and `Equity` from MT5 and writes the result to the **`Acc_data`** sheet in the same spreadsheet.

The script is invoked with a `start` or `end` argument that determines what gets written:

| Argument | When to run | What it writes |
|----------|-------------|----------------|
| `start` | 4:00 PM MST daily | Appends a new row using **tomorrow's date** as the trading day date, with `StartdayBalance`, `StartdayEquity`, and a `Status` value |
| `end` | 3:00 PM MST next day | Finds **today's date** row and fills `EnddayBalance`, `EnddayEquity`, and **appends** an end status to the existing `Status` cell |

> **Trading day date logic:** The start run fires at 4 PM MST on Day 1 but belongs to Day 2's trading session. It therefore writes Day 2 (tomorrow) as the date. The end run fires at 3 PM MST on Day 2 and looks for Day 2 (today) — both runs align on the same date. This ensures the start and end rows always match correctly.
>
> **Timezone:** The Windows server runs on CST. All timestamps in SMS reports are explicitly converted to MST via `pytz` regardless of the system timezone.

**Edge cases handled:**

| Scenario | Behaviour |
|----------|-----------|
| Start runs twice on the same day | Detects existing row with real balance data → skips, appends `START: Duplicate` to Status |
| Start re-run after a failed login | Detects existing row with **blank** balance → fills in real values, sets `START: OK (retry)` |
| End runs and today's row is missing | Logs a warning and skips (start was likely missed) |
| End runs twice on the same day | Overwrites EnddayBalance/Equity with latest values; Status shows `END: Overwritten` |
| MT5 login fails (start run) | Retries up to 3× with 5s gap; if still failing, writes a partial row (balance blank) with `START: MT5 login failed` and resets MT5 connection |
| MT5 login fails (end run) | Retries up to 3×; if still failing, appends `END: MT5 login failed` to existing row's Status and resets MT5 connection |
| MT5 login fails for one account cascading to others | After exhausting retries, `mt5.shutdown()` + `mt5.initialize()` clears any stuck terminal state before the next account |
| `account_info()` returns None (start run) | Writes a partial row with `START: Account info error` |
| `account_info()` returns None (end run) | Appends `END: Account info error` to existing row's Status |

**Status column values:**

The `Status` column (column G) in `Acc_data` records what happened during each run. The start run writes the first value; the end run appends with ` | ` so both are visible in the same cell.

| Value | Run | Meaning |
|-------|-----|---------|
| `START: OK` | start | Balance & equity recorded successfully |
| `START: OK (retry)` | start | Partial row from a previous failed run was filled in on re-run |
| `START: MT5 login failed` | start | Could not authenticate with MT5 after 3 attempts |
| `START: Account info error` | start | Logged in but `account_info()` returned None |
| `START: Duplicate` | start | Row with real balance already existed; second start run skipped |
| `END: OK` | end | End-of-day values recorded successfully |
| `END: Overwritten` | end | End values already existed; overwritten with latest |
| `END: MT5 login failed` | end | Could not authenticate with MT5 after 3 attempts |
| `END: Account info error` | end | Logged in but `account_info()` returned None |
| `END: No start row` | end | No matching start row found; start run likely missed |

Example cell after a clean day: `START: OK | END: OK`

> **Setup note:** Add a `Status` header in column G of the `Acc_data` sheet. The script writes to column 7 by index so data lands correctly either way, but the header keeps the sheet readable.

**Usage:**
```bash
# Normal scheduled runs
python API_Fetch_Data/api_metatrader5_updated.py start
python API_Fetch_Data/api_metatrader5_updated.py end

# Targeted re-run for a single account that failed (--account)
# Processes only that account; all other accounts are untouched.
# Works for both start and end runs.
python API_Fetch_Data/api_metatrader5_updated.py start --account 89088177
python API_Fetch_Data/api_metatrader5_updated.py end   --account 89088177
```

> **Re-running after login failures:** If one or more accounts failed during a start run, simply re-run the script (with or without `--account`). Accounts that already have a complete row are skipped automatically. Accounts that have a partial row (blank balance from a previous login failure) are detected and filled in — no manual sheet edits required.

**Output:** `Acc_data` sheet in Google Sheets (`Date`, `Account-ID`, `StartdayBalance`, `StartdayEquity`, `EnddayBalance`, `EnddayEquity`, `Status`)

> **Analysis calculations use equity, not balance.** All period performance figures (1d/2d/7d/14d/30d), challenge target progress, funded status moves, and the real profit summary are derived from equity values to include unrealised P&L.
>
> **Deposits and withdrawals are excluded from the Real Profit Summary.** During the END run, the script fetches `DEAL_TYPE_BALANCE` deals (MT5 type 2) for the trading session window (4 PM yesterday → 3 PM today). These are subtracted from the equity delta before reporting so a large deposit does not inflate the profit figure and a withdrawal does not show as a loss. When any deposit or withdrawal occurred, a separate `Dep / Wd (session)` breakdown is appended at the bottom of the Real Profit Summary.

**Logging:** Every run appends to `cron.log` in the project root with timestamps, account-level results, and any warnings.

---

### SMS Notifications (Twilio)

After each run completes, SMS alerts are sent to all numbers defined in `.env` under `SMS_RECIPIENTS`. Recipients receive identical messages. Numbers can be added or removed from `.env` without any code changes.

#### START run — 1 SMS (run summary)

```
[Forex Dashboard] START Run Complete
Date: 1-Apr-26 | Time: 04:00 PM MST

Summary:
  Total accounts : 15
  Recorded       : 13
  Skipped        : 2

Skipped accounts:
  - 500561 (MT5 login failed)
  - 512345 (Start row already exists)
```

#### END run — 2 SMS (or more if analysis is large)

**Message 1: Run summary**
```
[Forex Dashboard] END Run Complete
Date: 1-Apr-26 | Time: 03:00 PM MST

Summary:
  Total accounts : 15
  Recorded       : 13
  Skipped        : 2

Skipped accounts:
  - 500561 (MT5 login failed)
  - 512345 (No start row found)
```

**Message 2: Daily analysis**

Shows multi-period equity performance for each account relative to its deposit size. All calculations use **equity** (not balance) to capture unrealised P&L.

Each account displays five time periods:

| Label | Period | Start equity sourced from |
|-------|--------|--------------------------|
| `1d` | Today only | Today's `StartdayEquity` |
| `2d` | Last 2 days | Yesterday's `StartdayEquity` |
| `7d` | Last 7 days | 6 days ago `StartdayEquity` |
| `14d` | Last 14 days | 13 days ago `StartdayEquity` |
| `30d` | Last 30 days | 29 days ago `StartdayEquity` |

If fewer days of data exist than requested, the earliest available row is used and annotated with `[Xd]` (e.g. `[5d]` = only 5 days available).

```
[Forex Dashboard] Daily Analysis
Date: 1-Apr-26

-- Challenge Progress --

  541202045 ($100,000)
  1d:  +2.20% (+0.00% -> +2.20%)
  2d:  +2.70% (-0.50% -> +2.20%)
  7d:  +3.40% (-1.20% -> +2.20%) [5d]
  14d: +3.40% (-1.20% -> +2.20%) [5d]
  30d: +3.40% (-1.20% -> +2.20%) [5d]
  Target: 27.5% of 8%

  541202046 ($50,000)
  1d:  +2.50% (-1.00% -> +1.50%)
  2d:  +3.10% (-1.60% -> +1.50%)
  7d:  +3.10% (-1.60% -> +1.50%) [3d]
  14d: +3.10% (-1.60% -> +1.50%) [3d]
  30d: +3.10% (-1.60% -> +1.50%) [3d]
  Target: 18.75% of 8%

-- Funded Status --

  541202047 ($200,000)
  1d:  +0.11% (+5.10% -> +5.21%)
  2d:  +0.39% (+4.82% -> +5.21%)
  7d:  +1.21% (+4.00% -> +5.21%) [6d]
  14d: +1.21% (+4.00% -> +5.21%) [6d]
  30d: +1.21% (+4.00% -> +5.21%) [6d]

-- Real Profit Summary --
  Funded     : +$440.00
  Live $     : +$120.50
  Live c(÷100): -$15.30
  ---
  Total: +$545.20

  Dep / Wd (session):
  Funded : Dep $10,000 | Wd $0
  Live $ : Dep $0 | Wd $500
  Live c : Dep $0 | Wd $0
  Net D/W: +$9,500
```

> The `Dep / Wd` section only appears when at least one deposit or withdrawal occurred during the session. On days with no balance operations it is omitted entirely to keep the SMS concise.

If the analysis exceeds Twilio's 1600 character limit, it is automatically split into numbered parts sent as separate SMS messages:

```
[Forex Dashboard] Daily Analysis (1/2)   ← challenge accounts
[Forex Dashboard] Daily Analysis (2/2)   ← funded accounts + profit summary
```

> **Notes:**
> - The per-account delta table SMS (`build_end_performance_sms`) is commented out — the multi-period analysis covers this in greater detail. The function is retained in code for potential future use.
> - The `[Xd]` annotation only appears when fewer days exist than the period label requests. A clean `7d` line with no annotation means a full 7 days of history was found.

---

### 3. `TransactionHistory.py` — Deal History Exporter

Exports MT5 deal history for all active accounts to Google Sheets (`STS Transaction History`). Runs daily at 3:15 PM MST — 15 minutes after the `end` run — so end-of-day balances and any final trades are captured.

**How it works:**

Credentials are read from the same `Account` sheet in `STS Database` (same dynamic column resolution, `Status = Active` filter) as `api_metatrader5_updated.py`. All active accounts are processed.

Each account gets its own tab in `STS Transaction History`, named by the MT5 account login number (e.g. `541202045`).

**Run modes determined by tab existence:**

| Condition | Behaviour |
|-----------|-----------|
| Tab not found (first run) | Creates tab, writes header, backfills last **30 days** of deals |
| Tab found (subsequent runs) | Fetches last **2 days** from MT5 (yesterday + today), deduplicates, appends new only |

**Deduplication logic (subsequent runs):**

Ticket number is MT5's unique primary key for deals — used as the sole deduplication key. A date-based window (last **3 days** of sheet rows) is scanned for existing tickets rather than a fixed row count, making it reliable regardless of trade frequency (scalper vs. swing trader).

```
MT5 fetch:  yesterday 00:00 → today 23:59  (2 days)
Sheet scan: last 3 days of rows → extract existing ticket numbers
Result:     only deals with unseen ticket numbers are appended
```

Fetching 2 days (instead of today only) ensures deals placed after the previous 3:15 PM run are captured on the next run.

**Data written per deal** (all deal types included — BALANCE and CREDIT are essential):

| Column | Description |
|--------|-------------|
| Date | Deal date (`YYYY.MM.DD`) |
| Account Number | MT5 login number |
| Ticket | Unique deal ID (deduplication key) |
| Position ID | Links entry and exit deals for the same trade |
| Symbol | Instrument traded (e.g. EURUSD) |
| Type | BUY, SELL, BALANCE, CREDIT |
| Entry/Exit | ENTRY, EXIT, REVERSAL, CLOSE_BY |
| Magic Number | EA identifier (0 = manual trade) |
| Manual Trade | YES if magic == 0, NO otherwise |
| Comment | MT5 deal comment |
| Open Time / Close Time | Timestamps for entry and exit |
| Duration (s) | Seconds position was open |
| Entry Price / Exit Price | Prices at open and close |
| SL / TP | Stop loss and take profit levels |
| Lot Size | Volume traded |
| Balance at Export | Account balance at time of script run |
| Equity at Export | Account equity at time of script run |
| Profit / Commission / Swap / Net Profit | Full P&L breakdown |

**Key constants (top of file):**

| Constant | Value | Purpose |
|----------|-------|---------|
| `HISTORY_DAYS` | 30 | Days backfilled on first run |
| `INCREMENTAL_DAYS` | 2 | Days fetched from MT5 on subsequent runs |
| `DEDUP_DAYS` | 3 | Days of sheet rows scanned for duplicate ticket check |

**Output:** `STS Transaction History` Google Spreadsheet — one tab per account number.

**Logging:** Appends to the same `cron.log` as `api_metatrader5_updated.py`.

> **Setup note:** Share the `STS Transaction History` spreadsheet with the same service account email from the JSON key file, just as you did for `STS Database`.

---

### 4. `TraderMetrics.py` — Trader Performance Metrics (On-demand)

Calculates statistical trading performance metrics per trader and writes them to the **`Trader Metrics`** tab in `STS Database`. Run manually whenever a fresh metrics snapshot is needed — not scheduled.

**How it works:**

1. Reads `Employee` and `Emp_Acc` sheets from `STS Database` to map account IDs to trader names
2. Reads `Account` sheet for deposit sizes and daily drawdown limits (used for probability calculations)
3. Reads every tab in `STS Transaction History` (one tab per account) and collects all closed trades — `EXIT`, `CLOSE_BY`, and `REVERSAL` deals of type `BUY` or `SELL` only. `BALANCE` and `CREDIT` deals are excluded
4. Groups trades by trader (multiple accounts merged into one row)
5. Calculates all metrics and writes a full clear-and-rewrite to `Trader Metrics`

**Update behaviour:** Every run clears and rewrites the entire sheet from scratch. This means:
- Accounts linked in `Emp_Acc` since the last run automatically move from `UNKNOWN TRADER` to the correct trader row
- New traders appear, removed traders disappear, all numbers reflect the latest deal history

**Metrics calculated (columns A–Z):**

| Column | Metric | What it captures |
|--------|--------|-----------------|
| A | Last Updated | Timestamp of this run |
| B | Trader | Employee name (or `UNKNOWN TRADER`) |
| C | Accounts | All account IDs contributing trades |
| D | Days of Data | Calendar span of the sample |
| E | Total Trades | Sample size |
| F | Sample Quality | `Sufficient` (≥100) or `Low (N)` |
| G | Win Rate % | % of closed trades in profit |
| H | Break-even WR % | Minimum WR needed to profit at this R:R |
| I | WR Margin % | How far above/below the break-even line |
| J | Profit Factor | Gross profit ÷ gross loss |
| K | Net P&L ($) | Total realised profit |
| L | Avg Win ($) | Average profit on winning trades |
| M | Avg Loss ($) | Average loss on losing trades (negative) |
| N | R:R | Avg Win ÷ Avg Loss |
| O | EV/Trade ($) | Expected profit per trade — core viability signal |
| P | Avg Trades/Day | Trade frequency |
| Q | Daily EV ($) | Expected profit per calendar day |
| R | Max Loss Streak | Longest consecutive losing run |
| S | Largest Win ($) | Biggest single trade (outlier check) |
| T | Largest Loss ($) | Biggest single loss (risk check) |
| U | Avg Duration (hrs) | Average hold time |
| V | Manual % | % of trades placed manually vs EA |
| W | Top Symbol | Most traded instrument |
| X | Proj. Days to 10% | Linear estimate of days to reach 10% profit target |
| Y | P(10% before ruin) % | Monte Carlo probability of hitting +10% before the ruin threshold |
| Z | Notes | `⚠` flag for unlinked accounts needing Emp_Acc assignment |

**Glossary (columns AB–AE):** Every metric has an accompanying row in the sheet explaining its calculation, what it means, and the green/red flag thresholds — so the sheet is self-documenting.

**`P(10% before ruin)` detail:**
Uses a Monte Carlo simulation (3,000 runs) where each simulated trade either wins `avg_win_pct` or loses `avg_loss_pct` of the account, with the trader's historical win rate as the probability. The simulation stops when equity reaches +10% (success) or hits the ruin threshold (failure). The ruin threshold defaults to −5% but is overridden by the account's `Daily Drawdown` limit from the `Account` sheet if available.

**`UNKNOWN TRADER` handling:**
Accounts with no entry in `Emp_Acc` are grouped into a single `UNKNOWN TRADER` row. The `Notes` column lists the specific account IDs that need to be assigned. Once linked in `Emp_Acc`, the next run automatically moves them to the correct trader row.

**Usage:**
```bash
python TraderMetrics.py
```

**Output:** `Trader Metrics` tab in `STS Database` Google Spreadsheet.

**Logging:** Appends to the same `cron.log` as the other scripts.

---

### 5. `UI_flask.py` — Flask Web Dashboard

A lightweight Flask application that serves a browser-based dashboard displaying live MT5 account data.

**How it works:**
- Exposes a single route `/` (home page)
- Reads `api_web.csv` (written by `api_metatrader5.py`) using pandas
- Normalises the `Type` column to lowercase for consistent filtering
- Renders `templates/index2.html` with the account data

**Dashboard layout (`index2.html`):**
- Auto-refreshes every 60 seconds
- Splits accounts into three sections based on account type:
  - **Challenge / Funded** — accounts of type `challenge` or `funded`
  - **Live** — accounts of type `live`
  - **Demo** — accounts of type `demo`

**Run:**
```bash
python UI_flask.py
```

**Note:** The Flask dashboard currently depends on `api_web.csv` which is written by the original `api_metatrader5.py` script. Integration with the updated Google Sheets pipeline is planned for a future update.

---

## Setup

### Prerequisites
- Python 3.x
- MetaTrader 5 terminal installed (**Windows only** — the MT5 Python API does not support macOS or Linux)
- A Google Cloud service account JSON key with access to the `STS Database` spreadsheet

### Install dependencies
```bash
pip install -r requirements.txt
```

### Google Sheets authentication
Place the service account JSON key file in the project root:
```
Forex_Dashboard_DBtronics/n8n-automation-dbtronics-49815df8eb82.json
```
This file is excluded from git via `.gitignore`. It must be manually copied to each machine.

### Twilio & SMS configuration
Fill in the `.env` file in the project root with your Twilio credentials:
```
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token_here
TWILIO_FROM_NUMBER=+1xxxxxxxxxx
SMS_RECIPIENTS=+1xxxxxxxxxx,+1xxxxxxxxxx
```
`.env` is excluded from git. Add or remove recipient numbers in `SMS_RECIPIENTS` at any time — no code changes required.

---

## Windows Task Scheduler Setup

Three scripts run automatically via Windows Task Scheduler:

| Task | Time | Command |
|------|------|---------|
| Forex Start Run | 4:00 PM MST daily | `python API_Fetch_Data\api_metatrader5_updated.py start` |
| Forex End Run | 3:00 PM MST daily | `python API_Fetch_Data\api_metatrader5_updated.py end` |
| Transaction History | 3:15 PM MST daily | `python TransactionHistory.py` |

Set the **Start in** directory to:
```
C:\Users\Administrator\Desktop\Forex_Dashboard_DBtronics
```

---

## Logging

Each run of `api_metatrader5_updated.py` appends to `cron.log` in the project root. Example output:

```
2026-04-01 16:00:01 [INFO] ────────────────────────────────────────────────────────────
2026-04-01 16:00:01 [INFO] Run type : START
2026-04-01 16:00:01 [INFO] Active accounts found: 15
2026-04-01 16:00:01 [INFO] ────────────────────────────────────────────────────────────
2026-04-01 16:00:03 [INFO] Account: 541202045 | Balance: 105220.2 | Equity: 105220.2
2026-04-01 16:00:04 [INFO]   [START] New row written → 541202045 | Date: 1-Apr-26 | StartdayBalance=105220.2, StartdayEquity=105220.2
2026-04-01 16:00:05 [WARNING]   MT5 login failed for 500561 — skipping
2026-04-01 16:00:45 [INFO] Run completed successfully.
```

`cron.log` is excluded from git and lives only on the machine running the scheduler.

---

## Future Work
- Integrate Flask dashboard with Google Sheets data (replacing `api_web.csv` dependency)
- Add MT4 support via REST API

---

<!-- eraser-additional-content -->
## Diagrams
<!-- eraser-additional-files -->
<a href="/README-VPS and Raspberry Pi Data Handling Architecture Through Python API-1.eraserdiagram" data-element-id="VT9-m8e6-uXLCvG5jylwi"><img src="/.eraser/hsDlg3dpdZh3to4ZMfH7___RjBDyi3vteXAY5KNDoWEt0Ma2Iv2___---diagram----d1b6c072dedf9ef1439f9f74451b9ccf-VPS-and-Raspberry-Pi-Data-Handling-Architecture-Through-Python-API.png" alt="" data-element-id="VT9-m8e6-uXLCvG5jylwi" /></a>
<a href="/README-Python MT4/MT5 Data Fetch and Display System Through REST API-2.eraserdiagram" data-element-id="H4jkXr1aAxjYSMTXJsDcE"><img src="/.eraser/hsDlg3dpdZh3to4ZMfH7___RjBDyi3vteXAY5KNDoWEt0Ma2Iv2___---diagram----5afbad68e8b1128f7782649a4e613b6e-Python-MT4-MT5-Data-Fetch-and-Display-System-Through-REST-API.png" alt="" data-element-id="H4jkXr1aAxjYSMTXJsDcE" /></a>
<!-- end-eraser-additional-files -->
<!-- end-eraser-additional-content -->
<!--- Eraser file: https://app.eraser.io/workspace/hsDlg3dpdZh3to4ZMfH7 --->
