"""
TransactionHistory.py
Exports MT5 deal history to Google Sheets ('STS Transaction History').
One tab per account, named by account login number.

FIRST RUN (tab not found):
  - Creates a new tab named after the account number
  - Writes the header row
  - Fetches and writes the last HISTORY_DAYS (30) days of deals

SUBSEQUENT RUNS (tab already exists):
  - Fetches last INCREMENTAL_DAYS (2) days from MT5 (yesterday + today)
    to cover any deals that occurred after the previous run
  - Reads sheet rows within the last DEDUP_DAYS (3) days to collect
    existing ticket numbers
  - Appends only deals whose ticket number is not already in the sheet
    (ticket number is MT5's unique primary key for deals)

After all accounts are processed, sends a Twilio SMS report showing:
  - Open Orders   : all currently open positions (mt5.positions_get())
  - Opened Today  : entry deals placed today (BUY/SELL only)
  - Closed Today  : exit deals closed today (BUY/SELL only)

Reads MT5 credentials from 'Account' sheet in 'STS Database' (Status = Active).
Processes all active accounts.

Scheduled to run at 3:15 PM MST daily via Windows Task Scheduler
(15 minutes after api_metatrader5_updated.py end run).
"""

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

# ── Configuration ─────────────────────────────────────────────────────────────
HISTORY_DAYS     = 30  # days of history loaded on first run (tab doesn't exist yet)
INCREMENTAL_DAYS = 2   # days fetched from MT5 on subsequent runs (yesterday + today)
DEDUP_DAYS       = 3   # days of sheet rows scanned for existing ticket numbers

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

# ── Twilio configuration ──────────────────────────────────────────────────────
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN  = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_FROM_NUMBER = os.getenv('TWILIO_FROM_NUMBER')
SMS_RECIPIENTS     = [n.strip() for n in os.getenv('SMS_RECIPIENTS', '').split(',') if n.strip()]

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


# ── Time helper ───────────────────────────────────────────────────────────────
def get_mst_time():
    """Return current time converted to MST (server runs on CST)."""
    mst = pytz.timezone('US/Mountain')
    return datetime.now(mst)


# ── SMS ───────────────────────────────────────────────────────────────────────
def send_sms(body):
    """Send an SMS to all numbers in SMS_RECIPIENTS via Twilio."""
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


def build_transaction_sms(results):
    """
    Build the transaction report SMS.

    Per account shows:
      Open Orders  — currently open positions (mt5.positions_get() at run time)
      Opened Today — BUY/SELL entry deals placed today
      Closed Today — BUY/SELL exit deals closed today

    BALANCE and CREDIT deals are excluded from all counts as they are
    deposits/withdrawals, not actual trade orders.

    Ends with a totals block across all accounts.
    """
    mst_now  = get_mst_time()
    run_date = mst_now.strftime('%#d-%b-%y')   # e.g. 6-Apr-26 (Windows %#d)
    run_time = mst_now.strftime('%I:%M %p')    # e.g. 03:15 PM

    lines = [
        "[Forex Dashboard] Transaction Report",
        f"Date: {run_date} | Time: {run_time} MST",
    ]

    total_open_orders  = 0
    total_opened_today = 0
    total_closed_today = 0

    for r in results:
        if r['status'] == 'skipped':
            lines.append("")
            lines.append(f"-- {r['account_num']} --")
            lines.append(f"  Skipped ({r['reason']})")
            continue

        lines.append("")
        lines.append(f"-- {r['account_num']} --")
        lines.append(f"  Open Orders  : {r['open_orders']}")
        lines.append(f"  Opened Today : {r['opened_today']}")
        lines.append(f"  Closed Today : {r['closed_today']}")

        total_open_orders  += r['open_orders']
        total_opened_today += r['opened_today']
        total_closed_today += r['closed_today']

    # Totals block (only meaningful if more than one account)
    if len([r for r in results if r['status'] != 'skipped']) > 1:
        lines.append("")
        lines.append("-- Total --")
        lines.append(f"  Open Orders  : {total_open_orders}")
        lines.append(f"  Opened Today : {total_opened_today}")
        lines.append(f"  Closed Today : {total_closed_today}")

    return "\n".join(lines)


