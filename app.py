import os
import json
import calendar
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from models import get_db, init_db
from csv_parser import parse_chase_csv

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production-j8k2m4')
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
init_db()


# ─── HELPERS ────────────────────────────────────────────────

def get_monday(d):
    return d - timedelta(days=d.weekday())

def build_category_filter(categories_param):
    if not categories_param:
        return '', []
    cats = [c.strip() for c in categories_param.split(',') if c.strip()]
    if not cats:
        return '', []
    placeholders = ','.join(['?' for _ in cats])
    return f' AND category IN ({placeholders})', cats

def get_all_categories(conn):
    rows = conn.execute(
        "SELECT DISTINCT category FROM transactions WHERE is_internal_transfer = 0 ORDER BY category"
    ).fetchall()
    return [r['category'] for r in rows]

def get_current_balance(conn):
    """
    Correct balance for Chase CSV (newest-first file order):
    1. Anchor = last non-null balance row by (posting_date DESC, id ASC).
       Within the same date, lower id was imported first from the file,
       meaning it is the most-recent transaction of that day in a newest-first CSV.
    2. Adjustment = sum of all blank-balance rows that are chronologically NEWER
       than the anchor (later date, or same date with a lower id).
    """
    row = conn.execute(
        'SELECT id, posting_date, balance FROM transactions WHERE balance IS NOT NULL '
        'ORDER BY posting_date DESC, id ASC LIMIT 1'
    ).fetchone()
    if not row:
        return 0.0
    anchor_id   = row['id']
    anchor_date = row['posting_date']
    anchor_bal  = row['balance']

    adj = conn.execute('''
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE balance IS NULL
          AND (posting_date > ? OR (posting_date = ? AND id < ?))
    ''', (anchor_date, anchor_date, anchor_id)).fetchone()[0]

    return round(anchor_bal + adj, 2)


def get_summary_stats(conn, start_date=None, end_date=None):
    df = ""
    p = []
    if start_date: df += " AND posting_date >= ?"; p.append(start_date)
    if end_date: df += " AND posting_date <= ?"; p.append(end_date)

    total_income = conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE amount > 0 AND is_internal_transfer=0{df}', p).fetchone()[0]
    total_expenses = abs(conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE amount < 0 AND is_internal_transfer=0{df}', p).fetchone()[0])
    net_savings = total_income - total_expenses
    current_balance = get_current_balance(conn)
    txn_count = conn.execute(f'SELECT COUNT(*) FROM transactions WHERE is_internal_transfer=0{df}', p).fetchone()[0]

    row = conn.execute(f'SELECT MIN(posting_date), MAX(posting_date) FROM transactions WHERE amount < 0 AND is_internal_transfer=0{df}', p).fetchone()
    if row[0] and row[1]:
        d1 = datetime.strptime(row[0], '%Y-%m-%d')
        d2 = datetime.strptime(row[1], '%Y-%m-%d')
        daily_avg_spend = total_expenses / max((d2 - d1).days, 1)
    else:
        daily_avg_spend = 0

    return {'total_income': total_income, 'total_expenses': total_expenses, 'net_savings': net_savings,
            'current_balance': current_balance, 'txn_count': txn_count, 'daily_avg_spend': daily_avg_spend}

def get_category_breakdown(conn, start_date=None, end_date=None):
    df = ""; p = []
    if start_date: df += " AND posting_date >= ?"; p.append(start_date)
    if end_date: df += " AND posting_date <= ?"; p.append(end_date)
    rows = conn.execute(f'SELECT category, COALESCE(SUM(ABS(amount)),0) as total, COUNT(*) as count FROM transactions WHERE amount < 0 AND is_internal_transfer=0{df} GROUP BY category ORDER BY total DESC', p).fetchall()
    return [dict(r) for r in rows]

def get_spending_trend(conn, months=6):
    rows = conn.execute('''SELECT strftime('%Y-%m', posting_date) as month,
        COALESCE(SUM(CASE WHEN amount > 0 AND is_internal_transfer=0 THEN amount ELSE 0 END),0) as income,
        COALESCE(SUM(CASE WHEN amount < 0 AND is_internal_transfer=0 THEN ABS(amount) ELSE 0 END),0) as expenses
        FROM transactions GROUP BY month ORDER BY month DESC LIMIT ?''', (months,)).fetchall()
    return [dict(r) for r in reversed(rows)]

def get_recurring_expenses(conn):
    rows = conn.execute('''SELECT category, description, COUNT(*) as occurrences,
        ROUND(AVG(ABS(amount)),2) as avg_amount, ROUND(SUM(ABS(amount)),2) as total_amount
        FROM transactions WHERE amount < 0 AND is_internal_transfer=0
        GROUP BY description HAVING COUNT(*) >= 2 ORDER BY total_amount DESC''').fetchall()
    return [dict(r) for r in rows]

# ─── KPI HELPERS ─────────────────────────────────────────────

def compute_rolling_4week_avg(conn):
    """Rolling 4-week average of the most recent 4 complete weeks.
    Used for daily/weekly projections."""
    today = date.today()
    cur_monday = get_monday(today)
    results = []
    for i in range(1, 5):
        mon = cur_monday - timedelta(weeks=i)
        sun = mon + timedelta(days=6)
        inc = conn.execute(
            'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
            (mon.isoformat(), sun.isoformat())
        ).fetchone()[0]
        spd = conn.execute(
            'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
            (mon.isoformat(), sun.isoformat())
        ).fetchone()[0]
        results.append({'income': inc, 'spend': spd, 'net': inc - spd})
    wi = sum(r['income'] for r in results) / 4
    ws = sum(r['spend'] for r in results) / 4
    wn = sum(r['net'] for r in results) / 4
    return {
        'avg_weekly': {'income': round(wi, 2),     'spend': round(ws, 2),     'net': round(wn, 2)},
        'avg_daily':  {'income': round(wi / 7, 2), 'spend': round(ws / 7, 2), 'net': round(wn / 7, 2)}
    }

def compute_rolling_3month_avg(conn):
    """Rolling 3-month average of the most recent 3 complete months.
    Used for monthly/quarterly/annual projections."""
    today = date.today()
    y, m = today.year, today.month
    results = []
    for _ in range(3):
        m -= 1
        if m == 0:
            m = 12; y -= 1
        s = date(y, m, 1).isoformat()
        e = date(y, m, calendar.monthrange(y, m)[1]).isoformat()
        inc = conn.execute(
            'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
            (s, e)
        ).fetchone()[0]
        spd = conn.execute(
            'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
            (s, e)
        ).fetchone()[0]
        results.append({'income': inc, 'spend': spd, 'net': inc - spd})
    mi = sum(r['income'] for r in results) / 3
    ms = sum(r['spend'] for r in results) / 3
    mn = sum(r['net'] for r in results) / 3
    return {
        'avg_monthly':   {'income': round(mi, 2),        'spend': round(ms, 2),        'net': round(mn, 2)},
        'avg_quarterly': {'income': round(mi * 3, 2),    'spend': round(ms * 3, 2),    'net': round(mn * 3, 2)},
        'avg_annual':    {'income': round(mi * 12, 2),   'spend': round(ms * 12, 2),   'net': round(mn * 12, 2)},
        'avg_daily':     {'income': round(mi / 30.44, 2),'spend': round(ms / 30.44, 2),'net': round(mn / 30.44, 2)},
        'avg_weekly':    {'income': round(mi / 4.33, 2), 'spend': round(ms / 4.33, 2), 'net': round(mn / 4.33, 2)}
    }

def compute_ytd_table_kpis(conn):
    """YTD-mode KPI table: all averages derived from YTD totals using calendar-day denominators."""
    today = date.today()
    ytd_start = date(today.year, 1, 1).isoformat()
    ytd_end   = today.isoformat()

    inc = conn.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
        (ytd_start, ytd_end)
    ).fetchone()[0]
    spd = conn.execute(
        'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
        (ytd_start, ytd_end)
    ).fetchone()[0]
    net = inc - spd

    days_elapsed   = (today - date(today.year, 1, 1)).days + 1
    days_in_year   = 366 if calendar.isleap(today.year) else 365
    elapsed_weeks  = days_elapsed / 7
    # Elapsed months: whole months before current + fraction of current month
    days_in_cur_month  = calendar.monthrange(today.year, today.month)[1]
    elapsed_months     = (today.month - 1) + today.day / days_in_cur_month
    elapsed_quarters   = elapsed_months / 3

    def per(total, divisor):
        return round(total / divisor, 2) if divisor > 0 else 0.0

    eoy_i = round(inc / days_elapsed * days_in_year, 2) if days_elapsed > 0 else 0.0
    eoy_s = round(spd / days_elapsed * days_in_year, 2) if days_elapsed > 0 else 0.0
    eoy_n = round(net / days_elapsed * days_in_year, 2) if days_elapsed > 0 else 0.0

    return {
        'income': {
            'avg_daily':     per(inc, days_elapsed),
            'avg_weekly':    per(inc, elapsed_weeks),
            'avg_monthly':   per(inc, elapsed_months),
            'avg_quarterly': per(inc, elapsed_quarters),
            'ytd_total':     round(inc, 2),
            'eoy_pace':      eoy_i,
        },
        'spend': {
            'avg_daily':     per(spd, days_elapsed),
            'avg_weekly':    per(spd, elapsed_weeks),
            'avg_monthly':   per(spd, elapsed_months),
            'avg_quarterly': per(spd, elapsed_quarters),
            'ytd_total':     round(spd, 2),
            'eoy_pace':      eoy_s,
        },
        'net': {
            'avg_daily':     per(net, days_elapsed),
            'avg_weekly':    per(net, elapsed_weeks),
            'avg_monthly':   per(net, elapsed_months),
            'avg_quarterly': per(net, elapsed_quarters),
            'ytd_total':     round(net, 2),
            'eoy_pace':      eoy_n,
        },
        'meta': {
            'days_elapsed':       days_elapsed,
            'days_in_year':       days_in_year,
            'elapsed_weeks':      round(elapsed_weeks, 2),
            'elapsed_months':     round(elapsed_months, 2),
            'elapsed_quarters':   round(elapsed_quarters, 2),
        }
    }


