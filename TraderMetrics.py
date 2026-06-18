"""
TraderMetrics.py
Calculates trading performance metrics per trader and writes results
to the 'Trader Metrics' tab in 'STS Database'.

Data sources:
  STS Database / Employee    -> employee names
  STS Database / Emp_Acc     -> account-to-employee mapping
  STS Database / Account     -> deposit size and daily drawdown per account
  STS Transaction History    -> one tab per account, full deal history

Only EXIT / CLOSE_BY / REVERSAL deals of type BUY or SELL are analysed.
BALANCE and CREDIT deals (deposits/withdrawals) are excluded.

Each run does a full clear-and-rewrite of the Trader Metrics tab so:
  - Newly linked accounts move from UNKNOWN TRADER to their employee row
  - New traders get a fresh row appended
  - Removed/reassigned accounts are reflected immediately

No SMS is sent. Output is the Trader Metrics Google Sheet tab only.

Usage:
  python TraderMetrics.py
"""

import os
import sys
import logging
import random
from datetime import datetime
from collections import Counter

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE   = os.path.join(SCRIPT_DIR, 'n8n-automation-dbtronics-49815df8eb82.json')
LOG_FILE           = os.path.join(SCRIPT_DIR, 'cron.log')

SPREADSHEET_SOURCE  = 'STS Database'
SPREADSHEET_HISTORY = 'STS Transaction History'
METRICS_SHEET       = 'Trader Metrics'
EMPLOYEE_SHEET      = 'Employee'
EMP_ACC_SHEET       = 'Emp_Acc'
ACCOUNT_SHEET       = 'Account'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

TARGET_PCT       = 10.0   # profit target (%) used for projection and probability
DEFAULT_RUIN_PCT = 5.0    # fallback ruin threshold when account drawdown not available
MIN_SAMPLE       = 100    # trades required for 'Sufficient' sample quality label
MONTE_CARLO_RUNS = 3000   # simulations for P(reach target before ruin)

load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)

def log(msg):      logging.info(msg)
def log_warn(msg): logging.warning(msg)


# ── Sheet layout ──────────────────────────────────────────────────────────────
DATA_HEADERS = [
    'Last Updated', 'Trader', 'Accounts', 'Days of Data',
    'Total Trades', 'Sample Quality',
    'Win Rate %', 'Break-even WR %', 'WR Margin %',
    'Profit Factor', 'Net P&L ($)',
    'Avg Win ($)', 'Avg Loss ($)', 'R:R',
    'EV/Trade ($)', 'Avg Trades/Day', 'Daily EV ($)',
    'Max Loss Streak', 'Largest Win ($)', 'Largest Loss ($)',
    'Avg Duration (hrs)', 'Manual %', 'Top Symbol',
    'Proj. Days to 10%', 'P(10% before ruin) %',
    'Notes',
]

# One empty gap column sits between data and glossary
GLOSSARY_COL_OFFSET = len(DATA_HEADERS) + 1   # 0-based column index where glossary begins