# ── Google Sheets auth ────────────────────────────────────────────────────────
def get_gsheet_client():
    """Authenticate with Google Sheets using the service account JSON key."""
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Credential reader (same pattern as api_metatrader5_updated.py) ────────────
def get_credentials_from_sheet(client):
    """
    Read MT5 login credentials from the Account sheet in STS Database.
    Only returns accounts where Status = 'Active'.
    Column positions are resolved dynamically from the header row.
    """
    ws   = client.open(SPREADSHEET_SOURCE).worksheet(ACCOUNT_SHEET)
    rows = ws.get_all_values()

    if not rows:
        log_warn("Account sheet is empty.")
        return []

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

    credentials     = []
    skipped_inactive = 0

    for row in rows[1:]:
        if not row[col['ID']].strip():
            continue
        status = safe_col(row, 'Status')
        if status != 'Active':
            skipped_inactive += 1
            continue
        credentials.append({
            'ID':       row[col['ID']].strip(),
            'Password': row[col['Password']].strip(),
            'Server':   row[col['Server']].strip(),
        })

    log(f"  Active accounts found: {len(credentials)} | Inactive skipped: {skipped_inactive}")
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

COL_DATE   = 0   # column index of Date in sheet (0-based)
COL_TICKET = 2   # column index of Ticket in sheet (0-based)

TYPE_MAP      = {0: "BUY", 1: "SELL", 2: "BALANCE", 3: "CREDIT"}
ENTRY_MAP_STR = {0: "ENTRY", 1: "EXIT", 2: "REVERSAL", 3: "CLOSE_BY"}

# Deal types that count as real trade orders (excludes BALANCE=2, CREDIT=3)
TRADE_TYPES = {0, 1}


def deals_to_rows(deals, account_num, balance, equity):
    """
    Convert MT5 deal objects into sheet rows.
    Builds an entry_map to link exit deals back to their opening deal
    for open time and duration calculation.
    All deal types included (BUY, SELL, BALANCE, CREDIT).
    """
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
            open_time_str = deal_time.strftime("%Y.%m.%d %H:%M:%S")
        elif deal.entry in (1, 2, 3):
            close_time_str = deal_time.strftime("%Y.%m.%d %H:%M:%S")
            opening = entry_map.get(deal.position_id)
            if opening:
                open_dt       = datetime.fromtimestamp(opening.time)
                open_time_str = open_dt.strftime("%Y.%m.%d %H:%M:%S")
                duration      = deal.time - opening.time

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


def get_existing_tickets(ws, dedup_cutoff):
    """
    Return a set of ticket numbers (strings) from sheet rows whose Date
    falls on or after dedup_cutoff. Date-based window is used instead of
    a fixed row count so deduplication is reliable for any trade frequency.
    """
    all_rows         = ws.get_all_values()
    existing_tickets = set()

    for row in all_rows[1:]:
        if not row or len(row) <= COL_TICKET:
            continue
        try:
            row_date = datetime.strptime(row[COL_DATE].strip(), '%Y.%m.%d')
            if row_date >= dedup_cutoff:
                ticket = row[COL_TICKET].strip()
                if ticket:
                    existing_tickets.add(ticket)
        except (ValueError, IndexError):
            continue

    return existing_tickets


def count_today_deals(deals, today):
    """
    Count entry and exit deals for today from a list of MT5 deal objects.
    Only BUY and SELL deal types are counted (BALANCE and CREDIT excluded).

    Returns (opened_today, closed_today).
    """
    opened_today = 0
    closed_today = 0

    for deal in deals:
        if deal.type not in TRADE_TYPES:
            continue
        deal_date = datetime.fromtimestamp(deal.time).date()
        if deal_date != today:
            continue
        if deal.entry == 0:
            opened_today += 1
        elif deal.entry in (1, 2, 3):
            closed_today += 1

    return opened_today, closed_today


