import csv
import hashlib
import io
import re
from datetime import datetime
from models import get_db


def generate_dedup_hash(posting_date, description, amount, balance):
    """Generate a unique hash for deduplication using date + description + amount + balance."""
    raw = f"{posting_date}|{description.strip()}|{amount}|{balance}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def clean_description(desc):
    """Clean up Chase's padded description field."""
    desc = re.sub(r'\s{2,}', ' ', desc.strip())
    desc = desc.strip('"')
    return desc


def categorize_transaction(description, rules):
    """Match a transaction description against category rules (priority-ordered)."""
    desc_upper = description.upper()
    for rule in rules:
        if rule['keyword'].upper() in desc_upper:
            return rule['category']
    return 'Uncategorized'


def is_internal_transfer(category):
    """Check if a transaction is an internal transfer (excluded from spending calcs)."""
    return category in ('Internal Transfer', 'Savings Transfer')


def parse_chase_csv(file_content, filename='upload.csv'):
    """
    Parse a Chase bank CSV and insert transactions into the database.
    Returns a summary dict with counts.
    """
    conn = get_db()
    cursor = conn.cursor()

    # Load category rules sorted by priority
    cursor.execute('SELECT keyword, category, priority FROM category_rules ORDER BY priority ASC')
    rules = [dict(row) for row in cursor.fetchall()]

    # Read CSV
    if isinstance(file_content, bytes):
        file_content = file_content.decode('utf-8-sig')

    reader = csv.DictReader(io.StringIO(file_content))

    imported = 0
    skipped = 0
    errors = []

    for i, row in enumerate(reader, start=1):
        try:
            details = row.get('Details', '').strip()
            posting_date_str = row.get('Posting Date', '').strip()
            description = clean_description(row.get('Description', ''))
            amount_str = row.get('Amount', '').strip()
            txn_type = row.get('Type', '').strip()
            balance_str = row.get('Balance', '').strip().replace(',', '')

            # Parse date
            posting_date = datetime.strptime(posting_date_str, '%m/%d/%Y').strftime('%Y-%m-%d')

            # Parse amount
            amount = float(amount_str.replace(',', ''))

            # Parse balance (may be empty)
            balance = float(balance_str) if balance_str else None

            # Generate dedup hash
            dedup_hash = generate_dedup_hash(posting_date, description, amount, balance)

            # Check for duplicate
            cursor.execute('SELECT id FROM transactions WHERE dedup_hash = ?', (dedup_hash,))
            if cursor.fetchone():
                skipped += 1
                continue

            # Categorize
            category = categorize_transaction(description, rules)
            internal = 1 if is_internal_transfer(category) else 0

            cursor.execute('''
                INSERT INTO transactions 
                (details, posting_date, description, amount, type, balance, category, is_internal_transfer, dedup_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (details, posting_date, description, amount, txn_type, balance, category, internal, dedup_hash))

            imported += 1

        except Exception as e:
            errors.append(f"Row {i}: {str(e)}")
            continue

    # Log the upload
    cursor.execute(
        'INSERT INTO upload_log (filename, rows_imported, rows_skipped) VALUES (?, ?, ?)',
        (filename, imported, skipped)
    )

    conn.commit()
    conn.close()

    return {
        'imported': imported,
        'skipped': skipped,
        'errors': errors,
        'total_processed': imported + skipped + len(errors)
    }