def compute_trailing_table_kpis(conn):
    """Trailing-mode KPI table: rolling-window averages using calendar-day denominators."""
    today = date.today()

    # Trailing 28 calendar days for daily avg
    t28_start = (today - timedelta(days=27)).isoformat()
    t28_end   = today.isoformat()
    inc28 = conn.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
        (t28_start, t28_end)
    ).fetchone()[0]
    spd28 = conn.execute(
        'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
        (t28_start, t28_end)
    ).fetchone()[0]
    net28 = inc28 - spd28

    r4w = compute_rolling_4week_avg(conn)   # weekly avg from 4 complete weeks
    r3m = compute_rolling_3month_avg(conn)  # monthly/quarterly/annual from 3 complete months

    return {
        'income': {
            'avg_daily':        round(inc28 / 28, 2),
            'avg_weekly':       r4w['avg_weekly']['income'],
            'avg_monthly':      r3m['avg_monthly']['income'],
            'avg_quarterly':    r3m['avg_quarterly']['income'],
            'annualized_trend': r3m['avg_annual']['income'],
        },
        'spend': {
            'avg_daily':        round(spd28 / 28, 2),
            'avg_weekly':       r4w['avg_weekly']['spend'],
            'avg_monthly':      r3m['avg_monthly']['spend'],
            'avg_quarterly':    r3m['avg_quarterly']['spend'],
            'annualized_trend': r3m['avg_annual']['spend'],
        },
        'net': {
            'avg_daily':        round(net28 / 28, 2),
            'avg_weekly':       r4w['avg_weekly']['net'],
            'avg_monthly':      r3m['avg_monthly']['net'],
            'avg_quarterly':    r3m['avg_quarterly']['net'],
            'annualized_trend': r3m['avg_annual']['net'],
        },
        'meta': {
            'trailing_daily_days':     28,
            'trailing_weekly_weeks':   4,
            'trailing_monthly_months': 3,
        }
    }

def get_kpi_settings(conn):
    try:
        row = conn.execute('SELECT monthly_net_target, target_cash_balance FROM kpi_settings WHERE id=1').fetchone()
        if row:
            return {'monthly_net_target': row['monthly_net_target'], 'target_cash_balance': row['target_cash_balance']}
    except Exception:
        try:
            row = conn.execute('SELECT monthly_net_target FROM kpi_settings WHERE id=1').fetchone()
            if row:
                return {'monthly_net_target': row['monthly_net_target'], 'target_cash_balance': None}
        except Exception:
            pass
    return {'monthly_net_target': None, 'target_cash_balance': None}


def compute_primary_target(conn, monthly_net_target, ytd_net, months_remaining):
    today = date.today()
    month_start = date(today.year, today.month, 1).isoformat()
    month_end = today.isoformat()
    cur_inc = conn.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
        (month_start, month_end)
    ).fetchone()[0]
    cur_spd = conn.execute(
        'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
        (month_start, month_end)
    ).fetchone()[0]
    cur_month_net = cur_inc - cur_spd
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_elapsed_in_month = today.day
    paced_month_net = round(cur_month_net / days_elapsed_in_month * days_in_month, 2) if days_elapsed_in_month > 0 else 0.0
    on_track = paced_month_net >= monthly_net_target
    implied_yearend_net = round(ytd_net + monthly_net_target * months_remaining, 2)
    return {
        'monthly_net_target':     round(monthly_net_target, 2),
        'current_month_net':      round(cur_month_net, 2),
        'paced_month_net':        paced_month_net,
        'on_track':               on_track,
        'days_elapsed_in_month':  days_elapsed_in_month,
        'days_in_month':          days_in_month,
        'implied_yearend_net':    implied_yearend_net,
    }


