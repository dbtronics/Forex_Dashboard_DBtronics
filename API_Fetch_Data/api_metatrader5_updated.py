# pip install MetaTrader5 gspread google-auth
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
#
#   END run (3 PM MST next calendar day):
#     - Reads all Active accounts from the "Account" sheet
#     - Logs into each MT5 account and fetches current balance & equity
#     - Looks for yesterday's row in "Acc_data" for each account
#       (yesterday because 4 PM start → 3 PM end = different calendar dates in MST)
#     - If found and incomplete → fills EnddayBalance and EnddayEquity
#     - If found and already complete → overwrites with latest values (logs a warning)
#     - If not found at all → logs a warning and skips (start run likely missed)

import os
import sys
from datetime import datetime, timedelta
import MetaTrader5 as mt5
import gspread
from google.oauth2.service_account import Credentials

mt5.initialize()

# ── Google Sheets configuration ──────────────────────────────────────────────
# SCRIPT_DIR resolves to the folder this script lives in, regardless of where
# it is called from — makes all relative paths reliable on any machine.
SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))

# Service account JSON key file — stored in project root, excluded from git via .gitignore
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, '..', 'n8n-automation-dbtronics-49815df8eb82.json')

SPREADSHEET_NAME = 'STS Database'  # Google Sheets file name
ACCOUNT_SHEET    = 'Account'        # Sheet containing MT5 login credentials
ACC_DATA_SHEET   = 'Acc_data'       # Sheet where daily balance/equity data is written

# Scopes required: read + write access to Sheets and Drive
SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']

# # api_web.csv — commented out, kept for future Flask dashboard use
# import pandas as pd
# CSV_OUTPUT_PATH = os.path.join(SCRIPT_DIR, '..', 'api_web.csv')
# ─────────────────────────────────────────────────────────────────────────────


def get_gsheet_client():
    """Authenticate with Google Sheets using the service account key file."""
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def get_credentials_from_sheet(client):
    """
    Read MT5 login credentials from the Account sheet.
    Only returns accounts where Status column = 'Active'.
    Columns used: ID (0), Password (1), Server (2), Status (7).
    """
    ws   = client.open(SPREADSHEET_NAME).worksheet(ACCOUNT_SHEET)
    rows = ws.get_all_values()

    credentials = []
    for row in rows[1:]:        # skip header row
        if not row[0].strip():  # skip empty rows
            continue

        status = row[7].strip() if len(row) > 7 else ''

        if status != 'Active':
            # Inactive, closed, or blank status accounts are ignored
            print(f"  Skipping account {row[0].strip()} (Status: '{status}')")
            continue

        credentials.append({
            'ID':       row[0].strip(),
            'Password': row[1].strip(),
            'Server':   row[2].strip(),
        })

    return credentials


def get_date_str(date):
    """
    Format a date object into the Acc_data sheet format, e.g. '1-Apr-26'.
    Uses %#d on Windows (no leading zero on day).
    """
    return date.strftime('%#d-%b-%y')


def handle_start_run(acc_data_ws, acc_data_rows, account_id, balance, equity):
    """
    START run logic — called when script is invoked with 'start' argument.

    Appends a new row to Acc_data with:
      - Today's date (MST local date at time of run)
      - Account ID
      - StartdayBalance and StartdayEquity (current MT5 values)
      - EnddayBalance and EnddayEquity left blank (filled by end run)
    """
    today          = get_date_str(datetime.now())
    account_id_str = str(account_id)

    acc_data_ws.append_row([today, account_id_str, balance, equity, '', ''])
    print(f"  [START] New row written → {account_id_str} | Date: {today} | "
          f"StartdayBalance={balance}, StartdayEquity={equity}")


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
    """
    yesterday      = get_date_str(datetime.now() - timedelta(days=1))
    account_id_str = str(account_id)

    # Search Acc_data for a row matching yesterday's date and this account ID
    row_index = None
    for i, row in enumerate(acc_data_rows[1:], start=2):  # gspread rows are 1-indexed; skip header
        if row[0] == yesterday and str(row[1]).strip() == account_id_str:
            row_index = i
            break

    if row_index is None:
        # Case 3: No start row found for yesterday — start run was likely missed
        print(f"  [END] ⚠ WARNING: No start row found for {account_id_str} on {yesterday}. "
              f"Start run may have been missed. Skipping.")
        return

    # Check if EnddayBalance is already filled (column index 4, 0-based)
    existing_endday = acc_data_rows[row_index - 1][4]

    if existing_endday:
        # Case 2: End already recorded — overwrite with latest values
        print(f"  [END] ⚠ WARNING: EnddayBalance already exists for {account_id_str} on {yesterday}. "
              f"Overwriting with latest values.")
    else:
        # Case 1: Normal end run — fill in end-of-day values
        print(f"  [END] Row found for {account_id_str} on {yesterday} — filling end-of-day values.")

    # Write EnddayBalance (col 5) and EnddayEquity (col 6)
    acc_data_ws.update_cell(row_index, 5, balance)
    acc_data_ws.update_cell(row_index, 6, equity)
    print(f"  [END] Done → {account_id_str} | EnddayBalance={balance}, EnddayEquity={equity}")


def fetch_account_info(run_type):
    """
    Main function — connects to Google Sheets, loops through all Active MT5 accounts,
    fetches balance & equity, and writes to Acc_data based on run_type.

    run_type: 'start' or 'end'
    """
    client = get_gsheet_client()

    # Read credentials and Acc_data once per run to minimise API calls
    credentials   = get_credentials_from_sheet(client)
    db            = client.open(SPREADSHEET_NAME)
    acc_data_ws   = db.worksheet(ACC_DATA_SHEET)
    acc_data_rows = acc_data_ws.get_all_values()

    print(f"\nRun type : {run_type.upper()}")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Active accounts found: {len(credentials)}\n")

    for cred in credentials:
        # Login to MT5 account — login ID must be an integer
        success = mt5.login(int(cred['ID']), cred['Password'], cred['Server'])
        if not success:
            print(f"  MT5 login failed for {cred['ID']} — skipping")
            continue

        accountInfo = mt5.account_info()
        if accountInfo is None:
            print(f"  Could not retrieve account info for {cred['ID']} — skipping")
            continue

        print(f"Account: {accountInfo.login} | Balance: {accountInfo.balance} | Equity: {accountInfo.equity}")

        # Route to the correct handler based on run type
        if run_type == 'start':
            handle_start_run(acc_data_ws, acc_data_rows, cred['ID'], accountInfo.balance, accountInfo.equity)
        elif run_type == 'end':
            handle_end_run(acc_data_ws, acc_data_rows, cred['ID'], accountInfo.balance, accountInfo.equity)

        print()

    # # ── api_web.csv for Flask dashboard (commented out for now) ──────────────
    # df_data.to_csv(CSV_OUTPUT_PATH, index=False, mode='w')
    # print(f"api_web.csv written with {len(df_data)} accounts.")


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
    print("Done.")
finally:
    mt5.shutdown()
