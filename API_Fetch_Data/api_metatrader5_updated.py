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

    credentials = []
    for row in rows[1:]:        # skip header row
        if not row[col['ID']].strip():  # skip empty rows
            continue

        status = row[col['Status']].strip() if len(row) > col['Status'] else ''

        if status != 'Active':
            # Inactive, closed, or blank status accounts are ignored
            log(f"  Skipping account {row[col['ID']].strip()} (Status: '{status}')")
            continue

        credentials.append({
            'ID':       row[col['ID']].strip(),
            'Password': row[col['Password']].strip(),
            'Server':   row[col['Server']].strip(),
        })

    return credentials


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
        f"Date: {run_date} | Time: {datetime.now().strftime('%I:%M %p')} MST",
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
        f"Date: {run_date} | Time: {datetime.now().strftime('%I:%M %p')} MST",
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


def build_end_performance_sms(run_date, results):
    """
    Build SMS message 2 of 2 for an END run — the daily performance report.

    Content:
      - Delta of Balance and Equity (end - start) per account
      - Positive delta shown as +$x.xx, negative as -$x.xx
      - Only accounts that were successfully recorded are included
    """
    recorded = [r for r in results if r['status'] in ('recorded', 'overwritten')]

    lines = [
        "[Forex Dashboard] Daily Performance Report",
        f"Date: {run_date}",
        "",
        f"{'Account':<15} {'Bal Delta':>10} {'Eq Delta':>10}",
        f"{'-'*15} {'-'*10} {'-'*10}",
    ]

    for r in recorded:
        bal_delta = r['end_balance'] - r['start_balance']
        eq_delta  = r['end_equity']  - r['start_equity']

        bal_str = f"+${bal_delta:,.2f}" if bal_delta >= 0 else f"-${abs(bal_delta):,.2f}"
        eq_str  = f"+${eq_delta:,.2f}"  if eq_delta  >= 0 else f"-${abs(eq_delta):,.2f}"

        lines.append(f"{r['id']:<15} {bal_str:>10} {eq_str:>10}")

    return "\n".join(lines)


def handle_start_run(acc_data_ws, acc_data_rows, account_id, balance, equity):
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
    today          = get_date_str(datetime.now())
    account_id_str = str(account_id)

    # Check if a row already exists for today + this account
    for row in acc_data_rows[1:]:   # skip header
        if row[0] == today and str(row[1]).strip() == account_id_str:
            log_warn(f"  [START] Row already exists for {account_id_str} on {today}. "
                     f"Start run may have been triggered twice. Skipping.")
            return {'id': account_id_str, 'status': 'skipped', 'reason': 'Start row already exists'}

    # No existing row found — safe to append
    # USER_ENTERED tells Google Sheets to parse values as if typed by a user,
    # so '1-Apr-26' is stored as a real date rather than plain text.
    acc_data_ws.append_row([today, account_id_str, balance, equity, '', ''], value_input_option='USER_ENTERED')
    log(f"  [START] New row written → {account_id_str} | Date: {today} | "
        f"StartdayBalance={balance}, StartdayEquity={equity}")

    return {'id': account_id_str, 'status': 'recorded'}


def handle_end_run(acc_data_ws, acc_data_rows, account_id, balance, equity):
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
    yesterday      = get_date_str(datetime.now() - timedelta(days=1))
    account_id_str = str(account_id)

    # Search Acc_data for a row matching yesterday's date and this account ID
    row_index = None
    start_balance = None
    start_equity  = None

    for i, row in enumerate(acc_data_rows[1:], start=2):  # gspread rows are 1-indexed; skip header
        if row[0] == yesterday and str(row[1]).strip() == account_id_str:
            row_index     = i
            start_balance = parse_float(row[2])
            start_equity  = parse_float(row[3])
            break

    if row_index is None:
        # Case 3: No start row found for yesterday — start run was likely missed
        log_warn(f"  [END] No start row found for {account_id_str} on {yesterday}. "
                 f"Start run may have been missed. Skipping.")
        return {'id': account_id_str, 'status': 'skipped', 'reason': 'No start row found'}

    # Check if EnddayBalance is already filled (column index 4, 0-based)
    # Use parse_float to handle any currency formatting Google Sheets may apply
    existing_endday = parse_float(acc_data_rows[row_index - 1][4])

    if existing_endday:
        # Case 2: End already recorded — overwrite with latest values
        log_warn(f"  [END] EnddayBalance already exists for {account_id_str} on {yesterday}. "
                 f"Overwriting with latest values.")
        status = 'overwritten'
    else:
        # Case 1: Normal end run — fill in end-of-day values
        log(f"  [END] Row found for {account_id_str} on {yesterday} — filling end-of-day values.")
        status = 'recorded'

    # Write EnddayBalance (col 5) and EnddayEquity (col 6)
    acc_data_ws.update_cell(row_index, 5, balance)
    acc_data_ws.update_cell(row_index, 6, equity)
    log(f"  [END] Done → {account_id_str} | EnddayBalance={balance}, EnddayEquity={equity}")

    return {
        'id':            account_id_str,
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

    run_date = get_date_str(datetime.now())

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
            results.append({'id': cred['ID'], 'status': 'skipped', 'reason': 'MT5 login failed'})
            continue

        accountInfo = mt5.account_info()
        if accountInfo is None:
            log_warn(f"  Could not retrieve account info for {cred['ID']} — skipping")
            results.append({'id': cred['ID'], 'status': 'skipped', 'reason': 'Could not retrieve account info'})
            continue

        log(f"Account: {accountInfo.login} | Balance: {accountInfo.balance} | Equity: {accountInfo.equity}")

        # Route to the correct handler based on run type and collect result
        if run_type == 'start':
            result = handle_start_run(acc_data_ws, acc_data_rows, cred['ID'], accountInfo.balance, accountInfo.equity)
        elif run_type == 'end':
            result = handle_end_run(acc_data_ws, acc_data_rows, cred['ID'], accountInfo.balance, accountInfo.equity)

        results.append(result)

    # ── Send SMS notifications ────────────────────────────────────────────────
    if run_type == 'start':
        # Single SMS: run summary only
        sms = build_start_sms(run_date, results)
        log("Sending START summary SMS...")
        send_sms(sms)

    elif run_type == 'end':
        # SMS 1: run summary
        log("Sending END summary SMS...")
        send_sms(build_end_summary_sms(run_date, results))

        # SMS 2: daily performance report
        log("Sending END performance report SMS...")
        send_sms(build_end_performance_sms(run_date, results))

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