def compute_action_panel(monthly_net_target, ytd_net, trailing_monthly_net, months_remaining):
    gap = monthly_net_target - trailing_monthly_net  # positive = behind target
    if gap <= 0:
        feasibility = 'on_track'
        feasibility_label = 'On track'
    elif monthly_net_target > 0 and gap <= monthly_net_target * 0.25:
        feasibility = 'slight_stretch'
        feasibility_label = 'Slight stretch'
    else:
        feasibility = 'unlikely'
        feasibility_label = 'Unlikely at current pace'
    eoy_projected = round(ytd_net + trailing_monthly_net * months_remaining, 2)
    levers = {
        'income_only':    round(max(gap, 0), 2),
        'spend_only':     round(max(gap, 0), 2),
        'blended':        round(max(gap, 0) / 2, 2),
    }
    return {
        'gap':                   round(gap, 2),
        'feasibility':           feasibility,
        'feasibility_label':     feasibility_label,
        'eoy_projected':         eoy_projected,
        'breaks_even':           eoy_projected >= 0,
        'levers':                levers,
        'trailing_monthly_net':  round(trailing_monthly_net, 2),
        'monthly_net_target':    round(monthly_net_target, 2),
        'months_remaining':      round(months_remaining, 2),
    }


def compute_ytd_kpis(conn):
    """Year-to-date actuals + pace-based end-of-year projection."""
    today = date.today()
    ytd_start = date(today.year, 1, 1).isoformat()
    ytd_end   = today.isoformat()
    inc = conn.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
        (ytd_start, ytd_end)
    ).fetchone()[0]
    spd = conn.execute(
        'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
        (ytd_start, ytd_end)
    ).fetchone()[0]
    days_elapsed   = (today - date(today.year, 1, 1)).days + 1
    days_in_year   = 366 if calendar.isleap(today.year) else 365
    days_remaining = days_in_year - days_elapsed
    pace_i = inc / days_elapsed
    pace_s = spd / days_elapsed
    eoy_i  = inc + pace_i * days_remaining
    eoy_s  = spd + pace_s * days_remaining
    return {
        'year': today.year, 'income': round(inc, 2), 'spend': round(spd, 2), 'net': round(inc - spd, 2),
        'days_elapsed': days_elapsed, 'days_remaining': days_remaining,
        'projected_eoy_income': round(eoy_i, 2),
        'projected_eoy_spend':  round(eoy_s, 2),
        'projected_eoy_net':    round(eoy_i - eoy_s, 2)
    }

def compute_year_comparison(conn):
    """Current year YTD vs prior year same-date YTD. Ensures apples-to-apples comparison."""
    today = date.today()
    cur_yr = today.year
    pri_yr = cur_yr - 1

    # Match exact calendar date in prior year; handle Feb 29 in leap years
    try:
        pri_cutoff = date(pri_yr, today.month, today.day)
    except ValueError:
        pri_cutoff = date(pri_yr, today.month, today.day - 1)

    has_prior = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE posting_date LIKE ? AND is_internal_transfer=0",
        (f'{pri_yr}%',)
    ).fetchone()[0] > 0

    def ytd_by_month(yr, through_month, through_day):
        """Monthly breakdown for Jan through through_month (partial last month)."""
        result = []
        for mo in range(1, through_month + 1):
            s = date(yr, mo, 1).isoformat()
            e = date(yr, mo, through_day).isoformat() if mo == through_month \
                else date(yr, mo, calendar.monthrange(yr, mo)[1]).isoformat()
            mi = conn.execute('SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0', (s, e)).fetchone()[0]
            ms = conn.execute('SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0', (s, e)).fetchone()[0]
            result.append({'month': mo, 'label': datetime(yr, mo, 1).strftime('%b'),
                           'income': round(mi, 2), 'spend': round(ms, 2), 'net': round(mi - ms, 2)})
        total_i = sum(m['income'] for m in result)
        total_s = sum(m['spend'] for m in result)
        return {'year': yr, 'income': round(total_i, 2), 'spend': round(total_s, 2),
                'net': round(total_i - total_s, 2), 'by_month': result}

    cur_data = ytd_by_month(cur_yr, today.month, today.day)
    pri_data = ytd_by_month(pri_yr, pri_cutoff.month, pri_cutoff.day) if has_prior else None

    return {
        'current_year':    cur_data,
        'prior_year':      pri_data,
        'has_prior_year':  has_prior,
        'comparison_label': f'YTD through {today.strftime("%b")} {today.day} — both years'
    }


def get_available_weeks(conn):
    row = conn.execute('SELECT MIN(posting_date), MAX(posting_date) FROM transactions WHERE is_internal_transfer=0').fetchone()
    if not row[0]: return []
    min_d = datetime.strptime(row[0], '%Y-%m-%d').date()
    max_d = datetime.strptime(row[1], '%Y-%m-%d').date()
    weeks = []
    mon = get_monday(max_d)
    earliest = get_monday(min_d)
    while mon >= earliest:
        sun = mon + timedelta(days=6)
        weeks.append({'start': mon.isoformat(), 'end': sun.isoformat(),
                      'label': f"{mon.strftime('%b %d')} - {sun.strftime('%b %d')}"})
        mon -= timedelta(days=7)
    return weeks

def get_week_daily_data(conn, start_date, end_date, categories_filter=''):
    cat_sql, cat_params = build_category_filter(categories_filter)
    start = datetime.strptime(start_date, '%Y-%m-%d').date()
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        ds = d.isoformat()
        pm = [ds] + cat_params
        inc = conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date=? AND amount > 0 AND is_internal_transfer=0{cat_sql}', pm).fetchone()[0]
        exp = conn.execute(f'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date=? AND amount < 0 AND is_internal_transfer=0{cat_sql}', pm).fetchone()[0]
        days.append({'date': ds, 'day_label': d.strftime('%a'), 'day_num': d.day, 'income': round(inc,2), 'spend': round(exp,2)})
    return days

def get_week_transactions(conn, start_date, end_date, mode='all', categories_filter='', selected_date=None):
    cat_sql, cat_params = build_category_filter(categories_filter)
    p = []
    if selected_date:
        dsql = ' AND posting_date = ?'; p.append(selected_date)
    else:
        dsql = ' AND posting_date >= ? AND posting_date <= ?'; p.extend([start_date, end_date])
    msql = ''
    if mode == 'income': msql = ' AND amount > 0'
    elif mode == 'spend': msql = ' AND amount < 0'
    p.extend(cat_params)
    rows = conn.execute(f'SELECT id, posting_date, description, category, amount, tags FROM transactions WHERE is_internal_transfer=0{dsql}{msql}{cat_sql} ORDER BY posting_date DESC, id DESC', p).fetchall()
    return [dict(r) for r in rows]

