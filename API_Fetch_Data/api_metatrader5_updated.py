# pip install MetaTrader5 gspread google-auth twilio python-dotenv
#
# USAGE:
#   python api_metatrader5_updated.py start   ← run at 4:00 PM MST (Windows Task Scheduler)
#   python api_metatrader5_updated.py end     ← run at 3:00 PM MST next day (Windows Task Scheduler)
#
# HOW IT WORKS:
#   This script runs twice daily via Windows Task Scheduler.
#
#   START run (4 PM MST):
#     - Reads all Active accounts from the "Account" sheet in Google Sheets
#     - Logs into each MT5 account and fetches current balance & equity
#     - Appends a new row to "Acc_data" sheet with today's date,
#       Account ID, StartdayBalance, and StartdayEquity
#     - EnddayBalance and EnddayEquity are left blank to be filled later
#     - Sends an SMS summarising how many accounts were recorded vs skipped
#
#   END run (3 PM MST next calendar day):
#     - Reads all Active accounts from the "Account" sheet
#     - Logs into each MT5 account and fetches current balance & equity
#     - Looks for yesterday's row in "Acc_data" for each account
#       (yesterday because 4 PM start → 3 PM end = different calendar dates in MST)
#     - If found and incomplete → fills EnddayBalance and EnddayEquity
#     - If found and already complete → overwrites with latest values (logs a warning)
#     - If not found at all → logs a warning and skips (start run likely missed)
#     - Sends two SMS messages:
#         1. Run summary (total / recorded / skipped)
#         2. Daily analysis (challenge progress, funded status, real profit summary)

import os
import sys
import logging
from datetime import datetime, timedelta
import time
import pytz
import MetaTrader5 as mt5
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from twilio.rest import Client as TwilioClient

# Initialize MT5 IPC connection with retry at startup.
# mt5.initialize() can fail silently (returns False) even when the terminal
# is visibly open — all login() calls will then return -10004.
_MT5_INIT_RETRIES = 3
_MT5_INIT_DELAY   = 5
_mt5_initialized  = False
for _attempt in range(1, _MT5_INIT_RETRIES + 1):
    if mt5.initialize():
        _mt5_initialized = True
        break
    print(f"MT5 initialize() attempt {_attempt}/{_MT5_INIT_RETRIES} failed: {mt5.last_error()}")
    if _attempt < _MT5_INIT_RETRIES:
        time.sleep(_MT5_INIT_DELAY)

if not _mt5_initialized:
    print("MT5 terminal not responding to Python API after 3 attempts. "
          "Check that MT5 terminal is open and Python API is enabled in its settings.")
    sys.exit(1)

# Wait for the terminal to have an active account session.
# mt5.initialize() opens the IPC pipe but the terminal needs a base account
# logged in before mt5.login() switching works. Configure MT5 auto-login
# (File → Login → Save account info) to avoid this on unattended restarts.
_terminal_ready = False
for _ in range(12):   # wait up to 60 seconds
    _info = mt5.terminal_info()
    if _info and _info.connected:
        _terminal_ready = True
        break
    time.sleep(5)

if not _terminal_ready:
    print("MT5 terminal connected via IPC but has no active account session. "
          "Configure MT5 auto-login so it reconnects automatically on startup.")
    sys.exit(1)

# ── Load environment variables from .env ─────────────────────────────────────
# .env lives in the project root (one level up from this script).
# It contains Twilio credentials and SMS recipient numbers.
# It is excluded from git via .gitignore.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '..', '.env'))

# ── Google Sheets configuration ──────────────────────────────────────────────
# Service account JSON key file — stored in project root, excluded from git via .gitignore
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, '..', 'n8n-automation-dbtronics-49815df8eb82.json')

SPREADSHEET_NAME = 'STS Database'  # Google Sheets file name
ACCOUNT_SHEET    = 'Account'        # Sheet containing MT5 login credentials
ACC_DATA_SHEET   = 'Acc_data'       # Sheet where daily balance/equity data is written
EMP_ACC_SHEET    = 'Emp_Acc'        # Sheet mapping employee IDs to account IDs
EMPLOYEE_SHEET   = 'Employee'       # Sheet containing employee names

# Scopes required: read + write access to Sheets and Drive
SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']

# ── Twilio configuration ──────────────────────────────────────────────────────
# All values loaded from .env file — never hardcoded here.
TWILIO_ACCOUNT_SID  = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN   = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_FROM_NUMBER  = os.getenv('TWILIO_FROM_NUMBER')

# SMS_RECIPIENTS is a comma-separated list in .env, e.g. +1xxxxxxxxxx,+1xxxxxxxxxx
# Parsed into a list here — add or remove numbers in .env without touching code.
SMS_RECIPIENTS = [n.strip() for n in os.getenv('SMS_RECIPIENTS', '').split(',') if n.strip()]

# # api_web.csv — commented out, kept for future Flask dashboard use
# import pandas as pd
# CSV_OUTPUT_PATH = os.path.join(SCRIPT_DIR, '..', 'api_web.csv')

# ── Logging configuration ─────────────────────────────────────────────────────
# Log file lives in the project root folder (one level up from this script).
# It is excluded from git via *.log in .gitignore.
# Each run appends to the same file so the full history is preserved.
LOG_FILE = os.path.join(SCRIPT_DIR, '..', 'cron.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),  # write to cron.log
        logging.StreamHandler(sys.stdout)                 # also print to terminal
    ]
)

