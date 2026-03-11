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

def get_summary_stats(conn, start_date=None, end_date=None):
    df = ""
    p = []
    if start_date: df += " AND posting_date >= ?"; p.append(start_date)
    if end_date: df += " AND posting_date <= ?"; p.append(end_date)

    total_income = conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE amount > 0 AND is_internal_transfer=0{df}', p).fetchone()[0]
    total_expenses = abs(conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE amount < 0 AND is_internal_transfer=0{df}', p).fetchone()[0])
    net_savings = total_income - total_expenses
    row = conn.execute('SELECT balance FROM transactions WHERE balance IS NOT NULL ORDER BY posting_date DESC, id DESC LIMIT 1').fetchone()
    current_balance = row[0] if row else 0
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
    conn = get_db()
    today = datetime.now()
    period = request.args.get('period', 'all')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    if period == 'month':
        start_date = today.replace(day=1).strftime('%Y-%m-%d'); end_date = today.strftime('%Y-%m-%d')
    elif period == 'quarter':
        qs = ((today.month-1)//3)*3+1; start_date = today.replace(month=qs, day=1).strftime('%Y-%m-%d'); end_date = today.strftime('%Y-%m-%d')
    elif period == 'year':
        start_date = today.replace(month=1, day=1).strftime('%Y-%m-%d'); end_date = today.strftime('%Y-%m-%d')

    stats = get_summary_stats(conn, start_date, end_date)
    categories = get_category_breakdown(conn, start_date, end_date)
    trend = get_spending_trend(conn)
    recurring = get_recurring_expenses(conn)
    goals = [dict(r) for r in conn.execute('SELECT * FROM savings_goals WHERE is_active=1 ORDER BY created_at DESC').fetchall()]
    for goal in goals:
        remaining = goal['target_amount'] - goal['current_amount']
        if goal['target_date']:
            target = datetime.strptime(goal['target_date'], '%Y-%m-%d')
            ml = max((target.year-today.year)*12+target.month-today.month, 1)
            goal['monthly_needed'] = remaining/ml
            goal['progress_pct'] = min(round((goal['current_amount']/goal['target_amount'])*100,1), 100)
            goal['months_left'] = ml
            if stats['total_expenses'] > 0 and trend:
                goal['projected_monthly_income'] = stats['total_expenses']/max(len(trend),1) + goal['monthly_needed']
            else:
                goal['projected_monthly_income'] = goal['monthly_needed']
        else:
            goal['monthly_needed'] = None
            goal['progress_pct'] = min(round((goal['current_amount']/goal['target_amount'])*100,1),100) if goal['target_amount']>0 else 0
            goal['months_left'] = None; goal['projected_monthly_income'] = None

    recent = [dict(r) for r in conn.execute('SELECT * FROM transactions ORDER BY posting_date DESC, id DESC LIMIT 10').fetchall()]
    uncat = conn.execute("SELECT COUNT(*) FROM transactions WHERE category='Uncategorized'").fetchone()[0]
    conn.close()
    return render_template('dashboard.html', stats=stats, categories=categories, trend=json.dumps(trend),
        recurring=recurring, goals=goals, recent=recent, uncategorized_count=uncat, period=period, start_date=start_date, end_date=end_date)

@app.route('/weekly')
def weekly():
    return render_template('weekly.html')

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

if __name__ == '__main__':
    app.run(debug=True, port=5000)
