"""
TransactionHistory.py
Exports MT5 deal history to Google Sheets ('STS Transaction History').
One tab per account, named by account login number.

FIRST RUN (tab not found):
  - Creates a new tab named after the account number
  - Writes the header row
  - Fetches and writes the last 7 days of deals

SUBSEQUENT RUNS (tab already exists):
  - Appends today's deals only (incremental)

Reads MT5 credentials from 'Account' sheet in 'STS Database' (Status = Active).
Processes up to MAX_ACCOUNTS accounts — others are logged and skipped.

Scheduled to run at 3:15 PM MST daily via Windows Task Scheduler
(15 minutes after api_metatrader5_updated.py end run).
"""

import os
import sys
import logging
from datetime import datetime, timedelta
import MetaTrader5 as mt5
import gspread
from google.oauth2.service_account import Credentials

# ── Configuration ─────────────────────────────────────────────────────────────
MAX_ACCOUNTS  = 2   # cap: process only the first N active accounts
HISTORY_DAYS  = 7   # days of history loaded on first run (tab doesn't exist yet)

SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE  = os.path.join(SCRIPT_DIR, 'n8n-automation-dbtronics-49815df8eb82.json')
LOG_FILE          = os.path.join(SCRIPT_DIR, 'cron.log')

SPREADSHEET_SOURCE = 'STS Database'            # source: MT5 account credentials
SPREADSHEET_DEST   = 'STS Transaction History'  # destination: deal history output
ACCOUNT_SHEET      = 'Account'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# ── Logging ───────────────────────────────────────────────────────────────────
# Appends to the same cron.log used by api_metatrader5_updated.py
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def log(msg):
    logging.info(msg)

def log_warn(msg):
    logging.warning(msg)


# ── Google Sheets auth ────────────────────────────────────────────────────────
def get_gsheet_client():
    """Authenticate with Google Sheets using the service account JSON key."""
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Credential reader (same pattern as api_metatrader5_updated.py) ─────────────
def parse_float(value):
    """Strip currency formatting and convert to float."""
    if not value:
        return None
    try:
        return float(str(value).replace('$', '').replace(',', '').strip())
    except ValueError:
        return None


def get_credentials_from_sheet(client):
    """
    Read MT5 login credentials from the Account sheet in STS Database.
    Only returns accounts where Status = 'Active'.
    Column positions are resolved dynamically from the header row.
    Returns at most MAX_ACCOUNTS credentials — logs and skips the rest.
    """
    ws   = client.open(SPREADSHEET_SOURCE).worksheet(ACCOUNT_SHEET)
    rows = ws.get_all_values()

    if not rows:
        log_warn("Account sheet is empty.")
        return []

    # Build column index map from header row — resilient to reordering
    headers = [h.strip() for h in rows[0]]
    col     = {name: idx for idx, name in enumerate(headers)}
    log(f"  Account sheet columns: {headers}")

    required = ['ID', 'Password', 'Server', 'Status']
    missing  = [c for c in required if c not in col]
    if missing:
        log_warn(f"  Missing required columns in Account sheet: {missing}. Cannot proceed.")
        return []

    def safe_col(row, name):
        return row[col[name]].strip() if name in col and len(row) > col[name] else ''

    credentials    = []
    skipped_inactive = 0
    skipped_cap      = 0

    for row in rows[1:]:
        if not row[col['ID']].strip():
            continue

        status = safe_col(row, 'Status')
        if status != 'Active':
            skipped_inactive += 1
            continue

        # Enforce account cap — log each account that gets skipped
        if len(credentials) >= MAX_ACCOUNTS:
            log_warn(f"  Account cap ({MAX_ACCOUNTS}) reached — skipping {row[col['ID']].strip()}.")
            skipped_cap += 1
            continue

        credentials.append({
            'ID':       row[col['ID']].strip(),
            'Password': row[col['Password']].strip(),
            'Server':   row[col['Server']].strip(),
        })

    log(f"  Credentials loaded: {len(credentials)} | "
        f"Inactive skipped: {skipped_inactive} | Cap skipped: {skipped_cap}")
    return credentials


# ── Deal export helpers ───────────────────────────────────────────────────────
DEAL_HEADER = [
    "Date", "Account Number", "Ticket", "Position ID", "Symbol",
    "Type", "Entry/Exit", "Magic Number", "Manual Trade",
    "Comment", "Open Time", "Close Time", "Duration (s)",
    "Entry Price", "Exit Price", "SL", "TP", "Lot Size",
    "Balance at Export", "Equity at Export", "Profit",
    "Commission", "Swap", "Net Profit"
]

# All deal types included — BALANCE and CREDIT are essential for audit trail
TYPE_MAP      = {0: "BUY", 1: "SELL", 2: "BALANCE", 3: "CREDIT"}
ENTRY_MAP_STR = {0: "ENTRY", 1: "EXIT", 2: "REVERSAL", 3: "CLOSE_BY"}