GLOSSARY = [
    ['Field', 'Calculation', 'What it means', 'Green / Red flag', 'Range'],
    [
        'Win Rate %',
        'Winners ÷ Total Trades × 100',
        'Percentage of closed trades that ended in profit. Meaningless in isolation — must be read alongside R:R.',
        'Context-dependent. 35% WR with 2.5 R:R beats 65% WR with 0.4 R:R.',
        'Poor <35% | Average 35–50% | Good 50–65% | Excellent >65%',
    ],
    [
        'Break-even WR %',
        '1 ÷ (1 + R:R) × 100',
        'Minimum win rate needed to break even at this trader\'s R:R. The profitability threshold line.',
        'Trader must stay above this. Falling below = losing money regardless of appearance.',
        'Calculated value — compare against Win Rate %. No universal range.',
    ],
    [
        'WR Margin %',
        'Win Rate % − Break-even WR %',
        'How far the trader sits above (+) or below (−) the profitability threshold.',
        'Positive = viable system. Negative = losing system. Larger margin = more robust to variance.',
        'Losing <0% | Marginal 0–5% | Good 5–15% | Excellent >15%',
    ],
    [
        'Profit Factor',
        'Gross Profit ÷ Gross Loss',
        'Dollars earned for every dollar lost. Most reliable single profitability indicator.',
        '>1.5 decent | >2.0 strong | <1.0 losing money',
        'Losing <1.0 | Marginal 1.0–1.5 | Good 1.5–2.0 | Excellent >2.0',
    ],
    [
        'Net P&L ($)',
        'Sum of all closed trade Net Profit values',
        'Total realised profit or loss across all accounts.',
        'Must be positive over a sufficient sample. Small positive on low sample = inconclusive.',
        'Relative to account size — compare traders by Profit Factor and EV/Trade instead.',
    ],
    [
        'Avg Win ($)',
        'Sum of winning trade profits ÷ number of winners',
        'Average dollar gain on a trade that closes in profit.',
        'Should exceed Avg Loss in absolute terms if win rate is below 50%.',
        'Relative to account size — what matters is Avg Win vs Avg Loss ratio (R:R).',
    ],
    [
        'Avg Loss ($)',
        'Sum of losing trade losses ÷ number of losers  (shown negative)',
        'Average dollar loss on a losing trade.',
        'If larger than Avg Win, trader needs >50% WR just to break even.',
        'Relative to account size — what matters is Avg Loss vs Avg Win ratio (R:R).',
    ],
    [
        'R:R',
        'Avg Win ÷ |Avg Loss|',
        'Return earned per unit of risk. A 1:2 R:R means winning trades are twice the size of losers.',
        '>1.0 = wins bigger than losses | <1.0 = needs high win rate to compensate',
        'Poor <0.8 | Average 0.8–1.2 | Good 1.2–2.0 | Excellent >2.0',
    ],
    [
        'EV/Trade ($)',
        '(Win Rate × Avg Win) − (Loss Rate × |Avg Loss|)',
        'Expected dollar profit per trade over a large sample. The core viability signal.',
        'Must be positive. Negative EV = guaranteed long-run loss regardless of short-term results.',
        'Losing <$0 | Marginal $0–$10 | Good $10–$50 | Excellent >$50  (*scales with account size)',
    ],
    [
        'Avg Trades/Day',
        'Total Trades ÷ Calendar days between first and last trade',
        'Trade frequency. Used to convert per-trade metrics into time-based projections.',
        'Very low (<0.3) = projections span years. Very high (>20) = scalping style.',
        'Very Low <0.3 | Low 0.3–1 | Medium 1–5 | High >5  (no single best — depends on style)',
    ],
    [
        'Daily EV ($)',
        'EV/Trade × Avg Trades/Day',
        'Expected dollar profit generated per calendar day at historical pace.',
        'Positive and stable = consistent system. Near zero = marginal edge.',
        'Relative to account size — positive is required; compare proportionally across traders.',
    ],
    [
        'Max Loss Streak',
        'Longest consecutive run of losing trades in chronological order',
        'Worst observed drawdown in trade count. Tests account sizing and psychological resilience.',
        '>10 consecutive losses is dangerous. Account must be sized to withstand the streak.',
        'Safe <5 | Manageable 5–8 | Caution 8–12 | Dangerous >12',
    ],
    [
        'Largest Win ($)',
        'Maximum single trade net profit',
        'Biggest single trade. Checks if overall profit is driven by one lucky outlier.',
        'Flag if Largest Win > 5× Avg Win — results may not be repeatable.',
        'Flag if >5× Avg Win. Otherwise relative to account size — no fixed range.',
    ],
    [
        'Largest Loss ($)',
        'Minimum single trade net profit (most negative)',
        'Biggest single trade loss. Checks consistency of risk management.',
        'Flag if |Largest Loss| > 5× |Avg Loss| — indicates a risk management failure.',
        'Flag if >5× Avg Loss. Otherwise relative to account size — no fixed range.',
    ],
    [
        'Avg Duration (hrs)',
        'Mean of Duration(s) column ÷ 3600, exit deals only',
        'Average time a position is held open. Characterises trading style.',
        'Context only. Scalper (<1 hr) vs swing trader (>24 hrs) have different risk profiles.',
        'Scalper <1hr | Intraday 1–8hr | Swing 8–24hr | Position >24hr  (no single best)',
    ],
    [
        'Manual %',
        'Trades with Magic Number = 0 ÷ Total Trades × 100',
        'Percentage of trades placed manually vs by an automated EA/bot.',
        'Many prop firms restrict EAs. 100% manual is safest for compliance.',
        'Risk <50% manual | Caution 50–90% | Good 90–99% | Ideal 100% manual',
    ],
    [
        'Top Symbol',
        'Most frequently traded instrument across all closed trades',
        'Primary instrument the trader focuses on.',
        'Cross-check against account rules — some prop firms restrict exotics or indices.',
        'No numeric range — verify instrument is allowed under account rules.',
    ],
    [
        'Proj. Days to 10%',
        '(Account Size × 10%) ÷ Daily EV',
        'Linear estimate of calendar days to reach the 10% profit target at historical pace. Optimistic — ignores bad streaks.',
        'Always read alongside P(10% before ruin). A fast projection with low P(target) is misleading.',
        'Fast <30d | Good 30–60d | Average 60–120d | Slow >120d',
    ],
    [
        'P(10% before ruin) %',
        'Monte Carlo simulation (3,000 runs) using Win Rate and Avg Win/Loss as % of account size',
        'Probability of reaching +10% profit before hitting the ruin threshold (account daily drawdown limit, default −5%). Most honest forward-looking metric.',
        'This is the single most important number for vetting a trader on a funded account.',
        'Poor <40% | Uncertain 40–60% | Good 60–75% | Strong >75%',
    ],
    [
        'Notes',
        'Auto-generated by script',
        'Flags accounts with no employee assignment in the Emp_Acc sheet.',
        '⚠ warning = account needs to be assigned to an employee in Emp_Acc before next run.',
        'No range — action required if ⚠ is present.',
    ],
    [
        'Sample Quality',
        'Sufficient if Total Trades ≥ 100, else Low (N)',
        'Statistical reliability rating. Law of large numbers requires volume before metrics stabilise.',
        '<100 trades = luck dominates. All other metrics are unreliable until sample is sufficient.',
        'Insufficient <50 | Low 50–100 | Usable 100–200 | Reliable >200',
    ],
    [
        'Days of Data',
        'Date of last closed trade − Date of first closed trade + 1',
        'Calendar span covered by the trade sample. Context for Avg Trades/Day.',
        'Short span with few trades = very limited view. Longer always better.',
        'Short <30d | Medium 30–90d | Good 90–180d | Strong >180d',
    ],
]