def get_available_months(conn):
    rows = conn.execute("SELECT DISTINCT strftime('%Y-%m', posting_date) as month FROM transactions WHERE is_internal_transfer=0 ORDER BY month DESC").fetchall()
    result = []
    for r in rows:
        y, m = r['month'].split('-')
        result.append({'value': r['month'], 'label': datetime(int(y), int(m), 1).strftime('%B %Y')})
    return result

def get_month_daily_data(conn, year, month, categories_filter=''):
    cat_sql, cat_params = build_category_filter(categories_filter)
    num_days = calendar.monthrange(year, month)[1]
    days = []
    for day in range(1, num_days + 1):
        d = date(year, month, day)
        ds = d.isoformat()
        pm = [ds] + cat_params
        inc = conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date=? AND amount > 0 AND is_internal_transfer=0{cat_sql}', pm).fetchone()[0]
        exp = conn.execute(f'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date=? AND amount < 0 AND is_internal_transfer=0{cat_sql}', pm).fetchone()[0]
        days.append({'date': ds, 'day': day, 'weekday': d.weekday(), 'day_label': d.strftime('%a'), 'income': round(inc,2), 'spend': round(exp,2)})
    return days

def get_month_transactions(conn, year, month, mode='all', categories_filter='', selected_date=None):
    cat_sql, cat_params = build_category_filter(categories_filter)
    p = []
    if selected_date:
        dsql = ' AND posting_date = ?'; p.append(selected_date)
    else:
        start = date(year, month, 1).isoformat()
        end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        dsql = ' AND posting_date >= ? AND posting_date <= ?'; p.extend([start, end])
    msql = ''
    if mode == 'income': msql = ' AND amount > 0'
    elif mode == 'spend': msql = ' AND amount < 0'
    p.extend(cat_params)
    rows = conn.execute(f'SELECT id, posting_date, description, category, amount, tags FROM transactions WHERE is_internal_transfer=0{dsql}{msql}{cat_sql} ORDER BY posting_date DESC, id DESC', p).fetchall()
    return [dict(r) for r in rows]


# ─── PAGE ROUTES ────────────────────────────────────────────

@app.route('/')
def dashboard():
    return render_template('dashboard.html')

@app.route('/weekly')
def weekly():
    return render_template('weekly.html')

@app.route('/kpis')
def kpis():
    return render_template('kpis.html')