# ── Per-account export ────────────────────────────────────────────────────────
def export_account(dest_wb, cred):
    """
    Export deal history for one account to STS Transaction History.
    Returns a result dict used for SMS reporting.

    Tab not found → create tab, write header, backfill last HISTORY_DAYS days
    Tab found     → fetch last INCREMENTAL_DAYS, deduplicate by ticket, append new
    """
    account_id = cred['ID']
    log(f"  Logging into MT5 account {account_id}...")

    success = mt5.login(int(account_id), cred['Password'], cred['Server'])
    if not success:
        log_warn(f"  MT5 login failed for {account_id}: {mt5.last_error()}")
        return {'account_num': account_id, 'status': 'skipped', 'reason': 'MT5 login failed'}

    account_info = mt5.account_info()
    if account_info is None:
        log_warn(f"  Could not retrieve account info for {account_id}.")
        return {'account_num': account_id, 'status': 'skipped', 'reason': 'Account info unavailable'}

    account_num = str(account_info.login)
    balance     = account_info.balance
    equity      = account_info.equity
    log(f"  Account {account_num} | Balance: {balance} | Equity: {equity}")

    # Open positions — currently running trades regardless of when they opened
    positions   = mt5.positions_get() or []
    open_orders = len(positions)

    existing_tabs = [ws.title for ws in dest_wb.worksheets()]
    tab_exists    = account_num in existing_tabs
    now           = datetime.now()
    today         = now.date()

    if not tab_exists:
        # ── First run: create tab and backfill last HISTORY_DAYS ──────────
        log(f"  Tab '{account_num}' not found — creating and loading last {HISTORY_DAYS} days.")
        ws = dest_wb.add_worksheet(title=account_num, rows=5000, cols=len(DEAL_HEADER))
        ws.append_row(DEAL_HEADER)

        start_dt = (now - timedelta(days=HISTORY_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_dt = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        deals  = fetch_deals(start_dt, end_dt)
        log(f"  Deals found (last {HISTORY_DAYS}d): {len(deals)}")

        if deals:
            rows = deals_to_rows(deals, account_num, balance, equity)
            ws.append_rows(rows, value_input_option='USER_ENTERED')
            log(f"  Written {len(rows)} rows to tab '{account_num}'.")

    else:
        # ── Subsequent run: fetch last INCREMENTAL_DAYS, deduplicate ──────
        log(f"  Tab '{account_num}' found — fetching last {INCREMENTAL_DAYS} days from MT5.")
        ws = dest_wb.worksheet(account_num)

        start_dt = (now - timedelta(days=INCREMENTAL_DAYS - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_dt = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        deals  = fetch_deals(start_dt, end_dt)
        log(f"  Deals fetched from MT5: {len(deals)}")

        dedup_cutoff     = (now - timedelta(days=DEDUP_DAYS - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        existing_tickets = get_existing_tickets(ws, dedup_cutoff)
        log(f"  Existing tickets in last {DEDUP_DAYS} days: {len(existing_tickets)}")

        new_deals = [d for d in deals if str(d.ticket) not in existing_tickets]
        log(f"  New: {len(new_deals)} | Duplicates skipped: {len(deals) - len(new_deals)}")

        if new_deals:
            rows = deals_to_rows(new_deals, account_num, balance, equity)
            ws.append_rows(rows, value_input_option='USER_ENTERED')
            log(f"  Written {len(rows)} rows to tab '{account_num}'.")
        else:
            log(f"  Nothing new to write for account {account_num}.")

    # Count today's opened/closed deals from the full fetch
    # (use all deals fetched, not just new ones, for accurate daily counts)
    opened_today, closed_today = count_today_deals(deals if deals else [], today)

    return {
        'account_num':  account_num,
        'status':       'recorded',
        'open_orders':  open_orders,
        'opened_today': opened_today,
        'closed_today': closed_today,
    }


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

    results = []
    for cred in credentials:
        log("-" * 40)
        result = export_account(dest_wb, cred)
        results.append(result)

    # Send transaction report SMS
    log("-" * 40)
    log("Sending transaction report SMS...")
    send_sms(build_transaction_sms(results))

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
