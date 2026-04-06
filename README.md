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
| Start runs twice on the same day | Skips second run; appends `START: Duplicate` to the existing row's Status |
| End runs and today's row is missing | Logs a warning and skips (start was likely missed) |
| End runs twice on the same day | Overwrites EnddayBalance/Equity with latest values; Status shows `END: Overwritten` |
| MT5 login fails (start run) | Writes a partial row (balance/equity blank) with `START: MT5 login failed` |
| MT5 login fails (end run) | Finds the existing start row and appends `END: MT5 login failed` to Status |
| `account_info()` returns None (start run) | Writes a partial row with `START: Account info error` |
| `account_info()` returns None (end run) | Appends `END: Account info error` to existing row's Status |

**Status column values:**

The `Status` column (column G) in `Acc_data` records what happened during each run. The start run writes the first value; the end run appends with ` | ` so both are visible in the same cell.

| Value | Run | Meaning |
|-------|-----|---------|
| `START: OK` | start | Balance & equity recorded successfully |
| `START: MT5 login failed` | start | Could not authenticate with MT5 |
| `START: Account info error` | start | Logged in but `account_info()` returned None |
| `START: Duplicate` | start | Row already existed; second start run skipped |
| `END: OK` | end | End-of-day values recorded successfully |
| `END: Overwritten` | end | End values already existed; overwritten with latest |
| `END: MT5 login failed` | end | Could not authenticate with MT5 |
| `END: Account info error` | end | Logged in but `account_info()` returned None |
| `END: No start row` | end | No matching start row found; start run likely missed |

Example cell after a clean day: `START: OK | END: OK`

> **Setup note:** Add a `Status` header in column G of the `Acc_data` sheet. The script writes to column 7 by index so data lands correctly either way, but the header keeps the sheet readable.

**Usage:**
```bash
python API_Fetch_Data/api_metatrader5_updated.py start
python API_Fetch_Data/api_metatrader5_updated.py end
```

**Output:** `Acc_data` sheet in Google Sheets (`Date`, `Account-ID`, `StartdayBalance`, `StartdayEquity`, `EnddayBalance`, `EnddayEquity`, `Status`)

> **Analysis calculations use equity, not balance.** All period performance figures (1d/2d/7d/14d/30d), challenge target progress, funded status moves, and the real profit summary are derived from equity values to include unrealised P&L.

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
```

If the analysis exceeds Twilio's 1600 character limit, it is automatically split into numbered parts sent as separate SMS messages:

```
[Forex Dashboard] Daily Analysis (1/2)   ← challenge accounts
[Forex Dashboard] Daily Analysis (2/2)   ← funded accounts + profit summary
```

> **Notes:**
> - The per-account delta table SMS (`build_end_performance_sms`) is commented out — the multi-period analysis covers this in greater detail. The function is retained in code for potential future use.
> - The `[Xd]` annotation only appears when fewer days exist than the period label requests. A clean `7d` line with no annotation means a full 7 days of history was found.

---

### 3. `UI_flask.py` — Flask Web Dashboard

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

To run the updated script automatically twice a day:

| Task | Time | Command |
|------|------|---------|
| Forex Start Run | 4:00 PM daily | `python API_Fetch_Data\api_metatrader5_updated.py start` |
| Forex End Run | 3:00 PM daily | `python API_Fetch_Data\api_metatrader5_updated.py end` |

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
- Add deposit and withdrawal history tracking via `mt5.history_deals_get()`

---

<!-- eraser-additional-content -->
## Diagrams
<!-- eraser-additional-files -->
<a href="/README-VPS and Raspberry Pi Data Handling Architecture Through Python API-1.eraserdiagram" data-element-id="VT9-m8e6-uXLCvG5jylwi"><img src="/.eraser/hsDlg3dpdZh3to4ZMfH7___RjBDyi3vteXAY5KNDoWEt0Ma2Iv2___---diagram----d1b6c072dedf9ef1439f9f74451b9ccf-VPS-and-Raspberry-Pi-Data-Handling-Architecture-Through-Python-API.png" alt="" data-element-id="VT9-m8e6-uXLCvG5jylwi" /></a>
<a href="/README-Python MT4/MT5 Data Fetch and Display System Through REST API-2.eraserdiagram" data-element-id="H4jkXr1aAxjYSMTXJsDcE"><img src="/.eraser/hsDlg3dpdZh3to4ZMfH7___RjBDyi3vteXAY5KNDoWEt0Ma2Iv2___---diagram----5afbad68e8b1128f7782649a4e613b6e-Python-MT4-MT5-Data-Fetch-and-Display-System-Through-REST-API.png" alt="" data-element-id="H4jkXr1aAxjYSMTXJsDcE" /></a>
<!-- end-eraser-additional-files -->
<!-- end-eraser-additional-content -->
<!--- Eraser file: https://app.eraser.io/workspace/hsDlg3dpdZh3to4ZMfH7 --->