# ── Google Sheets auth ────────────────────────────────────────────────────────
def get_gsheet_client():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_float_safe(val):
    try:
        return float(str(val).replace(',', '').replace('$', '').replace('%', '').strip())
    except (ValueError, TypeError):
        return None


# ── Data readers ──────────────────────────────────────────────────────────────
def get_emp_acc_map(db):
    """
    Returns:
      acc_to_trader  {acc_id_str -> trader_name}
      trader_to_accs {trader_name -> [acc_id_str, ...]}
    """
    emp_rows       = db.worksheet(EMPLOYEE_SHEET).get_all_values()
    emp_id_to_name = {
        r[0].strip(): r[1].strip()
        for r in emp_rows[1:] if len(r) >= 2 and r[0].strip()
    }

    emp_acc_rows  = db.worksheet(EMP_ACC_SHEET).get_all_values()
    acc_to_trader = {}
    trader_to_accs = {}

    for r in emp_acc_rows[1:]:
        if len(r) >= 2 and r[0].strip() and r[1].strip():
            emp_id = r[0].strip()
            acc_id = r[1].strip()
            name   = emp_id_to_name.get(emp_id, 'UNKNOWN TRADER')
            acc_to_trader[acc_id] = name
            trader_to_accs.setdefault(name, []).append(acc_id)

    return acc_to_trader, trader_to_accs


def get_account_info(db):
    """
    Returns {acc_id_str -> {'deposit_size': float|None, 'daily_drawdown': float|None}}
    for every row in the Account sheet regardless of Status.
    """
    rows = db.worksheet(ACCOUNT_SHEET).get_all_values()
    if not rows:
        return {}

    headers = [h.strip() for h in rows[0]]
    col     = {h: i for i, h in enumerate(headers)}
    result  = {}

    for row in rows[1:]:
        acc_id = row[col['ID']].strip() if 'ID' in col and len(row) > col['ID'] else ''
        if not acc_id:
            continue
        dep  = parse_float_safe(row[col['Deposit/Size']])   if 'Deposit/Size'   in col and len(row) > col['Deposit/Size']   else None
        draw = parse_float_safe(row[col['Daily Drawdown']]) if 'Daily Drawdown' in col and len(row) > col['Daily Drawdown'] else None
        result[acc_id] = {'deposit_size': dep, 'daily_drawdown': draw}

    return result