def log(msg):
    """Shorthand for logging.info — used throughout the script."""
    logging.info(msg)

def log_warn(msg):
    """Shorthand for logging.warning."""
    logging.warning(msg)
# ─────────────────────────────────────────────────────────────────────────────


def get_gsheet_client():
    """Authenticate with Google Sheets using the service account key file."""
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def get_credentials_from_sheet(client):
    """
    Read MT5 login credentials from the Account sheet.
    Only returns accounts where Status column = 'Active'.

    Column positions are resolved dynamically from the header row so that
    reordering or adding columns in the sheet does not break the script.
    Required columns: ID, Password, Server, Status.
    """
    ws   = client.open(SPREADSHEET_NAME).worksheet(ACCOUNT_SHEET)
    rows = ws.get_all_values()

    if not rows:
        log_warn("Account sheet is empty.")
        return []

    # Build a column index map from the header row
    headers = [h.strip() for h in rows[0]]
    col = {name: idx for idx, name in enumerate(headers)}
    log(f"  Account sheet columns: {headers}")

    # Validate required columns exist
    required = ['ID', 'Password', 'Server', 'Status']
    missing  = [c for c in required if c not in col]
    if missing:
        log_warn(f"  Missing required columns in Account sheet: {missing}. Cannot proceed.")
        return []

    def safe_col(row, name):
        """Return stripped cell value for a column name, or '' if missing."""
        return row[col[name]].strip() if name in col and len(row) > col[name] else ''

    credentials = []
    for row in rows[1:]:        # skip header row
        if not row[col['ID']].strip():  # skip empty rows
            continue

        status = safe_col(row, 'Status')

        if status != 'Active':
            # Inactive, closed, or blank status accounts are ignored
            log(f"  Skipping account {row[col['ID']].strip()} (Status: '{status}')")
            continue

        credentials.append({
            'ID':            row[col['ID']].strip(),
            'Password':      row[col['Password']].strip(),
            'Server':        row[col['Server']].strip(),
            # Type: '$' or 'Cent$' — determines currency denomination of the account
            'Type':          safe_col(row, 'Type'),
            # Category: 'Funded', 'Challenge', or 'LIVE' — determines account class
            'Category':      safe_col(row, 'Category'),
            # Deposit/Size: account size in USD e.g. '$100,000' — base for % calculations
            'DepositSize':   parse_float(safe_col(row, 'Deposit/Size')),
            # Daily Drawdown: max daily loss allowed e.g. '5.00%'
            'DailyDrawdown': parse_percent(safe_col(row, 'Daily Drawdown')),
            # Profit Target: challenge completion target e.g. '8.00%' — blank for Funded
            'ProfitTarget':  parse_percent(safe_col(row, 'Profit Target')),
        })

    return credentials


def get_emp_acc_map(client):
    """
    Return a dict mapping account_id_str -> employee_name by joining
    the Employee and Emp_Acc sheets. Accounts with no employee entry
    will not appear in the dict — callers should default to 'UNKNOWN TRADER'.
    """
    db = client.open(SPREADSHEET_NAME)

    emp_rows = db.worksheet(EMPLOYEE_SHEET).get_all_values()
    emp_id_to_name = {r[0].strip(): r[1].strip() for r in emp_rows[1:] if len(r) >= 2 and r[0].strip()}

    emp_acc_rows = db.worksheet(EMP_ACC_SHEET).get_all_values()
    acc_to_emp = {}
    for r in emp_acc_rows[1:]:
        if len(r) >= 2 and r[0].strip() and r[1].strip():
            emp_id = r[0].strip()
            acc_id = r[1].strip()
            acc_to_emp[acc_id] = emp_id_to_name.get(emp_id, 'UNKNOWN TRADER')

    return acc_to_emp


def parse_percent(value):
    """
    Safely parse a percentage string from Google Sheets into a float.
    Strips the '%' character before converting.

    Examples:
      '5.00%'  → 5.0
      '8%'     → 8.0
      ''       → None
    """
    if not value:
        return None
    try:
        return float(str(value).replace('%', '').strip())
    except ValueError:
        log_warn(f"  Could not parse value as percent: '{value}' — treating as None")
        return None


def parse_float(value):
    """
    Safely parse a value from Google Sheets into a float.
    Handles plain numbers, currency-formatted strings, and empty values.

    Examples handled:
      '105220.2'     → 105220.2
      '$105,220.20'  → 105220.2
      '105,220.20'   → 105220.2
      ''             → None
      None           → None
    """
    if not value:
        return None
    try:
        # Strip currency symbols, spaces, and thousand separators then convert
        cleaned = str(value).replace('$', '').replace(',', '').strip()
        return float(cleaned)
    except ValueError:
        log_warn(f"  Could not parse value as float: '{value}' — treating as None")
        return None


