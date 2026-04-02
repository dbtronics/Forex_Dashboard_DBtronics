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
#         2. Daily performance report (balance and equity delta per account)

import os
import sys
import logging
from datetime import datetime, timedelta
import pytz
import MetaTrader5 as mt5
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from twilio.rest import Client as TwilioClient

mt5.initialize()

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


def build_start_sms(run_date, results):
    """
    Build the SMS message for a START run.

    Content:
      - Run date and time
      - Total / recorded / skipped counts
      - List of skipped accounts with reasons
    """
    total     = len(results)
    recorded  = [r for r in results if r['status'] == 'recorded']
    skipped   = [r for r in results if r['status'] == 'skipped']

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
            lines.append(f"  - {r['id']} ({r['reason']})")

    return "\n".join(lines)


def build_end_summary_sms(run_date, results):
    """
    Build SMS message 1 of 2 for an END run — the run summary.

    Content:
      - Run date and time
      - Total / recorded / skipped counts
      - List of skipped accounts with reasons
    """
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
            lines.append(f"  - {r['id']} ({r['reason']})")

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


def build_end_performance_sms(run_date, results):
    """
    Build SMS message 2 of 3 for an END run — per-account delta table only.
    Kept separate to stay within Twilio's 1600 character limit.
    """
    recorded = [r for r in results if r['status'] in ('recorded', 'overwritten')]

    lines = [
        "[Forex Dashboard] Account Deltas",
        f"Date: {run_date}",
        "",
        f"{'Account':<15} {'Type':<6} {'Cat':<10} {'Bal Delta':>11} {'Eq Delta':>11}",
        f"{'-'*15} {'-'*6} {'-'*10} {'-'*11} {'-'*11}",
    ]

    for r in recorded:
        is_cent   = r['type'] == 'Cent$'
        bal_delta = r['end_balance'] - r['start_balance']
        eq_delta  = r['end_equity']  - r['start_equity']
        lines.append(
            f"{r['id']:<15} {r['type']:<6} {r['category']:<10} "
            f"{fmt_delta(bal_delta, is_cent):>11} {fmt_delta(eq_delta, is_cent):>11}"
        )

    return "\n".join(lines)