# ── Monte Carlo ───────────────────────────────────────────────────────────────
def monte_carlo_probability(win_rate_pct, avg_win_pct, avg_loss_pct,
                            target_pct=TARGET_PCT, ruin_pct=DEFAULT_RUIN_PCT,
                            n=MONTE_CARLO_RUNS):
    """
    Estimate P(account equity reaches +target_pct before -ruin_pct)
    via Monte Carlo simulation.

    win_rate_pct : win probability as a percentage (e.g. 58.3)
    avg_win_pct  : average win expressed as % of account size (positive)
    avg_loss_pct : average loss expressed as % of account size (positive)
    target_pct   : profit target in % (default 10.0)
    ruin_pct     : ruin threshold in % (default 5.0 or account daily drawdown)
    n            : number of simulation runs
    """
    if avg_win_pct <= 0 or avg_loss_pct <= 0 or not (0 < win_rate_pct < 100):
        return None

    p         = win_rate_pct / 100
    successes = 0

    for _ in range(n):
        equity = 0.0
        for _ in range(100_000):   # safety cap per simulation path
            if equity >= target_pct:
                successes += 1
                break
            if equity <= -ruin_pct:
                break
            equity += avg_win_pct if random.random() < p else -avg_loss_pct

    return round(successes / n * 100, 1)


# ── Metrics calculation ───────────────────────────────────────────────────────
def calculate_metrics(trades, total_deposit, ruin_pct):
    """
    trades        : list of dicts — net_profit, duration_s, magic, symbol, date
    total_deposit : combined deposit size for this trader's accounts (float or None)
    ruin_pct      : ruin threshold % (strictest drawdown across accounts, or default)

    Returns a dict of all metric values, or None if trades is empty.
    """
    if not trades:
        return None

    profits = [t['net_profit'] for t in trades]
    total   = len(profits)

    winners      = [p for p in profits if p > 0]
    losers       = [p for p in profits if p < 0]
    gross_profit = sum(winners) if winners else 0.0
    gross_loss   = abs(sum(losers)) if losers else 0.0

    win_rate      = len(winners) / total * 100
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else '∞'
    avg_win       = gross_profit / len(winners) if winners else 0.0
    avg_loss      = gross_loss   / len(losers)  if losers  else 0.0   # positive value
    rr_num        = avg_win / avg_loss if avg_loss > 0 else 0.0
    rr            = round(rr_num, 2)   if avg_loss > 0 else '∞'
    ev            = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss)
    net_pnl       = sum(profits)

    breakeven_wr = (1 / (1 + rr_num) * 100) if rr_num > 0 else 50.0
    wr_margin    = win_rate - breakeven_wr

    # Max consecutive loss streak
    max_streak = cur = 0
    for p in profits:
        if p < 0:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0

    largest_win  = max(profits)
    largest_loss = min(profits)

    # Average hold duration (exit trades only, duration must be > 0)
    durations   = [t['duration_s'] for t in trades if t.get('duration_s', 0) > 0]
    avg_dur_hrs = round(sum(durations) / len(durations) / 3600, 1) if durations else 0.0

    # Manual trade percentage
    manual_pct = round(sum(1 for t in trades if t.get('magic', 1) == 0) / total * 100, 1)

    # Most traded symbol
    symbols    = [t['symbol'] for t in trades if t.get('symbol')]
    top_symbol = Counter(symbols).most_common(1)[0][0] if symbols else '—'

    # Calendar days of data
    dates = []
    for t in trades:
        try:
            dates.append(datetime.strptime(t['date'], '%Y.%m.%d'))
        except (ValueError, TypeError):
            pass
    days_of_data       = max(1, (max(dates) - min(dates)).days + 1) if len(dates) >= 2 else 1
    avg_trades_per_day = round(total / days_of_data, 1)
    daily_ev           = round(ev * avg_trades_per_day, 2)

    # Time-based projections (require account size)
    proj_days = None
    p_target  = None
    if total_deposit and total_deposit > 0:
        if daily_ev > 0:
            proj_days = round((total_deposit * TARGET_PCT / 100) / daily_ev, 1)
        avg_win_pct  = avg_win  / total_deposit * 100
        avg_loss_pct = avg_loss / total_deposit * 100
        if avg_loss_pct > 0:
            p_target = monte_carlo_probability(
                win_rate, avg_win_pct, avg_loss_pct, TARGET_PCT, ruin_pct
            )

    return {
        'days_of_data':    days_of_data,
        'total_trades':    total,
        'sample_quality':  'Sufficient' if total >= MIN_SAMPLE else f'Low ({total})',
        'win_rate':        round(win_rate, 1),
        'breakeven_wr':    round(breakeven_wr, 1),
        'wr_margin':       round(wr_margin, 1),
        'profit_factor':   profit_factor,
        'net_pnl':         round(net_pnl, 2),
        'avg_win':         round(avg_win, 2),
        'avg_loss':        round(-avg_loss, 2),   # shown negative
        'rr':              rr,
        'ev_per_trade':    round(ev, 2),
        'avg_trades_day':  avg_trades_per_day,
        'daily_ev':        daily_ev,
        'max_loss_streak': max_streak,
        'largest_win':     round(largest_win, 2),
        'largest_loss':    round(largest_loss, 2),
        'avg_dur_hrs':     avg_dur_hrs,
        'manual_pct':      manual_pct,
        'top_symbol':      top_symbol,
        'proj_days':       proj_days,
        'p_target':        p_target,
    }