@app.route('/monthly')
def monthly():
    return render_template('monthly.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        if 'csv_file' not in request.files:
            flash('No file selected', 'error'); return redirect(url_for('upload'))
        file = request.files['csv_file']
        if file.filename == '' or not file.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file', 'error'); return redirect(url_for('upload'))
        result = parse_chase_csv(file.read(), file.filename)
        flash(f"Import complete: {result['imported']} new, {result['skipped']} duplicates skipped.", 'success')
        if result['errors']: flash(f"{len(result['errors'])} rows had errors.", 'warning')
        return redirect(url_for('dashboard'))
    return render_template('upload.html')

@app.route('/transactions')
def transactions():
    conn = get_db()
    category = request.args.get('category', ''); txn_type = request.args.get('type', '')
    search = request.args.get('search', ''); start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', ''); min_amount = request.args.get('min_amount', '')
    max_amount = request.args.get('max_amount', ''); tag = request.args.get('tag', '')
    sort = request.args.get('sort', 'date_desc')

    q = 'SELECT * FROM transactions WHERE 1=1'; p = []
    if category: q += ' AND category = ?'; p.append(category)
    if txn_type == 'income': q += ' AND amount > 0'
    elif txn_type == 'expense': q += ' AND amount < 0'
    if search: q += ' AND description LIKE ?'; p.append(f'%{search}%')
    if start_date: q += ' AND posting_date >= ?'; p.append(start_date)
    if end_date: q += ' AND posting_date <= ?'; p.append(end_date)
    if min_amount: q += ' AND ABS(amount) >= ?'; p.append(float(min_amount))
    if max_amount: q += ' AND ABS(amount) <= ?'; p.append(float(max_amount))
    if tag: q += ' AND tags LIKE ?'; p.append(f'%{tag}%')
    sm = {'date_desc':'posting_date DESC,id DESC','date_asc':'posting_date ASC,id ASC',
          'amount_desc':'ABS(amount) DESC','amount_asc':'ABS(amount) ASC','category':'category ASC,posting_date DESC'}
    q += f' ORDER BY {sm.get(sort,"posting_date DESC,id DESC")}'
    rows = conn.execute(q, p).fetchall(); txn_list = [dict(r) for r in rows]
    cats = [r['category'] for r in conn.execute('SELECT DISTINCT category FROM transactions ORDER BY category').fetchall()]
    fi = sum(t['amount'] for t in txn_list if t['amount'] > 0 and not t['is_internal_transfer'])
    fe = sum(abs(t['amount']) for t in txn_list if t['amount'] < 0 and not t['is_internal_transfer'])
    conn.close()
    return render_template('transactions.html', transactions=txn_list, categories=cats,
        filters={'category':category,'type':txn_type,'search':search,'start_date':start_date,
                 'end_date':end_date,'min_amount':min_amount,'max_amount':max_amount,'tag':tag,'sort':sort},
        filtered_income=fi, filtered_expenses=fe, result_count=len(txn_list))

@app.route('/transactions/<int:txn_id>/update', methods=['POST'])
def update_transaction(txn_id):
    conn = get_db()
    conn.execute('UPDATE transactions SET category=?, tags=?, notes=? WHERE id=?',
        (request.form.get('category',''), request.form.get('tags',''), request.form.get('notes',''), txn_id))
    conn.commit(); conn.close(); flash('Transaction updated', 'success')
    return redirect(request.referrer or url_for('transactions'))

@app.route('/goals', methods=['GET', 'POST'])
def goals():
    conn = get_db()
    if request.method == 'POST':
        conn.execute('INSERT INTO savings_goals (name, target_amount, current_amount, target_date, goal_type) VALUES (?,?,?,?,?)',
            (request.form.get('name','').strip(), float(request.form.get('target_amount',0)),
             float(request.form.get('current_amount',0)), request.form.get('target_date','') or None,
             request.form.get('goal_type','custom')))
        conn.commit(); flash(f'Goal created!', 'success'); return redirect(url_for('goals'))

    goals_list = [dict(r) for r in conn.execute('SELECT * FROM savings_goals ORDER BY is_active DESC, created_at DESC').fetchall()]
    today = datetime.now(); stats = get_summary_stats(conn); trend = get_spending_trend(conn)
    for g in goals_list:
        rem = g['target_amount'] - g['current_amount']; g['remaining'] = rem
        g['progress_pct'] = min(round((g['current_amount']/g['target_amount'])*100,1),100) if g['target_amount']>0 else 0
        if g['target_date']:
            tgt = datetime.strptime(g['target_date'],'%Y-%m-%d')
            ml = max((tgt.year-today.year)*12+tgt.month-today.month,1)
            g['monthly_needed']=rem/ml; g['months_left']=ml; g['quarterly_needed']=g['monthly_needed']*3; g['yearly_needed']=g['monthly_needed']*12
            if trend:
                ae = sum(t['expenses'] for t in trend)/max(len(trend),1)
                g['projected_monthly_income']=ae+g['monthly_needed']; g['projected_quarterly_income']=g['projected_monthly_income']*3; g['projected_yearly_income']=g['projected_monthly_income']*12
            else:
                g['projected_monthly_income']=g['monthly_needed']; g['projected_quarterly_income']=g['monthly_needed']*3; g['projected_yearly_income']=g['monthly_needed']*12
            if g['progress_pct']>=100: g['status']='complete'
            elif g['monthly_needed']<=stats.get('net_savings',0): g['status']='on_track'
            else: g['status']='behind'
        else:
            g['monthly_needed']=None; g['months_left']=None; g['status']='no_date'; g['projected_monthly_income']=None
    conn.close()
    return render_template('goals.html', goals=goals_list)

@app.route('/goals/<int:goal_id>/update', methods=['POST'])
def update_goal(goal_id):
    conn = get_db(); conn.execute('UPDATE savings_goals SET current_amount=? WHERE id=?', (float(request.form.get('current_amount',0)), goal_id))
    conn.commit(); conn.close(); flash('Goal updated!', 'success'); return redirect(url_for('goals'))

@app.route('/goals/<int:goal_id>/delete', methods=['POST'])
def delete_goal(goal_id):
    conn = get_db(); conn.execute('DELETE FROM savings_goals WHERE id=?', (goal_id,)); conn.commit(); conn.close()
    flash('Goal deleted', 'success'); return redirect(url_for('goals'))

@app.route('/rules', methods=['GET', 'POST'])
def rules():
    conn = get_db()
    if request.method == 'POST':
        kw = request.form.get('keyword','').strip(); cat = request.form.get('category','').strip()
        pri = int(request.form.get('priority',100))
        if kw and cat:
            conn.execute('INSERT INTO category_rules (keyword, category, priority) VALUES (?,?,?)', (kw, cat, pri)); conn.commit()
            conn.execute("UPDATE transactions SET category=? WHERE category='Uncategorized' AND UPPER(description) LIKE UPPER(?)", (cat, f'%{kw}%')); conn.commit()
            flash(f'Rule added: "{kw}" → {cat}', 'success')
        return redirect(url_for('rules'))
    rl = [dict(r) for r in conn.execute('SELECT * FROM category_rules ORDER BY priority ASC, keyword ASC').fetchall()]
    conn.close(); return render_template('rules.html', rules=rl)

@app.route('/rules/<int:rule_id>/delete', methods=['POST'])
def delete_rule(rule_id):
    conn = get_db(); conn.execute('DELETE FROM category_rules WHERE id=?', (rule_id,)); conn.commit(); conn.close()
    flash('Rule deleted', 'success'); return redirect(url_for('rules'))

# ─── JSON API ───────────────────────────────────────────────

@app.route('/api/categories')
def api_categories():
    conn = get_db(); cats = get_all_categories(conn); conn.close()
    return jsonify({'categories': cats})

@app.route('/api/weekly/weeks')
def api_weekly_weeks():
    conn = get_db(); mode = request.args.get('mode','spend'); cf = request.args.get('categories','')
    weeks = get_available_weeks(conn); cat_sql, cat_params = build_category_filter(cf)
    for w in weeks:
        pi = [w['start'], w['end']] + cat_params; pe = [w['start'], w['end']] + cat_params
        w['income'] = round(conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0{cat_sql}', pi).fetchone()[0], 2)
        w['spend'] = round(conn.execute(f'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0{cat_sql}', pe).fetchone()[0], 2)
        w['total'] = w['income'] if mode=='income' else w['spend'] if mode=='spend' else w['income']+w['spend']
        w['daily'] = get_week_daily_data(conn, w['start'], w['end'], cf)
    conn.close(); return jsonify({'weeks': weeks})

@app.route('/api/weekly/detail')
def api_weekly_detail():
    conn = get_db(); sd = request.args.get('start',''); ed = request.args.get('end','')
    mode = request.args.get('mode','spend'); cf = request.args.get('categories','')
    sel = request.args.get('selected_date','')
    if not sd or not ed:
        t = date.today(); m = get_monday(t); sd = m.isoformat(); ed = (m+timedelta(days=6)).isoformat()
    daily = get_week_daily_data(conn, sd, ed, cf)
    txns = get_week_transactions(conn, sd, ed, mode, cf, sel if sel else None)
    cat_sql, cat_params = build_category_filter(cf)
    pi = [sd, ed]+cat_params; pe = [sd, ed]+cat_params
    ti = round(conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0{cat_sql}', pi).fetchone()[0], 2)
    ts = round(conn.execute(f'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0{cat_sql}', pe).fetchone()[0], 2)
    dwi = sum(1 for d in daily if d['income']>0); dws = sum(1 for d in daily if d['spend']>0)
    ai = round(ti/max(dwi,1),2); ase = round(ts/max(dws,1),2)
    cats = get_all_categories(conn); conn.close()
    return jsonify({'start':sd,'end':ed,'daily':daily,'transactions':txns,'total_income':ti,'total_spend':ts,'avg_income':ai,'avg_spend':ase,'categories':cats})

@app.route('/api/monthly/months')
def api_monthly_months():
    conn = get_db(); ms = get_available_months(conn); conn.close(); return jsonify({'months': ms})

@app.route('/api/monthly/detail')
def api_monthly_detail():
    conn = get_db(); ms = request.args.get('month',''); mode = request.args.get('mode','spend')
    cf = request.args.get('categories',''); sel = request.args.get('selected_date','')
    if not ms: ms = date.today().strftime('%Y-%m')
    y, m = ms.split('-'); year, month = int(y), int(m)
    nd = calendar.monthrange(year, month)[1]; s = date(year, month, 1).isoformat(); e = date(year, month, nd).isoformat()
    daily = get_month_daily_data(conn, year, month, cf)
    txns = get_month_transactions(conn, year, month, mode, cf, sel if sel else None)
    cat_sql, cat_params = build_category_filter(cf)
    pi = [s,e]+cat_params; pe = [s,e]+cat_params
    ti = round(conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0{cat_sql}', pi).fetchone()[0], 2)
    ts = round(conn.execute(f'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0{cat_sql}', pe).fetchone()[0], 2)
    dwd = sum(1 for d in daily if d['income']>0 or d['spend']>0)
    ai = round(ti/max(dwd,1),2); ase = round(ts/max(dwd,1),2)
    fw = date(year, month, 1).weekday(); cats = get_all_categories(conn); conn.close()
    return jsonify({'month':ms,'label':datetime(year,month,1).strftime('%B %Y'),'num_days':nd,'first_weekday':fw,
        'daily':daily,'transactions':txns,'total_income':ti,'total_spend':ts,'avg_income':ai,'avg_spend':ase,'categories':cats})

@app.route('/api/kpis')
def api_kpis():
    conn = get_db()
    mode = request.args.get('mode', 'ytd')  # ytd | trailing

    has_data = conn.execute("SELECT COUNT(*) FROM transactions WHERE is_internal_transfer=0").fetchone()[0] > 0
    ytd      = compute_ytd_kpis(conn)
    compare  = compute_year_comparison(conn)
    settings = get_kpi_settings(conn)

    table_kpis     = None
    primary_target = None
    action_panel   = None

    if has_data:
        table_kpis = compute_ytd_table_kpis(conn) if mode == 'ytd' else compute_trailing_table_kpis(conn)

        # Always need trailing monthly net for Action Panel / target card
        if mode == 'trailing':
            trailing_kpis = table_kpis
        else:
            trailing_kpis = compute_trailing_table_kpis(conn)
        trailing_monthly_net = trailing_kpis['net']['avg_monthly']

        # Months remaining in year (fractional)
        today = date.today()
        days_in_cur_month = calendar.monthrange(today.year, today.month)[1]
        elapsed_months    = (today.month - 1) + today.day / days_in_cur_month
        months_remaining  = 12 - elapsed_months

        # Resolve monthly net target (user-set or break-even fallback)
        mnt = settings['monthly_net_target']
        is_default_target = mnt is None
        if is_default_target:
            mnt = max(0.0, round(-ytd['net'] / months_remaining, 2)) if months_remaining > 0 else 0.0

        primary_target = compute_primary_target(conn, mnt, ytd['net'], months_remaining)
        primary_target['is_default_target'] = is_default_target

        action_panel = compute_action_panel(mnt, ytd['net'], trailing_monthly_net, months_remaining)

    conn.close()
    return jsonify({
        'mode':           mode,
        'has_data':       has_data,
        'table_kpis':     table_kpis,
        'ytd':            ytd,
        'compare_years':  compare,
        'settings':       settings,
        'primary_target': primary_target,
        'action_panel':   action_panel,
    })


@app.route('/api/kpis/settings', methods=['POST'])
def api_kpis_settings():
    data = request.get_json() or {}
    mnt  = data.get('monthly_net_target')  # float or None (None = reset to default)
    conn = get_db()
    # Use UPDATE to preserve target_cash_balance
    conn.execute(
        'UPDATE kpi_settings SET monthly_net_target=?, updated_at=CURRENT_TIMESTAMP WHERE id=1',
        (mnt,)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'monthly_net_target': mnt})


# ─── DASHBOARD V2 HELPERS ────────────────────────────────────

def resolve_horizon(horizon, start_param, end_param):
    """Returns (start_date_str, end_date_str, days_count, label)."""
    today = date.today()
    if horizon == '7d':
        s = today - timedelta(days=6)
        return s.isoformat(), today.isoformat(), 7, 'Last 7 Days'
    elif horizon == '30d':
        s = today - timedelta(days=29)
        return s.isoformat(), today.isoformat(), 30, 'Last 30 Days'
    elif horizon == '90d':
        s = today - timedelta(days=89)
        return s.isoformat(), today.isoformat(), 90, 'Last 90 Days'
    elif horizon == 'mtd':
        s = date(today.year, today.month, 1)
        days = today.day
        return s.isoformat(), today.isoformat(), max(days, 1), 'Month to Date'
    elif horizon == 'ytd':
        s = date(today.year, 1, 1)
        days = (today - s).days + 1
        return s.isoformat(), today.isoformat(), max(days, 1), 'Year to Date'
    elif horizon == 'custom' and start_param and end_param:
        try:
            d1 = date.fromisoformat(start_param)
            d2 = date.fromisoformat(end_param)
            days = (d2 - d1).days + 1
            label = f'{d1.strftime("%b %d")} \u2013 {d2.strftime("%b %d, %Y")}'
            return start_param, end_param, max(days, 1), label
        except Exception:
            pass
    # Default: last 30 days
    s = today - timedelta(days=29)
    return s.isoformat(), today.isoformat(), 30, 'Last 30 Days'


def trend_bucket(horizon, days):
    if horizon in ('7d', '30d'):
        return 'daily'
    elif horizon == '90d':
        return 'weekly'
    elif horizon == 'ytd':
        return 'monthly'
    else:  # custom
        if days <= 31:
            return 'daily'
        elif days <= 90:
            return 'weekly'
        return 'monthly'


def get_dashboard_trend(conn, start_date, end_date, bucket):
    """Return bucketed trend data with one SQL query per bucket type."""
    from collections import defaultdict
    start = date.fromisoformat(start_date)
    end   = date.fromisoformat(end_date)

    # Single query fetching all days in range
    rows = conn.execute('''
        SELECT posting_date,
               COALESCE(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),0) AS income,
               COALESCE(SUM(CASE WHEN amount<0 THEN ABS(amount) ELSE 0 END),0) AS spend
        FROM transactions
        WHERE posting_date>=? AND posting_date<=? AND is_internal_transfer=0
        GROUP BY posting_date ORDER BY posting_date
    ''', (start_date, end_date)).fetchall()

    if bucket == 'daily':
        row_map = {r['posting_date']: (r['income'], r['spend']) for r in rows}
        data = []
        cur = start
        while cur <= end:
            ds = cur.isoformat()
            inc, spd = row_map.get(ds, (0, 0))
            data.append({'label': cur.strftime('%b %d'), 'income': round(inc, 2),
                         'spend': round(spd, 2), 'net': round(inc - spd, 2)})
            cur += timedelta(days=1)
        return data

    elif bucket == 'weekly':
        week_data = defaultdict(lambda: {'income': 0.0, 'spend': 0.0})
        for r in rows:
            d = date.fromisoformat(r['posting_date'])
            ws = get_monday(d)
            week_data[ws]['income'] += r['income']
            week_data[ws]['spend']  += r['spend']
        data = []
        for ws in sorted(week_data.keys()):
            inc = week_data[ws]['income']
            spd = week_data[ws]['spend']
            data.append({'label': ws.strftime('%b %d'), 'income': round(inc, 2),
                         'spend': round(spd, 2), 'net': round(inc - spd, 2)})
        return data

    else:  # monthly
        month_data = defaultdict(lambda: {'income': 0.0, 'spend': 0.0})
        for r in rows:
            mo = r['posting_date'][:7]  # YYYY-MM
            month_data[mo]['income'] += r['income']
            month_data[mo]['spend']  += r['spend']
        data = []
        for mo in sorted(month_data.keys()):
            y, m = mo.split('-')
            inc = month_data[mo]['income']
            spd = month_data[mo]['spend']
            label = datetime(int(y), int(m), 1).strftime('%b %Y')
            data.append({'label': label, 'income': round(inc, 2),
                         'spend': round(spd, 2), 'net': round(inc - spd, 2)})
        return data


def get_comparison_periods(horizon, start_date, end_date, days):
    """Returns (prior_start, prior_end, label)."""
    today = date.today()
    s = date.fromisoformat(start_date)

    if horizon in ('7d', '30d', '90d', 'custom'):
        prior_end   = (s - timedelta(days=1)).isoformat()
        prior_start = (s - timedelta(days=days)).isoformat()
        label = f'Prior {days} Days'
        return prior_start, prior_end, label

    elif horizon == 'mtd':
        elapsed = today.day
        pm_m = today.month - 1 if today.month > 1 else 12
        pm_y = today.year if today.month > 1 else today.year - 1
        pm_total = calendar.monthrange(pm_y, pm_m)[1]
        prior_start = date(pm_y, pm_m, 1).isoformat()
        prior_end   = date(pm_y, pm_m, min(elapsed, pm_total)).isoformat()
        label = f'{date(pm_y, pm_m, 1).strftime("%b %Y")} (day 1\u2013{min(elapsed, pm_total)})'
        return prior_start, prior_end, label

    else:  # ytd
        py = today.year - 1
        try:
            pe = date(py, today.month, today.day)
        except ValueError:
            pe = date(py, today.month, calendar.monthrange(py, today.month)[1])
        return date(py, 1, 1).isoformat(), pe.isoformat(), f'YTD {py}'


def compute_monthly_goal_tracking(conn, monthly_net_target, avg_3mo_daily_spend=None):
    """Always based on current calendar month, not the selected horizon."""
    today = date.today()
    month_start = date(today.year, today.month, 1).isoformat()
    month_end   = today.isoformat()
    days_in_month  = calendar.monthrange(today.year, today.month)[1]
    days_elapsed   = today.day
    days_remaining = days_in_month - days_elapsed

    cur_inc = conn.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
        (month_start, month_end)
    ).fetchone()[0]
    cur_spd = conn.execute(
        'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
        (month_start, month_end)
    ).fetchone()[0]
    actual_net = cur_inc - cur_spd

    avg_daily_spend_month = cur_spd / days_elapsed if days_elapsed > 0 else 0.0
    avg_daily_net_month   = actual_net / days_elapsed if days_elapsed > 0 else 0.0
    projected_eom_net     = round(avg_daily_net_month * days_in_month, 2)

    # Improved spend estimate: min(MTD pace, 3-month baseline) to dampen early-month spikes
    if avg_3mo_daily_spend is not None and avg_3mo_daily_spend > 0:
        estimated_daily_spend = min(avg_daily_spend_month, avg_3mo_daily_spend)
    else:
        estimated_daily_spend = avg_daily_spend_month

    goal          = None
    progress_pct  = None
    expected_net  = None
    pace_status   = None
    req_daily_net = None
    req_daily_inc = None
    pace_gap       = None

    if monthly_net_target is not None:
        goal = round(monthly_net_target, 2)
        progress_pct  = round(actual_net / monthly_net_target * 100, 1) if monthly_net_target != 0 else None
        expected_net  = round(monthly_net_target * days_elapsed / days_in_month, 2)
        pace_gap      = round(actual_net - expected_net, 2)
        if days_remaining > 0:
            req_daily_net = round((monthly_net_target - actual_net) / days_remaining, 2)
            proj_rem_spd  = estimated_daily_spend * days_remaining
            req_daily_inc = round((max(0.0, monthly_net_target - actual_net) + proj_rem_spd) / days_remaining, 2)
        # Pace status: ahead if actual >= expected × 1.05, near >= 0.85, else behind
        if expected_net is not None:
            if actual_net >= expected_net * 1.05:
                pace_status = 'ahead'
            elif actual_net >= expected_net * 0.85:
                pace_status = 'near'
            else:
                pace_status = 'behind'

    return {
        'goal':                    goal,
        'actual_net':              round(actual_net, 2),
        'actual_income':           round(cur_inc, 2),
        'actual_spend':            round(cur_spd, 2),
        'progress_pct':            progress_pct,
        'expected_net_today':      expected_net,
        'pace_gap':                pace_gap,
        'pace_status':             pace_status,
        'projected_eom_net':       projected_eom_net,
        'required_daily_net':      req_daily_net,
        'required_daily_income':   req_daily_inc,
        'days_elapsed':            days_elapsed,
        'days_in_month':           days_in_month,
        'days_remaining':          days_remaining,
        'avg_daily_spend_month':   round(avg_daily_spend_month, 2),
        'estimated_daily_spend':   round(estimated_daily_spend, 2),
        'month_label':             today.strftime('%B %Y'),
    }


@app.route('/api/dashboard')
def api_dashboard():
    conn = get_db()
    horizon     = request.args.get('horizon', '30d')
    start_param = request.args.get('start', '')
    end_param   = request.args.get('end', '')

    start_date, end_date, days, period_label = resolve_horizon(horizon, start_param, end_param)

    # ── KPIs
    income = conn.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
        (start_date, end_date)
    ).fetchone()[0]
    spend = conn.execute(
        'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
        (start_date, end_date)
    ).fetchone()[0]
    net          = income - spend
    savings_rate = round(net / income * 100, 1) if income > 0 else 0.0

    # ── Daily pace
    avg_daily_income = round(income / days, 2)
    avg_daily_spend  = round(spend  / days, 2)
    avg_daily_net    = round(net    / days, 2)
    savings_velocity_monthly = round(avg_daily_net * 30, 2)

    # ── Cash position  (current balance never changes with horizon)
    current_balance = get_current_balance(conn)

    # Net change = ending_balance - starting_balance over a fixed 30-day window
    # Anchored to max(posting_date) in dataset, not system clock
    ref_row  = conn.execute('SELECT MAX(posting_date) FROM transactions').fetchone()
    ref_date = date.fromisoformat(ref_row[0]) if ref_row[0] else date.today()
    l30_end   = ref_date.isoformat()
    l30_start = (ref_date - timedelta(days=29)).isoformat()

    # Implied balance just before the window start: last non-null balance before l30_start
    # plus any blank-balance transactions between that anchor and l30_start
    pre_anchor = conn.execute(
        'SELECT id, posting_date, balance FROM transactions '
        'WHERE balance IS NOT NULL AND posting_date < ? '
        'ORDER BY posting_date DESC, id ASC LIMIT 1',
        (l30_start,)
    ).fetchone()
    if pre_anchor:
        pre_adj = conn.execute(
            'SELECT COALESCE(SUM(amount),0) FROM transactions '
            'WHERE balance IS NULL AND posting_date < ? '
            '  AND (posting_date > ? OR (posting_date = ? AND id < ?))',
            (l30_start, pre_anchor['posting_date'], pre_anchor['posting_date'], pre_anchor['id'])
        ).fetchone()[0]
        starting_balance = round(pre_anchor['balance'] + pre_adj, 2)
    else:
        starting_balance = 0.0

    net_change = round(current_balance - starting_balance, 2)

    r3m = compute_rolling_3month_avg(conn)
    avg_monthly_spend = r3m['avg_monthly']['spend']
    runway_months     = round(current_balance / avg_monthly_spend, 1) if avg_monthly_spend > 0 else None
    runway_days       = round(current_balance / avg_monthly_spend * 30, 1) if avg_monthly_spend > 0 else None
    avg_3mo_daily_spend = round(avg_monthly_spend / 30, 2) if avg_monthly_spend > 0 else 0.0

    # ── Forecast: weighted (70% last-30-day trend, 30% selected-horizon baseline)
    projected_monthly_income = round(avg_daily_income * 30, 2)
    projected_monthly_spend  = round(avg_daily_spend  * 30, 2)
    today_dt     = date.today()
    recent_start = (today_dt - timedelta(days=29)).isoformat()
    recent_end   = today_dt.isoformat()
    recent_inc = conn.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
        (recent_start, recent_end)
    ).fetchone()[0]
    recent_spd = conn.execute(
        'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
        (recent_start, recent_end)
    ).fetchone()[0]
    daily_net_recent   = (recent_inc - recent_spd) / 30
    forecast_daily_net = round(0.7 * daily_net_recent + 0.3 * avg_daily_net, 2)
    projected_monthly_net = round(forecast_daily_net * 30, 2)
    projected_annual_net  = round(forecast_daily_net * 365, 2)

    # ── Trend chart
    bucket     = trend_bucket(horizon, days)
    trend_data = get_dashboard_trend(conn, start_date, end_date, bucket)

    # ── Comparison
    prior_start, prior_end, prior_label = get_comparison_periods(horizon, start_date, end_date, days)
    prior_income = conn.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount>0 AND is_internal_transfer=0',
        (prior_start, prior_end)
    ).fetchone()[0]
    prior_spend = conn.execute(
        'SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0',
        (prior_start, prior_end)
    ).fetchone()[0]
    prior_net = prior_income - prior_spend

    # ── Spending breakdown (top 5 expense categories)
    breakdown_rows = conn.execute(
        '''SELECT category, SUM(ABS(amount)) as amount, COUNT(*) as count
           FROM transactions WHERE posting_date>=? AND posting_date<=? AND amount<0 AND is_internal_transfer=0
           GROUP BY category ORDER BY amount DESC LIMIT 5''',
        (start_date, end_date)
    ).fetchall()
    spending_breakdown = [{'category': r['category'], 'amount': round(r['amount'], 2), 'count': r['count']} for r in breakdown_rows]

    # ── Balance sparkline (last 30 days, reuse cash-position window)
    bal_spark_rows = conn.execute(
        'SELECT balance FROM transactions WHERE balance IS NOT NULL AND posting_date>=? AND posting_date<=? ORDER BY posting_date ASC, id ASC',
        (l30_start, l30_end)
    ).fetchall()
    balance_sparkline = [r['balance'] for r in bal_spark_rows]

    # ── Monthly goal tracking (always current calendar month)
    settings     = get_kpi_settings(conn)
    monthly_goal = compute_monthly_goal_tracking(conn, settings.get('monthly_net_target'), avg_3mo_daily_spend)

    conn.close()
    return jsonify({
        'horizon': horizon,
        'period': {
            'start': start_date, 'end': end_date,
            'days': days, 'label': period_label,
        },
        'kpis': {
            'income': round(income, 2), 'spend': round(spend, 2),
            'net': round(net, 2), 'savings_rate': savings_rate,
            'savings_velocity_daily':   avg_daily_net,
            'savings_velocity_monthly': savings_velocity_monthly,
        },
        'daily_pace': {
            'avg_daily_income': avg_daily_income,
            'avg_daily_spend':  avg_daily_spend,
            'avg_daily_net':    avg_daily_net,
        },
        'monthly_goal': monthly_goal,
        'forecast': {
            'projected_monthly_income': projected_monthly_income,
            'projected_monthly_spend':  projected_monthly_spend,
            'projected_monthly_net':    projected_monthly_net,
            'projected_annual_net':     projected_annual_net,
            'forecast_daily_net':       forecast_daily_net,
            'label': f'Weighted \u00b7 70% last 30d / 30% {period_label}',
        },
        'trend': {'bucket': bucket, 'data': trend_data},
        'comparison': {
            'current_label': period_label,
            'prior_label':   prior_label,
            'current': {'income': round(income, 2),       'spend': round(spend, 2),       'net': round(net, 2)},
            'prior':   {'income': round(prior_income, 2), 'spend': round(prior_spend, 2), 'net': round(prior_net, 2)},
            'deltas':  {
                'income': round(income - prior_income, 2),
                'spend':  round(spend  - prior_spend,  2),
                'net':    round(net    - prior_net,    2),
            },
        },
        'spending_breakdown': spending_breakdown,
        'cash_position': {
            'current_balance':   round(current_balance, 2),
            'net_change':        net_change,
            'avg_monthly_spend': round(avg_monthly_spend, 2),
            'runway_months':     runway_months,
            'runway_days':       runway_days,
            'balance_sparkline': balance_sparkline,
        },
        'settings': {
            'monthly_net_target':  settings.get('monthly_net_target'),
            'target_cash_balance': settings.get('target_cash_balance'),
        },
    })


@app.route('/api/dashboard/settings', methods=['POST'])
def api_dashboard_settings():
    data = request.get_json() or {}
    conn = get_db()
    tcb = data.get('target_cash_balance', '__unchanged__')
    if tcb != '__unchanged__':
        conn.execute(
            'UPDATE kpi_settings SET target_cash_balance=?, updated_at=CURRENT_TIMESTAMP WHERE id=1',
            (tcb,)
        )
        conn.commit()
    row = conn.execute('SELECT monthly_net_target, target_cash_balance FROM kpi_settings WHERE id=1').fetchone()
    conn.close()
    return jsonify({'success': True, 'settings': dict(row) if row else {}})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
