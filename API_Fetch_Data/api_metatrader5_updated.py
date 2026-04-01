# pip install MetaTrader5 gspread google-auth

import os
import MetaTrader5 as mt5
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

mt5.initialize()

# ── Google Sheets config ────────────────────────────────────────────────────
SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, '..', 'n8n-automation-dbtronics-49815df8eb82.json')
SPREADSHEET_NAME = 'STS Database'
ACCOUNT_SHEET    = 'Account'
ACC_DATA_SHEET   = 'Acc_data'
SCOPES           = ['https://www.googleapis.com/auth/spreadsheets',
                    'https://www.googleapis.com/auth/drive']

# # api_web.csv — commented out, kept for future Flask dashboard use
# import pandas as pd
# CSV_OUTPUT_PATH = os.path.join(SCRIPT_DIR, '..', 'api_web.csv')
# ────────────────────────────────────────────────────────────────────────────


def get_gsheet_client():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def get_credentials_from_sheet(client):
    """Read ID, Password, Server from the Account sheet."""
    ws = client.open(SPREADSHEET_NAME).worksheet(ACCOUNT_SHEET)
    rows = ws.get_all_values()

    credentials = []
    for row in rows[1:]:         # skip header
        if not row[0].strip():   # skip blank rows
            continue
        status = row[7].strip() if len(row) > 7 else ''
        if status != 'Active':   # only process active accounts
            print(f"  Skipping account {row[0].strip()} (Status: '{status}')")
            continue
        credentials.append({
            'ID':       row[0].strip(),
            'Password': row[1].strip(),
            'Server':   row[2].strip(),
        })
    return credentials


def get_today_str():
    """Returns date string matching the Acc_data format, e.g. '1-Apr-26'."""
    return datetime.now().strftime('%-d-%b-%y')


def update_acc_data(acc_data_ws, acc_data_rows, account_id, balance, equity):
    """
    First run of the day  → no row exists for today + account_id
                          → append new row, populate StartdayBalance & StartdayEquity.
    Second run of the day → row already exists
                          → update EnddayBalance & EnddayEquity only.
    """
    today          = get_today_str()
    account_id_str = str(account_id)

    # Search for an existing row matching today + account_id
    row_index = None
    for i, row in enumerate(acc_data_rows[1:], start=2):  # 1-indexed; row 1 is header
        if row[0] == today and str(row[1]).strip() == account_id_str:
            row_index = i
            break

    if row_index is None:
        # First run of the day — write start-of-day row
        acc_data_ws.append_row([today, account_id_str, balance, equity, '', ''])
        print(f"  [Acc_data] NEW row → {account_id_str} | StartdayBalance={balance}, StartdayEquity={equity}")
    else:
        # Second run of the day — update end-of-day columns
        acc_data_ws.update_cell(row_index, 5, balance)  # EnddayBalance
        acc_data_ws.update_cell(row_index, 6, equity)   # EnddayEquity
        print(f"  [Acc_data] UPDATED → {account_id_str} | EnddayBalance={balance}, EnddayEquity={equity}")


def fetch_account_info():
    client = get_gsheet_client()

    # Read credentials and Acc_data once per run
    credentials   = get_credentials_from_sheet(client)
    db            = client.open(SPREADSHEET_NAME)
    acc_data_ws   = db.worksheet(ACC_DATA_SHEET)
    acc_data_rows = acc_data_ws.get_all_values()

    print(f"Accounts found: {len(credentials)}")

    for cred in credentials:
        success = mt5.login(int(cred['ID']), cred['Password'], cred['Server'])
        if not success:
            print(f"  MT5 login failed for {cred['ID']} — skipping")
            continue

        accountInfo = mt5.account_info()
        if accountInfo is None:
            print(f"  Could not retrieve account info for {cred['ID']} — skipping")
            continue

        print(f"Account: {accountInfo.login} | Balance: {accountInfo.balance} | Equity: {accountInfo.equity}")

        update_acc_data(acc_data_ws, acc_data_rows, cred['ID'], accountInfo.balance, accountInfo.equity)
        print()

    # # ── api_web.csv for Flask dashboard (commented out for now) ──────────────
    # df_data.to_csv(CSV_OUTPUT_PATH, index=False, mode='w')
    # print(f"api_web.csv written with {len(df_data)} accounts.")


# ── Run once (designed to be triggered by cron job twice a day) ──────────────
try:
    fetch_account_info()
    print("Done.")
finally:
    mt5.shutdown()
