import sqlite3
import json

DB_PATH = "payments.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payment_links (
            payment_link_id TEXT PRIMARY KEY,
            phone_number_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            order_id INTEGER NOT NULL,
            order_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            payment_reference TEXT,
            amount_paid REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_link_id TEXT,
            event TEXT NOT NULL,
            outcome TEXT NOT NULL,
            raw_payload TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    return conn


def save_payment_link(payment_link_id, phone_number_id, user_id, order_id, order_name):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO payment_links "
        "(payment_link_id, phone_number_id, user_id, order_id, order_name, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending') "
        "ON CONFLICT(payment_link_id) DO UPDATE SET status = 'pending'",
        (payment_link_id, phone_number_id, user_id, order_id, order_name)
    )
    conn.commit()
    conn.close()


def get_payment_link(payment_link_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT payment_link_id, phone_number_id, user_id, order_id, order_name, status, "
        "payment_reference, amount_paid, created_at, updated_at "
        "FROM payment_links WHERE payment_link_id = ?",
        (payment_link_id,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    keys = ["payment_link_id", "phone_number_id", "user_id", "order_id", "order_name",
            "status", "payment_reference", "amount_paid", "created_at", "updated_at"]
    return dict(zip(keys, row))


def get_payment_link_by_order_name(order_name):
    conn = _get_conn()
    row = conn.execute(
        "SELECT payment_link_id, phone_number_id, user_id, order_id, order_name, status, "
        "payment_reference, amount_paid, created_at, updated_at "
        "FROM payment_links WHERE order_name = ? ORDER BY created_at DESC LIMIT 1",
        (order_name,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    keys = ["payment_link_id", "phone_number_id", "user_id", "order_id", "order_name",
            "status", "payment_reference", "amount_paid", "created_at", "updated_at"]
    return dict(zip(keys, row))


def get_payment_link_by_order_name(order_name):
    conn = _get_conn()
    row = conn.execute(
        "SELECT payment_link_id, phone_number_id, user_id, order_id, order_name, status, "
        "payment_reference, amount_paid, created_at, updated_at "
        "FROM payment_links WHERE order_name = ? ORDER BY created_at DESC LIMIT 1",
        (order_name,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    keys = ["payment_link_id", "phone_number_id", "user_id", "order_id", "order_name",
            "status", "payment_reference", "amount_paid", "created_at", "updated_at"]
    return dict(zip(keys, row))


def try_claim(payment_link_id, new_status, payment_reference=None, amount_paid=None,
              valid_from_statuses=("pending",)):
    """
    Atomically moves a payment link's status forward only if it's currently
    in one of valid_from_statuses. Returns True if THIS call performed the
    transition (this delivery should trigger side effects like sending a
    WhatsApp message or confirming the order), False if another delivery
    already claimed it first.
    """
    conn = _get_conn()
    placeholders = ",".join("?" for _ in valid_from_statuses)
    cur = conn.execute(
        f"UPDATE payment_links SET status = ?, "
        f"payment_reference = COALESCE(?, payment_reference), "
        f"amount_paid = COALESCE(?, amount_paid), "
        f"updated_at = datetime('now') "
        f"WHERE payment_link_id = ? AND status IN ({placeholders})",
        (new_status, payment_reference, amount_paid, payment_link_id, *valid_from_statuses)
    )
    conn.commit()
    claimed = cur.rowcount == 1
    conn.close()
    return claimed


def log_event(payment_link_id, event, outcome, raw_payload=None):
    """Append-only audit log — every webhook delivery gets a row here,
    successful, duplicate, malformed, or otherwise."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO payment_events (payment_link_id, event, outcome, raw_payload) VALUES (?, ?, ?, ?)",
        (payment_link_id, event, outcome, json.dumps(raw_payload) if raw_payload is not None else None)
    )
    conn.commit()
    conn.close()