# MoneyMap — Personal Finance Dashboard

## Overview
A personal finance dashboard for tracking spending, income, and savings goals. Built for a single user (John) who drives for Uber and runs a proposal photography business (Capture My Proposal). Primary income sources are Uber payouts and Venmo. Deployed on PythonAnywhere.

## Stack
- **Backend:** Flask + SQLite (single `finance.db` file)
- **Frontend:** Vanilla JS + Chart.js 4.4.1 for interactive views
- **CSS:** Single file, mobile-first responsive, dark theme
- **Fonts:** DM Sans (UI) + JetBrains Mono (numbers/code)
- **Deployment:** PythonAnywhere (paid account, subdomain web app)
- **No React, no build step, no npm** — keep it simple

## Project Structure
```
finance-dashboard/
├── app.py              # All Flask routes + JSON API endpoints
├── csv_parser.py       # Chase CSV parser with SHA-256 dedup
├── models.py           # SQLite schema, init_db(), default category rules
├── requirements.txt    # Flask only (everything else is stdlib)
├── wsgi.py             # PythonAnywhere WSGI config
├── finance.db          # SQLite database (gitignore this)
├── uploads/            # Uploaded CSVs (gitignore this)
├── static/
│   └── css/
│       └── style.css   # Complete mobile-first responsive stylesheet
└── templates/
    ├── base.html       # Layout: sidebar nav, mobile hamburger, flash messages
    ├── dashboard.html  # Main dashboard: KPIs, charts, goals, recent txns
    ├── weekly.html     # Weekly Analytics: JS-driven, fetches from /api/*
    ├── monthly.html    # Monthly Calendar: JS-driven, fetches from /api/*
    ├── transactions.html  # Transaction browser with filters
    ├── goals.html      # Savings goals CRUD with projections
    ├── upload.html     # CSV upload with drag-and-drop
    └── rules.html      # Category rules management
```

## Architecture Patterns

### Page types
1. **Server-rendered pages** — Dashboard, Transactions, Goals, Upload, Rules use standard Flask templates with Jinja2. Data passed via `render_template()`.
2. **JS-driven interactive pages** — Weekly and Monthly views render a shell template, then fetch all data from JSON API endpoints. All interactivity (mode switching, day selection, chart updates, category filtering) happens client-side.

### API endpoints
| Endpoint | Purpose |
|----------|---------|
| `GET /api/categories` | All distinct categories |
| `GET /api/weekly/weeks?mode=&categories=` | Week list with totals + mini chart data |
| `GET /api/weekly/detail?start=&end=&mode=&categories=&selected_date=` | Single week: daily data, transactions, totals, averages |
| `GET /api/monthly/months` | Available months |
| `GET /api/monthly/detail?month=&mode=&categories=&selected_date=` | Single month: daily data, calendar info, transactions |

### Category filtering
- Passed as comma-separated category names in `categories` query param
- Empty = all categories (no filter)
- Built into SQL via `build_category_filter()` helper which returns SQL fragment + params

### Deduplication
- SHA-256 hash of `posting_date|description|amount|balance`
- Stored in `dedup_hash` column (UNIQUE constraint)
- On upload: skip rows where hash already exists

### Transaction categorization
- Keyword-matching rules table (`category_rules`) scanned in priority order
- First match wins, unmatched → "Uncategorized"
- Manual re-categorization updates only that transaction (no learning yet)
- Adding a rule on the Rules page auto-recategorizes matching uncategorized txns

## Database Schema

### transactions
`id, details, posting_date, description, amount, type, balance, check_or_slip, category, tags, notes, is_internal_transfer, created_at, dedup_hash`

### category_rules
`id, keyword, category, priority, created_at`

### savings_goals
`id, name, target_amount, current_amount, target_date, goal_type, is_active, created_at`

### monthly_snapshots
`id, month, total_income, total_expenses, net_savings, ending_balance, created_at`
(Currently unused — reserved for future tracking)

### upload_log
`id, filename, rows_imported, rows_skipped, uploaded_at`

## Key Business Logic
- **Internal transfers** (`is_internal_transfer = 1`) are excluded from ALL income/expense/savings calculations. Categories: "Internal Transfer", "Savings Transfer"
- **Week boundaries:** Monday through Sunday
- **Income** = positive amounts where `is_internal_transfer = 0`
- **Expenses** = negative amounts where `is_internal_transfer = 0`
- **Net savings** = income - expenses
- **Current balance** = latest transaction with a non-null balance field
- **Averages** in weekly view = total / days_with_activity (not total / 7)

## Chase CSV Format
```
Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
DEBIT,02/27/2026,"POS DEBIT CVS/PHARMACY...",-18.58,MISC_DEBIT, ,,
```
- Date format: MM/DD/YYYY (parsed to YYYY-MM-DD)
- Amount: negative = debit, positive = credit
- Balance: sometimes empty (especially for MISC_DEBIT/MISC_CREDIT)
- Description: heavily padded with spaces, cleaned on import

## Conventions
- Mobile: stacked transaction cards. Desktop: tables.
- Dark theme exclusively (no light mode toggle)
- All dollar amounts use JetBrains Mono
- Positive amounts green, negative amounts red
- Category badges are small pills with border
- Status badges: green (on track), red (behind), gold (complete)

## What NOT to change
- Do not add React or any build tools
- Do not change the database schema without migration logic
- Do not remove the dedup system
- Keep all CSS in one file (no CSS modules/preprocessors)
- Keep PythonAnywhere compatibility (no async, no websockets)

## Testing
- No test framework set up yet
- Test with `python3 app.py` locally on port 5000
- Use Flask test client for API endpoint testing:
  ```python
  with app.test_client() as c:
      r = c.get('/api/weekly/weeks?mode=spend')
      data = r.get_json()
  ```

## Deployment
1. `git push` from local
2. SSH/Bash console on PythonAnywhere: `cd ~/finance-dashboard && git pull`
3. Web tab → Reload