def build_end_analysis_sms(run_date, results):
    """
    Build SMS message 3 of 3 for an END run — challenge/funded analysis + real profit summary.
    Kept separate to stay within Twilio's 1600 character limit.

    Sections:
      1. Challenge Progress — start%→end%, progress to target, daily DD limit
      2. Funded Status     — start%→end%, daily DD limit (no profit target)
      3. Real Profit Summary — Funded + LIVE$ + LIVE Cent$ (÷100) = Total USD profit
    """
    recorded = [r for r in results if r['status'] in ('recorded', 'overwritten')]

    lines = [
        "[Forex Dashboard] Daily Analysis",
        f"Date: {run_date}",
    ]

    # ── Challenge Progress ────────────────────────────────────────────────────
    challenges = [r for r in recorded if r['category'] == 'Challenge']
    if challenges:
        lines.append("")
        lines.append("── Challenge Progress ──────────────────────")
        for r in challenges:
            size = r['deposit_size']
            if size:
                start_pct = ((r['start_balance'] - size) / size) * 100
                end_pct   = ((r['end_balance']   - size) / size) * 100
                start_str = f"{'+' if start_pct >= 0 else ''}{start_pct:.2f}%"
                end_str   = f"{'+' if end_pct   >= 0 else ''}{end_pct:.2f}%"

                lines.append(f"  {r['id']} (Size: ${size:,.0f})")
                lines.append(f"  Day move : {start_str} → {end_str}")

                if r['profit_target'] and r['profit_target'] > 0:
                    progress = (end_pct / r['profit_target']) * 100
                    lines.append(f"  To target: {progress:.1f}% of {r['profit_target']:.0f}% target")

                if r['daily_drawdown']:
                    lines.append(f"  Daily DD : {r['daily_drawdown']:.2f}% limit")
            else:
                lines.append(f"  {r['id']} — account size not available")

    # ── Funded Status ─────────────────────────────────────────────────────────
    funded = [r for r in recorded if r['category'] == 'Funded']
    if funded:
        lines.append("")
        lines.append("── Funded Status ───────────────────────────")
        for r in funded:
            size = r['deposit_size']
            if size:
                start_pct = ((r['start_balance'] - size) / size) * 100
                end_pct   = ((r['end_balance']   - size) / size) * 100
                start_str = f"{'+' if start_pct >= 0 else ''}{start_pct:.2f}%"
                end_str   = f"{'+' if end_pct   >= 0 else ''}{end_pct:.2f}%"

                lines.append(f"  {r['id']} (Size: ${size:,.0f})")
                lines.append(f"  Day move : {start_str} → {end_str}")

                if r['daily_drawdown']:
                    lines.append(f"  Daily DD : {r['daily_drawdown']:.2f}% limit")
            else:
                lines.append(f"  {r['id']} — account size not available")

    # ── Real Profit Summary ───────────────────────────────────────────────────
    # Only Funded, LIVE $, and LIVE Cent$ count toward real profit.
    # Cent$ balances are divided by 100 to convert to USD.
    funded_profit = sum(r['end_balance'] - r['start_balance']
                        for r in recorded if r['category'] == 'Funded')
    live_dollar   = sum(r['end_balance'] - r['start_balance']
                        for r in recorded if r['category'] == 'LIVE' and r['type'] == '$')
    live_cent_raw = sum(r['end_balance'] - r['start_balance']
                        for r in recorded if r['category'] == 'LIVE' and r['type'] == 'Cent$')
    live_cent_usd = live_cent_raw / 100
    total_real    = funded_profit + live_dollar + live_cent_usd

    lines.append("")
    lines.append("── Real Profit Summary (USD) ───────────────")
    lines.append(f"  Funded           : {fmt_delta(funded_profit)}")
    lines.append(f"  Live Dollar ($)  : {fmt_delta(live_dollar)}")
    lines.append(f"  Live Cent (÷100) : {fmt_delta(live_cent_usd)}")
    lines.append(f"  {'─'*27}")
    lines.append(f"  Total Real Profit: {fmt_delta(total_real)}")

    return "\n".join(lines)


def handle_start_run(acc_data_ws, acc_data_rows, account_id, account_type, account_category,
                     deposit_size, daily_drawdown, profit_target, balance, equity):
    """
    START run logic — called when script is invoked with 'start' argument.

    Appends a new row to Acc_data with:
      - Today's date (MST local date at time of run)
      - Account ID
      - StartdayBalance and StartdayEquity (current MT5 values)
      - EnddayBalance and EnddayEquity left blank (filled by end run)

    If a row for today + this account already exists (start ran twice),
    it logs a warning and skips to prevent duplicate rows.

    Returns a result dict used for SMS reporting.
    """
    # Start run at 4 PM MST opens the NEXT calendar day's trading session.
    # e.g. start runs on Apr 1 at 4 PM → trading day is Apr 2.
    # End run on Apr 2 at 3 PM will look for today's date (Apr 2) to match.
    trading_date   = get_date_str(datetime.now() + timedelta(days=1))
    account_id_str = str(account_id)

    # Check if a row already exists for this trading date + account (duplicate start guard)
    for row in acc_data_rows[1:]:   # skip header
        if row[0] == trading_date and str(row[1]).strip() == account_id_str:
            log_warn(f"  [START] Row already exists for {account_id_str} on {trading_date}. "
                     f"Start run may have been triggered twice. Skipping.")
            return {'id': account_id_str, 'type': account_type, 'category': account_category,
                    'status': 'skipped', 'reason': 'Start row already exists'}

    # No existing row found — safe to append
    # USER_ENTERED tells Google Sheets to parse values as if typed by a user,
    # so '1-Apr-26' is stored as a real date rather than plain text.
    acc_data_ws.append_row([trading_date, account_id_str, balance, equity, '', ''], value_input_option='USER_ENTERED')
    log(f"  [START] New row written → {account_id_str} | Date: {trading_date} | "
        f"StartdayBalance={balance}, StartdayEquity={equity}")

    return {'id': account_id_str, 'type': account_type, 'category': account_category,
            'deposit_size': deposit_size, 'daily_drawdown': daily_drawdown,
            'profit_target': profit_target, 'status': 'recorded'}