def deals_to_rows(deals, account_num, balance, equity):
    """
    Convert MT5 deal objects into sheet rows.

    Builds an entry_map (position_id → opening deal) to link exit deals
    back to their entry for open time and trade duration calculation.
    If the opening deal falls outside the fetched date range, open time
    and duration are left blank/zero gracefully.

    All deal types are included: BUY, SELL, BALANCE, CREDIT, etc.
    """
    # Map position_id → entry deal so exit deals can look up their open time
    entry_map = {
        deal.position_id: deal
        for deal in deals
        if deal.entry == 0
    }

    rows = []
    for deal in deals:
        deal_time  = datetime.fromtimestamp(deal.time)
        type_str   = TYPE_MAP.get(deal.type, "OTHER")
        entry_str  = ENTRY_MAP_STR.get(deal.entry, "OTHER")
        manual_str = "YES" if deal.magic == 0 else "NO"
        net_profit = deal.profit + deal.commission + deal.swap

        open_time_str  = ""
        close_time_str = ""
        duration       = 0

        if deal.entry == 0:
            # This deal is an entry — record as open time
            open_time_str = deal_time.strftime("%Y.%m.%d %H:%M:%S")
        elif deal.entry in (1, 2, 3):
            # This deal is an exit — record close time and look up open time
            close_time_str = deal_time.strftime("%Y.%m.%d %H:%M:%S")
            opening = entry_map.get(deal.position_id)
            if opening:
                open_dt       = datetime.fromtimestamp(opening.time)
                open_time_str = open_dt.strftime("%Y.%m.%d %H:%M:%S")
                duration      = deal.time - opening.time

        # Entry price is only meaningful on ENTRY deals, exit price on EXIT deals
        entry_price = deal.price if deal.entry == 0 else 0.0
        exit_price  = deal.price if deal.entry == 1 else 0.0

        rows.append([
            deal_time.strftime("%Y.%m.%d"),
            account_num,
            deal.ticket,
            deal.position_id,
            deal.symbol,
            type_str,
            entry_str,
            deal.magic,
            manual_str,
            deal.comment,
            open_time_str,
            close_time_str,
            duration,
            f"{entry_price:.5f}",
            f"{exit_price:.5f}",
            f"{getattr(deal, 'sl', 0.0):.5f}",
            f"{getattr(deal, 'tp', 0.0):.5f}",
            f"{deal.volume:.2f}",
            f"{balance:.2f}",
            f"{equity:.2f}",
            f"{deal.profit:.2f}",
            f"{deal.commission:.2f}",
            f"{deal.swap:.2f}",
            f"{net_profit:.2f}",
        ])

    return rows


def fetch_deals(start_dt, end_dt):
    """Fetch all deals from MT5 between start_dt and end_dt."""
    deals = mt5.history_deals_get(start_dt, end_dt)
    if deals is None:
        log_warn(f"  mt5.history_deals_get returned None: {mt5.last_error()}")
        return ()
    return deals


# ── Per-account export ────────────────────────────────────────────────────────
def export_account(dest_wb, cred):
    """
    Export deal history for one account to STS Transaction History.

    Tab existence check:
      - Tab not found → create tab, write header, fetch last HISTORY_DAYS days
      - Tab found     → append today's deals only (incremental)
    """
    account_id = cred['ID']
    log(f"  Logging into MT5 account {account_id}...")

    success = mt5.login(int(account_id), cred['Password'], cred['Server'])
    if not success:
        log_warn(f"  MT5 login failed for {account_id}: {mt5.last_error()}")
        return

    account_info = mt5.account_info()
    if account_info is None:
        log_warn(f"  Could not retrieve account info for {account_id}.")
        return

    account_num = str(account_info.login)
    balance     = account_info.balance
    equity      = account_info.equity
    log(f"  Account {account_num} | Balance: {balance} | Equity: {equity}")

    # Check whether a tab for this account already exists
    existing_tabs = [ws.title for ws in dest_wb.worksheets()]
    tab_exists    = account_num in existing_tabs
    now           = datetime.now()

    if not tab_exists:
        # ── First run: create tab and backfill last HISTORY_DAYS days ─────
        log(f"  Tab '{account_num}' not found — creating and loading last {HISTORY_DAYS} days.")
        ws = dest_wb.add_worksheet(title=account_num, rows=5000, cols=len(DEAL_HEADER))
        ws.append_row(DEAL_HEADER)

        start_dt = (now - timedelta(days=HISTORY_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_dt = now.replace(hour=23, minute=59, second=59, microsecond=999999)

        deals = fetch_deals(start_dt, end_dt)
        log(f"  Deals found (last {HISTORY_DAYS}d): {len(deals)}")

    else:
        # ── Subsequent run: append today's deals only ──────────────────────
        log(f"  Tab '{account_num}' found — appending today's deals.")
        ws = dest_wb.worksheet(account_num)

        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt   = now.replace(hour=23, minute=59, second=59, microsecond=999999)

        deals = fetch_deals(start_dt, end_dt)
        log(f"  Deals found (today): {len(deals)}")

    if not deals:
        log(f"  No deals to write for account {account_num}.")
        return

    rows = deals_to_rows(deals, account_num, balance, equity)

    # Write all rows in one batch call to minimise Google Sheets API calls
    ws.append_rows(rows, value_input_option='USER_ENTERED')
    log(f"  Written {len(rows)} rows to tab '{account_num}'.")


# ── Entry point ───────────────────────────────────────────────────────────────
def run():
    log("=" * 60)
    log("TransactionHistory — START")
    log("=" * 60)

    client = get_gsheet_client()

    log("Reading credentials from STS Database...")
    credentials = get_credentials_from_sheet(client)

    if not credentials:
        log_warn("No active accounts to process. Exiting.")
        return

    dest_wb = client.open(SPREADSHEET_DEST)

    mt5.initialize()

    for cred in credentials:
        log("-" * 40)
        export_account(dest_wb, cred)

    log("=" * 60)
    log("TransactionHistory — DONE")
    log("=" * 60)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log_warn(f"Script failed with error: {e}")
        raise
    finally:
        mt5.shutdown()
        log("MT5 disconnected.")
