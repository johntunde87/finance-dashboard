import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'finance.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # Transactions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            details TEXT NOT NULL,
            posting_date DATE NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            type TEXT NOT NULL,
            balance REAL,
            check_or_slip TEXT,
            category TEXT DEFAULT 'Uncategorized',
            tags TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            is_internal_transfer INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            dedup_hash TEXT UNIQUE NOT NULL
        )
    ''')

    # Category rules table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS category_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            category TEXT NOT NULL,
            priority INTEGER DEFAULT 100,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Savings goals table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS savings_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            target_amount REAL NOT NULL,
            current_amount REAL DEFAULT 0,
            target_date DATE,
            goal_type TEXT DEFAULT 'custom',
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Monthly snapshots for tracking over time
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS monthly_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month TEXT NOT NULL UNIQUE,
            total_income REAL DEFAULT 0,
            total_expenses REAL DEFAULT 0,
            net_savings REAL DEFAULT 0,
            ending_balance REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Upload log for tracking CSV imports
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS upload_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            rows_imported INTEGER DEFAULT 0,
            rows_skipped INTEGER DEFAULT 0,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # KPI settings — single-row table (id always = 1)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS kpi_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            monthly_net_target REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('INSERT OR IGNORE INTO kpi_settings (id, monthly_net_target) VALUES (1, NULL)')

    # Migrate: add target_cash_balance if not present
    try:
        cursor.execute('ALTER TABLE kpi_settings ADD COLUMN target_cash_balance REAL')
        conn.commit()
    except Exception:
        pass  # column already exists

    # Insert default category rules
    cursor.execute('SELECT COUNT(*) FROM category_rules')
    if cursor.fetchone()[0] == 0:
        default_rules = [
            # Groceries
            ('WHOLEFDS', 'Groceries', 10),
            ('WHOLE FOODS', 'Groceries', 10),
            ('MORTON WILLIAMS', 'Groceries', 10),
            ('SUNAC NATURAL', 'Groceries', 10),
            ('FRESH DELI', 'Groceries', 10),
            ('WESTSIDE MARKET', 'Groceries', 10),
            ('SUNRISE MARKETPLACE', 'Groceries', 10),
            ('HEAVENLY MARKET', 'Groceries', 10),
            ('HEAVENLY FOOD', 'Groceries', 10),
            ('BROADWAY MINI MARKET', 'Groceries', 10),
            ('HELLS KITCHEN GOURMET', 'Groceries', 10),
            ('TARGET', 'Groceries', 10),
            ('5 BROTHERS FOOD', 'Groceries', 10),
            ('7-ELEVEN', 'Groceries', 10),

            # Dining Out
            ('CAVA', 'Dining Out', 20),
            ('MCDONALD', 'Dining Out', 20),
            ('HIGHLINE PIZZERIA', 'Dining Out', 20),
            ('SULLIVAN STREET BAK', 'Dining Out', 20),
            ('KEUR COUMBA', 'Dining Out', 20),
            ('DUNKIN', 'Dining Out', 20),
            ('STARBUCKS', 'Dining Out', 20),
            ('LEVAIN', 'Dining Out', 20),
            ('SCHNIPPERS', 'Dining Out', 20),
            ('LITTLE ITALY PIZZA', 'Dining Out', 20),
            ('ANDIAMO PIZZA', 'Dining Out', 20),
            ('AFD 10TH AVE', 'Dining Out', 20),

            # Transportation
            ('E-Z*PASSNY', 'Transportation', 30),
            ('CONOCO', 'Transportation', 30),
            ('BP#', 'Transportation', 30),
            ('BRUCKNER CAR WASH', 'Transportation', 30),

            # Gig Platform (Buggy)
            ('BUGGY*', 'Gig Platform', 35),

            # Income
            ('Uber USA, LLC', 'Income: Uber', 5),
            ('UBER USA 6787', 'Income: Uber', 5),
            ('VENMO            CASHOUT', 'Income: Venmo', 5),

            # Debt Payments
            ('CHASE CREDIT CRD AUTOPAY', 'Debt Payments', 15),
            ('CAPITAL ONE      CRCARDPMT', 'Debt Payments', 15),
            ('Chase Pay in 4', 'Debt Payments', 15),

            # Subscriptions
            ('Netflix', 'Subscriptions', 40),
            ('Spotify', 'Subscriptions', 40),
            ('YouTubePremi', 'Subscriptions', 40),
            ('Audible', 'Subscriptions', 40),
            ('CLAUDE.AI', 'Subscriptions', 40),
            ('OPENAI', 'Subscriptions', 40),
            ('APPLE.COM/BILL', 'Subscriptions', 40),

            # Business Expenses
            ('GOOGLE *ADS', 'Business Expenses', 25),
            ('UNBOUNCE', 'Business Expenses', 25),
            ('PYTHONANYWHERE', 'Business Expenses', 25),
            ('INSPIRE-LABS', 'Business Expenses', 25),
            ('HERCULES CORP', 'Business Expenses', 25),

            # Software & Tools
            ('GOOGLE *Workspace', 'Software & Tools', 45),
            ('GOOGLE *Google One', 'Software & Tools', 45),

            # Health & Wellness
            ('CVS/PHARMACY', 'Health & Wellness', 50),
            ('GNC', 'Health & Wellness', 50),
            ('DUANE READE', 'Health & Wellness', 50),

            # Shipping & Postal
            ('USPS', 'Shipping & Postal', 55),

            # Internal Transfers
            ('ODP TRANSFER FROM SAVINGS', 'Internal Transfer', 1),
            ('Online Transfer to  SAV', 'Savings Transfer', 1),
            ('TOT ODP', 'Internal Transfer', 1),
            ('ODP TRANSFER', 'Internal Transfer', 1),

            # Miscellaneous
            ('COWBOY BAIL BONDS', 'Miscellaneous', 90),
        ]
        cursor.executemany(
            'INSERT INTO category_rules (keyword, category, priority) VALUES (?, ?, ?)',
            default_rules
        )

    conn.commit()
    conn.close()