def handle_end_run(acc_data_ws, acc_data_rows, account_id, account_type, account_category,
                   deposit_size, daily_drawdown, profit_target, balance, equity):
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

    for i, row in enumerate(acc_data_rows[1:], start=2):  # gspread rows are 1-indexed; skip header
        if row[0] == trading_date and str(row[1]).strip() == account_id_str:
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
        status = 'overwritten'
    else:
        # Case 1: Normal end run — fill in end-of-day values
        log(f"  [END] Row found for {account_id_str} on {trading_date} — filling end-of-day values.")
        status = 'recorded'

    # Write EnddayBalance (col 5) and EnddayEquity (col 6)
    acc_data_ws.update_cell(row_index, 5, balance)
    acc_data_ws.update_cell(row_index, 6, equity)
    log(f"  [END] Done → {account_id_str} | EnddayBalance={balance}, EnddayEquity={equity}")

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
    }


def fetch_account_info(run_type):
    """
    Main function — connects to Google Sheets, loops through all Active MT5 accounts,
    fetches balance & equity, writes to Acc_data, then sends SMS notification.

    run_type: 'start' or 'end'
    """
    client = get_gsheet_client()

    # Read credentials and Acc_data once per run to minimise API calls
    credentials   = get_credentials_from_sheet(client)
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

    for cred in credentials:
        # Login to MT5 account — login ID must be an integer
        success = mt5.login(int(cred['ID']), cred['Password'], cred['Server'])
        if not success:
            log_warn(f"  MT5 login failed for {cred['ID']} — skipping")
            results.append({'id': cred['ID'], 'type': cred['Type'], 'category': cred['Category'],
                            'status': 'skipped', 'reason': 'MT5 login failed'})
            continue

        accountInfo = mt5.account_info()
        if accountInfo is None:
            log_warn(f"  Could not retrieve account info for {cred['ID']} — skipping")
            results.append({'id': cred['ID'], 'type': cred['Type'], 'category': cred['Category'],
                            'status': 'skipped', 'reason': 'Could not retrieve account info'})
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
            result = handle_end_run(
                acc_data_ws, acc_data_rows, cred['ID'], cred['Type'], cred['Category'],
                cred['DepositSize'], cred['DailyDrawdown'], cred['ProfitTarget'],
                accountInfo.balance, accountInfo.equity
            )

        results.append(result)

    # ── Send SMS notifications ────────────────────────────────────────────────
    if run_type == 'start':
        # Single SMS: run summary only
        sms = build_start_sms(run_date, results)
        log("Sending START summary SMS...")
        send_sms(sms)

    elif run_type == 'end':
        # SMS 1: run summary (total / recorded / skipped)
        log("Sending END summary SMS...")
        send_sms(build_end_summary_sms(run_date, results))

        # SMS 2: per-account delta table
        log("Sending END account deltas SMS...")
        send_sms(build_end_performance_sms(run_date, results))

        # SMS 3: challenge/funded analysis + real profit summary
        log("Sending END analysis SMS...")
        send_sms(build_end_analysis_sms(run_date, results))

    # # ── api_web.csv for Flask dashboard (commented out for now) ──────────────
    # df_data.to_csv(CSV_OUTPUT_PATH, index=False, mode='w')
    # log(f"api_web.csv written with {len(df_data)} accounts.")


# ── Entry point ───────────────────────────────────────────────────────────────
# Expects exactly one argument: 'start' or 'end'
# Example: python api_metatrader5_updated.py start
if len(sys.argv) != 2 or sys.argv[1] not in ('start', 'end'):
    print("Usage: python api_metatrader5_updated.py start|end")
    print("  start → records StartdayBalance and StartdayEquity (run at 4 PM MST)")
    print("  end   → records EnddayBalance and EnddayEquity (run at 3 PM MST next day)")
    sys.exit(1)

run_type = sys.argv[1]

try:
    fetch_account_info(run_type)
    log("Run completed successfully.")
except Exception as e:
    log_warn(f"Script failed with error: {e}")
    raise
finally:
    mt5.shutdown()