# ── Sheet writer ──────────────────────────────────────────────────────────────
def ensure_metrics_sheet(db):
    existing = [ws.title for ws in db.worksheets()]
    if METRICS_SHEET not in existing:
        log(f"  Creating '{METRICS_SHEET}' tab in {SPREADSHEET_SOURCE}...")
        return db.add_worksheet(title=METRICS_SHEET, rows=200, cols=40)
    return db.worksheet(METRICS_SHEET)


def write_metrics_sheet(ws, trader_rows):
    """
    Clear the sheet and write data + glossary in one batch update.

    Layout:  [DATA_HEADERS row + trader rows] | [empty gap col] | [GLOSSARY]
    The glossary is always rewritten alongside the data.
    """
    ws.clear()

    data_rows = [DATA_HEADERS] + trader_rows
    num_rows  = max(len(data_rows), len(GLOSSARY))
    grid      = []

    for i in range(num_rows):
        data_part  = data_rows[i] if i < len(data_rows) else [''] * len(DATA_HEADERS)
        gloss_part = GLOSSARY[i]  if i < len(GLOSSARY)  else []
        grid.append(data_part + [''] + gloss_part)

    ws.update(grid, value_input_option='USER_ENTERED')
    log(f"  Wrote {len(trader_rows)} trader row(s) + glossary ({len(GLOSSARY)} rows) "
        f"to '{METRICS_SHEET}'.")


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    log('=' * 60)
    log('TraderMetrics — START')
    log('=' * 60)

    client  = get_gsheet_client()
    db      = client.open(SPREADSHEET_SOURCE)
    hist_wb = client.open(SPREADSHEET_HISTORY)

    log('Reading employee/account mapping...')
    acc_to_trader, _ = get_emp_acc_map(db)
    log(f'  {len(acc_to_trader)} mapped accounts')

    log('Reading account deposit sizes and drawdown limits...')
    account_info = get_account_info(db)
    log(f'  {len(account_info)} accounts read from Account sheet')

    # ── Collect closed trades per trader from deal history ─────────
    log('Reading deal history from STS Transaction History...')
    trader_trades    = {}   # trader_name -> list of trade dicts
    trader_accs_seen = {}   # trader_name -> set of account IDs that contributed trades
    unlinked_accs    = []   # account IDs with no Emp_Acc entry

    for ws in hist_wb.worksheets():
        acc_id = ws.title.strip()
        trader = acc_to_trader.get(acc_id, 'UNKNOWN TRADER')

        rows = ws.get_all_values()
        if len(rows) < 2:
            log(f'  {acc_id}: empty tab — skipping')
            continue

        count = 0
        for row in rows[1:]:
            if len(row) < 24:
                continue

            deal_type  = row[5].strip()   # BUY, SELL, BALANCE, CREDIT
            deal_entry = row[6].strip()   # ENTRY, EXIT, REVERSAL, CLOSE_BY

            # Only closed BUY/SELL trades
            if deal_type  not in ('BUY', 'SELL'):
                continue
            if deal_entry not in ('EXIT', 'CLOSE_BY', 'REVERSAL'):
                continue

            net_profit = parse_float_safe(row[23])
            if net_profit is None:
                continue

            duration_s = parse_float_safe(row[12])
            magic      = parse_float_safe(row[7])

            trader_trades.setdefault(trader, []).append({
                'net_profit': net_profit,
                'duration_s': int(duration_s) if duration_s is not None else 0,
                'magic':      int(magic)       if magic      is not None else 1,
                'symbol':     row[4].strip(),
                'date':       row[0].strip(),
            })
            count += 1

        trader_accs_seen.setdefault(trader, set()).add(acc_id)
        if trader == 'UNKNOWN TRADER':
            unlinked_accs.append(acc_id)

        log(f'  {acc_id} ({trader}): {count} closed trades')

    # ── Calculate metrics and build output rows ────────────────────
    log('Calculating metrics...')
    last_updated = datetime.now().strftime('%Y-%m-%d %H:%M')
    output_rows  = []

    # Sort: named traders alphabetically first, UNKNOWN TRADER last
    sorted_traders = sorted(
        trader_trades.keys(),
        key=lambda name: (name == 'UNKNOWN TRADER', name)
    )

    for trader in sorted_traders:
        trades = trader_trades[trader]
        accs   = sorted(trader_accs_seen.get(trader, set()))

        # Aggregate deposit size and use strictest drawdown across all accounts
        dep_values  = [account_info[a]['deposit_size']   for a in accs if a in account_info and account_info[a]['deposit_size']]
        draw_values = [account_info[a]['daily_drawdown'] for a in accs if a in account_info and account_info[a]['daily_drawdown']]

        total_dep = sum(dep_values) if dep_values else None
        ruin_pct  = min(draw_values) if draw_values else DEFAULT_RUIN_PCT

        notes = ''
        if trader == 'UNKNOWN TRADER':
            # UNKNOWN TRADER is a mix of unlinked accounts — metrics would be
            # misleading. Show N/A for everything and flag the account IDs.
            notes = f"⚠ No employee linked — assign in Emp_Acc: {', '.join(sorted(unlinked_accs))}"
            na    = 'N/A'
            output_rows.append([
                last_updated, trader, ', '.join(accs),
                na, na, na, na, na, na, na, na, na, na,
                na, na, na, na, na, na, na, na, na, na,
                na, na, notes,
            ])
            log(f"  UNKNOWN TRADER: {len(trades)} trades across {len(accs)} unlinked accounts — metrics suppressed")
            continue

        m = calculate_metrics(trades, total_dep, ruin_pct)
        if m is None:
            continue

        output_rows.append([
            last_updated,
            trader,
            ', '.join(accs),
            m['days_of_data'],
            m['total_trades'],
            m['sample_quality'],
            m['win_rate'],
            m['breakeven_wr'],
            m['wr_margin'],
            m['profit_factor'],
            m['net_pnl'],
            m['avg_win'],
            m['avg_loss'],
            m['rr'],
            m['ev_per_trade'],
            m['avg_trades_day'],
            m['daily_ev'],
            m['max_loss_streak'],
            m['largest_win'],
            m['largest_loss'],
            m['avg_dur_hrs'],
            m['manual_pct'],
            m['top_symbol'],
            m['proj_days'] if m['proj_days'] is not None else 'N/A',
            m['p_target']  if m['p_target']  is not None else 'N/A',
            notes,
        ])

        log(
            f"  {trader}: {m['total_trades']} trades | "
            f"WR {m['win_rate']}% (BEP {m['breakeven_wr']}%, margin {m['wr_margin']:+.1f}%) | "
            f"PF {m['profit_factor']} | EV/trade ${m['ev_per_trade']} | "
            f"P(target) {m['p_target']}%"
        )

    # ── Write to sheet ─────────────────────────────────────────────
    log(f"Writing to '{METRICS_SHEET}' in {SPREADSHEET_SOURCE}...")
    metrics_ws = ensure_metrics_sheet(db)
    write_metrics_sheet(metrics_ws, output_rows)

    log('=' * 60)
    log('TraderMetrics — DONE')
    log('=' * 60)


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        log_warn(f'Script failed: {e}')
        raise