def parse_sheet_date(date_str):
    """
    Parse a date value returned by gspread get_all_values() into a date object.

    Google Sheets may return dates in multiple formats depending on the cell's
    number format and whether the value was stored as a date serial or text:
      '2-Jun-26'   → d-mmm-yy   (abbreviated month)
      '2-June-26'  → d-mmmm-yy  (full month name — Google Sheets returns this for June)
      '6/2/2026'   → M/d/yyyy   (US locale default)
      '6/2/26'     → M/d/yy
      '2026-06-02' → ISO
      '45810'      → date serial (days since 1899-12-30)

    Returns a datetime.date or None on failure.
    """
    s = str(date_str).strip()
    if not s:
        return None
    for fmt in ('%d-%b-%y', '%d-%B-%y', '%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Google Sheets numeric date serial (e.g. "45810" or "45810.0")
    try:
        serial = float(s)
        if serial > 1:  # guard against accidentally parsing a small number
            return (datetime(1899, 12, 30) + timedelta(days=int(serial))).date()
    except (ValueError, TypeError):
        pass
    return None


def get_date_str(date):
    """
    Format a date object into the Acc_data sheet format, e.g. '1-Apr-26'.
    Uses %#d on Windows (no leading zero on day).
    Written with value_input_option='USER_ENTERED' so Google Sheets stores
    it as a real date value rather than plain text.
    """
    return date.strftime('%#d-%b-%y')


def get_mst_time():
    """
    Returns the current time converted to MST (US/Mountain).
    The Windows server runs on CST — this ensures SMS timestamps
    always reflect MST regardless of the system timezone.
    """
    mst = pytz.timezone('US/Mountain')
    return datetime.now(mst)


def send_sms(body):
    """
    Send an SMS to all numbers in SMS_RECIPIENTS using Twilio.
    Each recipient receives the same message.
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, SMS_RECIPIENTS]):
        log_warn("Twilio config incomplete — SMS not sent. Check .env file.")
        return

    twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    for number in SMS_RECIPIENTS:
        try:
            twilio.messages.create(to=number, from_=TWILIO_FROM_NUMBER, body=body)
            log(f"  SMS sent to {number}")
        except Exception as e:
            log_warn(f"  Failed to send SMS to {number}: {e}")


def append_status_to_row(acc_data_ws, acc_data_rows, trading_date, account_id_str, status_text):
    """
    Find the Acc_data row matching trading_date + account_id and append status_text
    to the Status column (column 7). If the cell already has content (e.g. a START
    status), the new text is appended with ' | ' so both runs are visible.

    Called for end-run error cases where handle_end_run is never reached,
    so we still want to record that the end run failed for this account.
    """
    target_date = parse_sheet_date(trading_date)
    for i, row in enumerate(acc_data_rows[1:], start=2):
        if parse_sheet_date(row[0]) == target_date and str(row[1]).strip() == account_id_str:
            existing = row[6].strip() if len(row) > 6 else ''
            new_status = f"{existing} | {status_text}" if existing else status_text
            acc_data_ws.update_cell(i, 7, new_status)
            log(f"  Status updated for {account_id_str}: '{new_status}'")
            return
    log_warn(f"  No row found for {account_id_str} on {trading_date} — cannot update Status column.")


def get_period_start_equity(acc_data_rows, account_id_str, today_date_obj, n_days):
    """
    Look up the StartdayEquity for an account from n_days ago.

    For a period of n_days, the target start date is today - (n_days - 1).
      e.g. n_days=2  → yesterday
           n_days=7  → 6 days ago
           n_days=14 → 13 days ago
           n_days=30 → 29 days ago

    If the exact target date is not found in Acc_data (account didn't exist yet
    or that day's data is missing), falls back to the earliest available row for
    that account and reports the actual number of days that row covers.

    Returns:
      (equity_value, actual_days)  — actual_days < n_days signals a [Xd] annotation
      (None, 0)                    — no history exists at all for this account
    """
    target_date = (today_date_obj - timedelta(days=n_days - 1)).date()

    # Collect all rows for this account that have a parseable date and start equity.
    # Dates in Acc_data are written as '6-Apr-26' by get_date_str().
    account_rows = []
    for row in acc_data_rows[1:]:
        if str(row[1]).strip() != account_id_str:
            continue
        try:
            row_date = parse_sheet_date(row[0])
            if row_date is None:
                continue
            equity   = parse_float(row[3])   # StartdayEquity is column index 3 (0-based)
            if equity is not None:
                account_rows.append((row_date, equity))
        except (ValueError, IndexError):
            continue

    if not account_rows:
        return None, 0

    account_rows.sort(key=lambda x: x[0])

    # Try to find the exact target date first
    for row_date, equity in account_rows:
        if row_date == target_date:
            return equity, n_days

    # Exact date not found — use the earliest available row and report actual coverage
    earliest_date, earliest_equity = account_rows[0]
    actual_days = (today_date_obj.date() - earliest_date).days + 1
    return earliest_equity, actual_days


def build_start_sms(run_date, results, emp_map=None):
    """
    Build the SMS message for a START run.

    Content:
      - Run date and time
      - Total / recorded / skipped counts
      - List of skipped accounts with reasons
    """
    emp_map  = emp_map or {}
    total    = len(results)
    recorded = [r for r in results if r['status'] == 'recorded']
    skipped  = [r for r in results if r['status'] == 'skipped']

    lines = [
        "[Forex Dashboard] START Run Complete",
        f"Date: {run_date} | Time: {get_mst_time().strftime('%I:%M %p')} MST",
        "",
        "Summary:",
        f"  Total accounts : {total}",
        f"  Recorded       : {len(recorded)}",
        f"  Skipped        : {len(skipped)}",
    ]

    if skipped:
        lines.append("")
        lines.append("Skipped accounts:")
        for r in skipped:
            trader = emp_map.get(str(r['id']), 'UNKNOWN TRADER')
            lines.append(f"  - {r['id']} | {trader} ({r['reason']})")

    return "\n".join(lines)


def build_end_summary_sms(run_date, results, emp_map=None):
    """
    Build SMS message 1 of 2 for an END run — the run summary.

    Content:
      - Run date and time
      - Total / recorded / skipped counts
      - List of skipped accounts with reasons
    """
    emp_map  = emp_map or {}
    total    = len(results)
    recorded = [r for r in results if r['status'] in ('recorded', 'overwritten')]
    skipped  = [r for r in results if r['status'] == 'skipped']

    lines = [
        "[Forex Dashboard] END Run Complete",
        f"Date: {run_date} | Time: {get_mst_time().strftime('%I:%M %p')} MST",
        "",
        "Summary:",
        f"  Total accounts : {total}",
        f"  Recorded       : {len(recorded)}",
        f"  Skipped        : {len(skipped)}",
    ]

    if skipped:
        lines.append("")
        lines.append("Skipped accounts:")
        for r in skipped:
            trader = emp_map.get(str(r['id']), 'UNKNOWN TRADER')
            lines.append(f"  - {r['id']} | {trader} ({r['reason']})")

    return "\n".join(lines)


def fmt_delta(value, is_cent=False):
    """
    Format a balance/equity delta value for display.
    - Dollar accounts: prefix with + or - and $ symbol  e.g. +$220.20
    - Cent accounts:   suffix with c                    e.g. +1230.00c
    """
    sign = '+' if value >= 0 else '-'
    abs_val = abs(value)
    if is_cent:
        return f"{sign}{abs_val:,.2f}c"
    return f"{sign}${abs_val:,.2f}"


def build_end_performance_sms(run_date, results, emp_map=None):
    """
    Build SMS message 2 of 3 for an END run — per-account delta table only.
    Kept separate to stay within Twilio's 1600 character limit.
    """
    emp_map  = emp_map or {}
    recorded = [r for r in results if r['status'] in ('recorded', 'overwritten')]

    lines = [
        "[Forex Dashboard] Account Deltas",
        f"Date: {run_date}",
        "",
        f"{'Account':<15} {'Trader':<20} {'Cat':<10} {'Bal Delta':>11} {'Eq Delta':>11}",
        f"{'-'*15} {'-'*20} {'-'*10} {'-'*11} {'-'*11}",
    ]

    for r in recorded:
        is_cent   = r['type'] == 'Cent$'
        bal_delta = r['end_balance'] - r['start_balance']
        eq_delta  = r['end_equity']  - r['start_equity']
        trader    = emp_map.get(str(r['id']), 'UNKNOWN TRADER')[:20]
        lines.append(
            f"{r['id']:<15} {trader:<20} {r['category']:<10} "
            f"{fmt_delta(bal_delta, is_cent):>11} {fmt_delta(eq_delta, is_cent):>11}"
        )

    return "\n".join(lines)


def build_end_analysis_sms(run_date, results, acc_data_rows, emp_map=None):
    """
    Build SMS message(s) for an END run — multi-period analysis per account
    + real profit summary. Returns a LIST of strings so each part stays within
    Twilio's 1600 character limit. Caller sends each part as a separate SMS.

    Per-account format:
      net: today's net % gain  (start% -> end%, both relative to deposit size)
      2d / 7d / 14d / 30d: net % gain over the period (start% -> end%)
        [Xd] annotation shown when fewer days of data exist than requested

    Sections:
      1. Challenge Progress — net + multi-period + profit target
      2. Funded Status     — net + multi-period (no target)
      3. Real Profit Summary — Funded + LIVE$ + LIVE Cent$ (÷100)
    """
    MAX_CHARS = 1580   # leave a little buffer for the (X/Y) part suffix
    PERIODS   = [(2, '2d'), (7, '7d'), (14, '14d'), (30, '30d')]

    emp_map  = emp_map or {}
    recorded = [r for r in results if r['status'] in ('recorded', 'overwritten')]

    # Parse run_date string back to a date object for period arithmetic
    try:
        today_date_obj = datetime.strptime(run_date, '%d-%b-%y')
    except ValueError:
        today_date_obj = get_mst_time().replace(tzinfo=None)

    def pct_str(v):
        """Format a percentage value with explicit +/- sign."""
        return f"{'+' if v >= 0 else ''}{v:.2f}%"

    def account_lines(r):
        """Build the line block for a single account."""
        size           = r['deposit_size']
        account_id_str = r['id']
        trader         = emp_map.get(str(account_id_str), 'UNKNOWN TRADER')
        blk            = [""]   # blank separator before each account block

        if not size:
            blk.append(f"  {account_id_str} | {trader} - size not available")
            return blk

        blk.append(f"  {account_id_str} | {trader} (${size:,.0f})")

        end_pct   = ((r['end_equity']   - size) / size) * 100
        start_pct = ((r['start_equity'] - size) / size) * 100
        net_pct   = end_pct - start_pct
        end_str   = pct_str(end_pct)

        # 1d line — today's start → end
        blk.append(f"  {'1d:':<5}{pct_str(net_pct)} ({pct_str(start_pct)} -> {end_str})")

        # Multi-period lines — look up historical StartdayEquity from Acc_data
        for n_days, label in PERIODS:
            eq, actual = get_period_start_equity(
                acc_data_rows, account_id_str, today_date_obj, n_days
            )
            if eq is None:
                blk.append(f"  {label+':':<5}no data")
                continue
            period_start_pct = ((eq - size) / size) * 100
            period_net_pct   = end_pct - period_start_pct
            annotation       = f" [{actual}d]" if actual < n_days else ""
            blk.append(
                f"  {label+':':<5}{pct_str(period_net_pct)} "
                f"({pct_str(period_start_pct)} -> {end_str}){annotation}"
            )

        # Profit target progress (challenge accounts only)
        if r['category'] == 'Challenge' and r.get('profit_target') and r['profit_target'] > 0:
            progress = (end_pct / r['profit_target']) * 100
            blk.append(f"  Target: {progress:.1f}% of {r['profit_target']:.0f}%")

        return blk

    # ── Build content as a list of segments ───────────────────────────────────
    # Each segment is a list of lines. Segments are packed greedily into SMS
    # parts — a segment is never split across two parts.
    segments = []

    challenges = [r for r in recorded if r['category'] == 'Challenge']
    if challenges:
        segments.append(["", "-- Challenge Progress --"])
        for r in challenges:
            segments.append(account_lines(r))

    funded = [r for r in recorded if r['category'] == 'Funded']
    if funded:
        segments.append(["", "-- Funded Status --"])
        for r in funded:
            segments.append(account_lines(r))

    # ── Real Profit Summary ───────────────────────────────────────────────────
    # Only Funded, LIVE $, and LIVE Cent$ count toward real profit.
    # Equity is used (not balance) to capture unrealised P&L.
    # Deposits and withdrawals (DEAL_TYPE_BALANCE) are stripped from the equity
    # delta before summing so they don't distort the trading P&L figure.
    # Cent$ values are divided by 100 to convert to USD.
    def trading_pnl(r):
        raw    = r['end_equity'] - r['start_equity']
        net_dw = r.get('today_deposits', 0) + r.get('today_withdrawals', 0)
        return raw - net_dw

    funded_profit   = sum(trading_pnl(r) for r in recorded if r['category'] == 'Funded')
    live_dollar     = sum(trading_pnl(r) for r in recorded if r['category'] == 'LIVE' and r['type'] == '$')
    live_cent_raw   = sum(trading_pnl(r) for r in recorded if r['category'] == 'LIVE' and r['type'] == 'Cent$')
    live_cent_usd   = live_cent_raw / 100
    total_real      = funded_profit + live_dollar + live_cent_usd

    # Collect deposits/withdrawals per category for the informational section
    funded_dep      = sum(r.get('today_deposits', 0)    for r in recorded if r['category'] == 'Funded')
    funded_wd       = sum(r.get('today_withdrawals', 0) for r in recorded if r['category'] == 'Funded')
    live_dep        = sum(r.get('today_deposits', 0)    for r in recorded if r['category'] == 'LIVE' and r['type'] == '$')
    live_wd         = sum(r.get('today_withdrawals', 0) for r in recorded if r['category'] == 'LIVE' and r['type'] == '$')
    cent_dep_raw    = sum(r.get('today_deposits', 0)    for r in recorded if r['category'] == 'LIVE' and r['type'] == 'Cent$')
    cent_wd_raw     = sum(r.get('today_withdrawals', 0) for r in recorded if r['category'] == 'LIVE' and r['type'] == 'Cent$')
    cent_dep_usd    = cent_dep_raw / 100
    cent_wd_usd     = cent_wd_raw  / 100
    total_dep       = funded_dep + live_dep + cent_dep_usd
    total_wd        = funded_wd  + live_wd  + cent_wd_usd

    summary_seg = [
        "",
        "-- Real Profit Summary --",
        f"  Funded     : {fmt_delta(funded_profit)}",
        f"  Live $     : {fmt_delta(live_dollar)}",
        f"  Live c(÷100): {fmt_delta(live_cent_usd)}",
        f"  ---",
        f"  Total: {fmt_delta(total_real)}",
    ]

    # Only append the Dep/Wd section when something actually moved
    if any([funded_dep, funded_wd, live_dep, live_wd, cent_dep_raw, cent_wd_raw]):
        def fmt_dw(dep, wd):
            return f"Dep ${abs(dep):,.0f} | Wd ${abs(wd):,.0f}"
        summary_seg += [
            "",
            "  Dep / Wd (session):",
            f"  Funded : {fmt_dw(funded_dep, funded_wd)}",
            f"  Live $ : {fmt_dw(live_dep, live_wd)}",
            f"  Live c : {fmt_dw(cent_dep_usd, cent_wd_usd)}",
            f"  Net D/W: {fmt_delta(total_dep + total_wd)}",
        ]

    segments.append(summary_seg)

    # ── Pack segments into SMS parts (greedy, max MAX_CHARS each) ────────────
    base_header = f"[Forex Dashboard] Daily Analysis\nDate: {run_date}"

    # Try to fit everything into a single SMS first
    all_lines = [base_header]
    for seg in segments:
        all_lines.extend(seg)
    if len("\n".join(all_lines)) <= MAX_CHARS:
        return ["\n".join(all_lines)]

    # Doesn't fit — greedily pack: flush current part when next segment won't fit
    parts         = []
    current_lines = [base_header]

    for seg in segments:
        candidate = current_lines + seg
        if len("\n".join(candidate)) <= MAX_CHARS:
            current_lines = candidate
        else:
            # Flush the current part (only if it has content beyond the bare header)
            if len(current_lines) > 1:
                parts.append("\n".join(current_lines))
            # Start a new part with this segment
            current_lines = [base_header] + seg

    if len(current_lines) > 1:
        parts.append("\n".join(current_lines))

    # Add (X/N) numbering to each part's header line
    if len(parts) > 1:
        total = len(parts)
        parts = [
            p.replace(
                "[Forex Dashboard] Daily Analysis",
                f"[Forex Dashboard] Daily Analysis ({i+1}/{total})",
                1   # replace only the first occurrence
            )
            for i, p in enumerate(parts)
        ]

    return parts if parts else [base_header]


def handle_start_run(acc_data_ws, acc_data_rows, account_id, account_type, account_category,
                     deposit_size, daily_drawdown, profit_target, balance, equity):
    """
    START run logic — called when script is invoked with 'start' argument.

    Appends a new row to Acc_data with:
      - Trading date (tomorrow in MST — the session being opened)
      - Account ID
      - StartdayBalance and StartdayEquity (current MT5 values)
      - EnddayBalance and EnddayEquity left blank (filled by end run)
      - Status: 'START: OK'

    If a row for tomorrow + this account already exists (start ran twice),
    it logs a warning and skips to prevent duplicate rows. The existing
    row's Status is left unchanged.

    Returns a result dict used for SMS reporting.

    Possible Status values written by this function:
      'START: OK'        — balance & equity recorded successfully
      'START: Duplicate' — row already existed; this run was skipped
    """
    # Start run at 4 PM MST opens the NEXT calendar day's trading session.
    # e.g. start runs on Apr 1 at 4 PM → trading day is Apr 2.
    # End run on Apr 2 at 3 PM will look for today's date (Apr 2) to match.
    trading_date   = get_date_str(datetime.now() + timedelta(days=1))
    account_id_str = str(account_id)

    # Check if a row already exists for this trading date + account.
    target_date = parse_sheet_date(trading_date)
    for i, row in enumerate(acc_data_rows[1:], start=2):
        if parse_sheet_date(row[0]) == target_date and str(row[1]).strip() == account_id_str:
            existing_balance = parse_float(row[2]) if len(row) > 2 else None

            if existing_balance is None:
                # Row exists but balance is empty — written by a previously failed login.
                # Fill in the real values now instead of skipping.
                log(f"  [START] Partial row found for {account_id_str} on {trading_date} "
                    f"(previous run had no balance). Filling in now.")
                acc_data_ws.update_cell(i, 3, balance)
                acc_data_ws.update_cell(i, 4, equity)
                acc_data_ws.update_cell(i, 7, 'START: OK (retry)')
                log(f"  [START] Retry done → {account_id_str} | "
                    f"StartdayBalance={balance}, StartdayEquity={equity}")
                return {'id': account_id_str, 'type': account_type, 'category': account_category,
                        'deposit_size': deposit_size, 'daily_drawdown': daily_drawdown,
                        'profit_target': profit_target, 'status': 'recorded'}

            # Row has real balance data — genuine duplicate start run.
            log_warn(f"  [START] Row already exists for {account_id_str} on {trading_date}. "
                     f"Start run may have been triggered twice. Skipping.")
            existing = row[6].strip() if len(row) > 6 else ''
            new_status = f"{existing} | START: Duplicate" if existing else 'START: Duplicate'
            acc_data_ws.update_cell(i, 7, new_status)
            return {'id': account_id_str, 'type': account_type, 'category': account_category,
                    'status': 'skipped', 'reason': 'Start row already exists'}

    # No existing row found — safe to append.
    # USER_ENTERED tells Google Sheets to parse values as if typed by a user,
    # so '1-Apr-26' is stored as a real date rather than plain text.
    # Column 7 (Status) is set to 'START: OK' to confirm a clean start recording.
    acc_data_ws.append_row(
        [trading_date, account_id_str, balance, equity, '', '', 'START: OK'],
        value_input_option='USER_ENTERED'
    )
    log(f"  [START] New row written → {account_id_str} | Date: {trading_date} | "
        f"StartdayBalance={balance}, StartdayEquity={equity}")

    return {'id': account_id_str, 'type': account_type, 'category': account_category,
            'deposit_size': deposit_size, 'daily_drawdown': daily_drawdown,
            'profit_target': profit_target, 'status': 'recorded'}


def handle_end_run(acc_data_ws, acc_data_rows, account_id, account_type, account_category,
                   deposit_size, daily_drawdown, profit_target, balance, equity,
                   today_deposits=0.0, today_withdrawals=0.0):
    """
    END run logic — called when script is invoked with 'end' argument.

    Looks for yesterday's row in Acc_data for this account.
    Yesterday is used because:
      - Start runs at 4 PM MST (Day 1)
      - End runs at 3 PM MST (Day 2 — next calendar day in MST)

    Cases handled:
      1. Row found, EnddayBalance is empty   → fill EnddayBalance & EnddayEquity ✓
      2. Row found, EnddayBalance is filled  → overwrite with latest values + warn ✓
      3. No row found for yesterday          → log warning, skip (start likely missed) ✓

    Returns a result dict used for SMS reporting.
    """
    # End run at 3 PM MST on Apr 2 closes the trading day that started on Apr 1 at 4 PM.
    # The start run wrote tomorrow's date (Apr 2), so end run looks for today's date (Apr 2).
    trading_date   = get_date_str(datetime.now())
    account_id_str = str(account_id)

    # Search Acc_data for a row matching today's trading date and this account ID
    row_index = None
    start_balance = None
    start_equity  = None

    target_date = parse_sheet_date(trading_date)
    for i, row in enumerate(acc_data_rows[1:], start=2):  # gspread rows are 1-indexed; skip header
        if parse_sheet_date(row[0]) == target_date and str(row[1]).strip() == account_id_str:
            row_index     = i
            start_balance = parse_float(row[2])
            start_equity  = parse_float(row[3])
            break

    if row_index is None:
        # Case 3: No start row found for yesterday — start run was likely missed
        log_warn(f"  [END] No start row found for {account_id_str} on {trading_date}. "
                 f"Start run may have been missed. Skipping.")
        return {'id': account_id_str, 'type': account_type, 'category': account_category,
                'status': 'skipped', 'reason': 'No start row found'}

    # Check if EnddayBalance is already filled (column index 4, 0-based)
    # Use parse_float to handle any currency formatting Google Sheets may apply
    existing_endday = parse_float(acc_data_rows[row_index - 1][4])

    if existing_endday:
        # Case 2: End already recorded — overwrite with latest values
        log_warn(f"  [END] EnddayBalance already exists for {account_id_str} on {trading_date}. "
                 f"Overwriting with latest values.")
        status     = 'overwritten'
        end_status = 'END: Overwritten'
    else:
        # Case 1: Normal end run — fill in end-of-day values
        log(f"  [END] Row found for {account_id_str} on {trading_date} — filling end-of-day values.")
        status     = 'recorded'
        end_status = 'END: OK'

    # Write EnddayBalance (col 5) and EnddayEquity (col 6)
    acc_data_ws.update_cell(row_index, 5, balance)
    acc_data_ws.update_cell(row_index, 6, equity)

    # Append end status to the Status column (col 7) — preserve the start run status already there
    existing_status = acc_data_rows[row_index - 1][6].strip() if len(acc_data_rows[row_index - 1]) > 6 else ''
    new_status = f"{existing_status} | {end_status}" if existing_status else end_status
    acc_data_ws.update_cell(row_index, 7, new_status)

    log(f"  [END] Done → {account_id_str} | EnddayBalance={balance}, EnddayEquity={equity} | Status: '{new_status}'")

    return {
        'id':            account_id_str,
        'type':          account_type,
        'category':      account_category,
        'deposit_size':  deposit_size,
        'daily_drawdown': daily_drawdown,
        'profit_target': profit_target,
        'status':        status,
        'start_balance': start_balance if start_balance is not None else balance,
        'start_equity':  start_equity  if start_equity  is not None else equity,
        'end_balance':   balance,
        'end_equity':    equity,
        'today_deposits':    today_deposits,
        'today_withdrawals': today_withdrawals,
    }


def fetch_account_info(run_type, single_account=None):
    """
    Main function — connects to Google Sheets, loops through all Active MT5 accounts,
    fetches balance & equity, writes to Acc_data, then sends SMS notification.

    run_type:       'start' or 'end'
    single_account: optional account ID string — when provided, only that account is
                    processed (used for targeted re-runs after a login failure).
    """
    client = get_gsheet_client()

    # Read credentials, Acc_data, and employee-account mapping once per run
    credentials   = get_credentials_from_sheet(client)

    if single_account:
        credentials = [c for c in credentials if c['ID'] == single_account]
        if not credentials:
            log_warn(f"Account {single_account} not found or not Active in Account sheet. Exiting.")
            return
        log(f"Single-account mode: processing only account {single_account}")
    emp_map       = get_emp_acc_map(client)
    db            = client.open(SPREADSHEET_NAME)
    acc_data_ws   = db.worksheet(ACC_DATA_SHEET)
    acc_data_rows = acc_data_ws.get_all_values()

    # run_date is used in SMS reports.
    # For start: show tomorrow's date (the trading day being opened).
    # For end: show today's date (the trading day being closed).
    mst_now  = get_mst_time()
    run_date = get_date_str(mst_now + timedelta(days=1)) if run_type == 'start' else get_date_str(mst_now)

    log("─" * 60)
    log(f"Run type : {run_type.upper()}")
    log(f"Active accounts found: {len(credentials)}")
    log("─" * 60)

    results = []  # collects outcome for each account, used for SMS reporting

    MAX_LOGIN_RETRIES = 3
    RETRY_DELAY_SEC   = 5

    for cred in credentials:
        # Login to MT5 — retry up to MAX_LOGIN_RETRIES times with a short delay.
        # mt5.login() is flaky under broker load / rapid account switching.
        success = False
        for attempt in range(1, MAX_LOGIN_RETRIES + 1):
            success = mt5.login(int(cred['ID']), cred['Password'], cred['Server'])
            if success:
                break
            log_warn(f"  MT5 login attempt {attempt}/{MAX_LOGIN_RETRIES} failed for {cred['ID']}: {mt5.last_error()}")
            if attempt < MAX_LOGIN_RETRIES:
                time.sleep(RETRY_DELAY_SEC)

        if not success:
            log_warn(f"  MT5 login failed for {cred['ID']} after {MAX_LOGIN_RETRIES} attempts — skipping")
            # Reset the MT5 terminal connection so the stuck state from this failed
            # login does not cascade and cause all subsequent accounts to fail too.
            mt5.shutdown()
            time.sleep(2)
            if not mt5.initialize():
                log_warn(f"  MT5 re-initialize() failed after reset: {mt5.last_error()}")
            results.append({'id': cred['ID'], 'type': cred['Type'], 'category': cred['Category'],
                            'status': 'skipped', 'reason': 'MT5 login failed'})
            # Record the error in Acc_data so it's visible in the sheet:
            #   START run → write a partial row (balance/equity blank) with error status
            #   END run   → find existing row written by start run and append error status
            if run_type == 'start':
                trading_date = get_date_str(datetime.now() + timedelta(days=1))
                acc_data_ws.append_row(
                    [trading_date, str(cred['ID']), '', '', '', '', 'START: MT5 login failed'],
                    value_input_option='USER_ENTERED'
                )
                log(f"  Partial error row written for {cred['ID']} on {trading_date}.")
            else:
                append_status_to_row(acc_data_ws, acc_data_rows, run_date, str(cred['ID']),
                                     'END: MT5 login failed')
            continue

        accountInfo = mt5.account_info()
        if accountInfo is None:
            log_warn(f"  Could not retrieve account info for {cred['ID']} — skipping")
            results.append({'id': cred['ID'], 'type': cred['Type'], 'category': cred['Category'],
                            'status': 'skipped', 'reason': 'Could not retrieve account info'})
            # Same pattern — record error in sheet for visibility
            if run_type == 'start':
                trading_date = get_date_str(datetime.now() + timedelta(days=1))
                acc_data_ws.append_row(
                    [trading_date, str(cred['ID']), '', '', '', '', 'START: Account info error'],
                    value_input_option='USER_ENTERED'
                )
                log(f"  Partial error row written for {cred['ID']} on {trading_date}.")
            else:
                append_status_to_row(acc_data_ws, acc_data_rows, run_date, str(cred['ID']),
                                     'END: Account info error')
            continue

        log(f"Account: {accountInfo.login} | Balance: {accountInfo.balance} | Equity: {accountInfo.equity}")

        # Route to the correct handler based on run type and collect result
        if run_type == 'start':
            result = handle_start_run(
                acc_data_ws, acc_data_rows, cred['ID'], cred['Type'], cred['Category'],
                cred['DepositSize'], cred['DailyDrawdown'], cred['ProfitTarget'],
                accountInfo.balance, accountInfo.equity
            )
        elif run_type == 'end':
            # Fetch BALANCE deals (type 2 = deposit/withdrawal) for the trading session window:
            # from 4 PM yesterday (start run time) to now (end run time).
            _now           = datetime.now()
            _session_start = (_now - timedelta(days=1)).replace(
                hour=16, minute=0, second=0, microsecond=0
            )
            _bal_deals   = mt5.history_deals_get(_session_start, _now) or []
            _deposits    = sum(d.profit for d in _bal_deals if d.type == 2 and d.profit > 0)
            _withdrawals = sum(d.profit for d in _bal_deals if d.type == 2 and d.profit < 0)
            if _deposits or _withdrawals:
                log(f"  Session balance ops — deposits: {_deposits:.2f} | withdrawals: {_withdrawals:.2f}")
            result = handle_end_run(
                acc_data_ws, acc_data_rows, cred['ID'], cred['Type'], cred['Category'],
                cred['DepositSize'], cred['DailyDrawdown'], cred['ProfitTarget'],
                accountInfo.balance, accountInfo.equity,
                _deposits, _withdrawals
            )

        time.sleep(5)
        results.append(result)

    # ── Send SMS notifications ────────────────────────────────────────────────
    if run_type == 'start':
        # Single SMS: run summary only
        sms = build_start_sms(run_date, results, emp_map)
        log("Sending START summary SMS...")
        send_sms(sms)

    elif run_type == 'end':
        # SMS 1: run summary (total / recorded / skipped)
        log("Sending END summary SMS...")
        send_sms(build_end_summary_sms(run_date, results, emp_map))

        # SMS 2: per-account delta table (commented out — covered by analysis SMS below)
        # log("Sending END account deltas SMS...")
        # send_sms(build_end_performance_sms(run_date, results, emp_map))

        # SMS 2: challenge/funded multi-period analysis + real profit summary
        # May be split into multiple parts if content exceeds 1600 chars
        analysis_parts = build_end_analysis_sms(run_date, results, acc_data_rows, emp_map)
        log(f"Sending END analysis SMS ({len(analysis_parts)} part(s))...")
        for i, part in enumerate(analysis_parts, 1):
            log(f"  Sending analysis part {i}/{len(analysis_parts)}...")
            send_sms(part)

    # # ── api_web.csv for Flask dashboard (commented out for now) ──────────────
    # df_data.to_csv(CSV_OUTPUT_PATH, index=False, mode='w')
    # log(f"api_web.csv written with {len(df_data)} accounts.")


# ── Entry point ───────────────────────────────────────────────────────────────
# Required argument : 'start' or 'end'
# Optional argument : --account <ID>  (process only one account — for targeted re-runs)
#
# Examples:
#   python api_metatrader5_updated.py start
#   python api_metatrader5_updated.py end
#   python api_metatrader5_updated.py start --account 89088177
#   python api_metatrader5_updated.py end   --account 89088177
if len(sys.argv) < 2 or sys.argv[1] not in ('start', 'end'):
    print("Usage: python api_metatrader5_updated.py start|end [--account ACCOUNT_ID]")
    print("  start            → records StartdayBalance and StartdayEquity (4 PM MST)")
    print("  end              → records EnddayBalance and EnddayEquity (3 PM MST next day)")
    print("  --account <ID>   → process only this account (for targeted re-runs)")
    sys.exit(1)

run_type       = sys.argv[1]
single_account = None
if '--account' in sys.argv:
    idx = sys.argv.index('--account')
    if idx + 1 >= len(sys.argv):
        print("Error: --account requires an account ID argument.")
        sys.exit(1)
    single_account = sys.argv[idx + 1]

try:
    fetch_account_info(run_type, single_account)
    log("Run completed successfully.")
except Exception as e:
    log_warn(f"Script failed with error: {e}")
    raise
finally:
    mt5.shutdown()
