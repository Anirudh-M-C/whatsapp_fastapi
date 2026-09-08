import sqlite3
import json

DB_PATH = "conversations.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            user_id TEXT NOT NULL,
            phone_number_id TEXT NOT NULL,
            messages TEXT NOT NULL,
            PRIMARY KEY (user_id, phone_number_id)
        )
    """)
    return conn


MAX_TURNS = 6


def load_history(user_id, phone_number_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT messages FROM conversations WHERE user_id = ? AND phone_number_id = ?",
        (user_id, phone_number_id)
    ).fetchone()
    conn.close()

    if not row:
        return []

    messages = json.loads(row[0])

    system_msg = None
    rest = messages
    if messages and messages[0].get("role") == "system":
        system_msg = messages[0]
        rest = messages[1:]

    turn_start_indices = [i for i, m in enumerate(rest) if m.get("role") == "user"]

    if len(turn_start_indices) > MAX_TURNS:
        cutoff = turn_start_indices[-MAX_TURNS]
        rest = rest[cutoff:]

    return ([system_msg] if system_msg else []) + rest


def save_history(user_id, phone_number_id, messages):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conversations (user_id, phone_number_id, messages) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, phone_number_id) DO UPDATE SET messages = excluded.messages",
        (user_id, phone_number_id, json.dumps(messages))
    )
    conn.commit()
    conn.close()