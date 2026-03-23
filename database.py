import sqlite3
import os
from datetime import datetime, date

DB_PATH = os.getenv("DB_PATH", "data/cravada.db")


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            joined_at TEXT,
            picks_seen_today INTEGER DEFAULT 0,
            picks_reset_date TEXT
        );

        CREATE TABLE IF NOT EXISTS waitlist (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_at TEXT
        );

        CREATE TABLE IF NOT EXISTS picks_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            picks_json TEXT,
            generated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS injury_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            updated_at TEXT
        );
    """)
    conn.commit()
    conn.close()


def add_user(user_id: int, username: str):
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO users (user_id, username, joined_at, picks_seen_today, picks_reset_date)
        VALUES (?, ?, ?, 0, ?)
    """, (user_id, username, datetime.now().isoformat(), date.today().isoformat()))
    conn.commit()
    conn.close()


def get_user(user_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()

    if not row:
        return {"picks_seen_today": 0}

    user = dict(row)
    # Reset diário
    if user.get("picks_reset_date") != date.today().isoformat():
        update_user_picks_seen(user_id, 0)
        user["picks_seen_today"] = 0

    return user


def update_user_picks_seen(user_id: int, count: int):
    conn = get_conn()
    conn.execute("""
        UPDATE users SET picks_seen_today = ?, picks_reset_date = ? WHERE user_id = ?
    """, (count, date.today().isoformat(), user_id))
    conn.commit()
    conn.close()


def add_to_waitlist(user_id: int, username: str):
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO waitlist (user_id, username, added_at)
        VALUES (?, ?, ?)
    """, (user_id, username, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def is_on_waitlist(user_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM waitlist WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def get_all_users() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = get_conn()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    waitlist = conn.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0]
    picks_today = conn.execute(
        "SELECT picks_json FROM picks_log WHERE date = ?",
        (date.today().isoformat(),)
    ).fetchone()
    last_injury = conn.execute(
        "SELECT updated_at FROM injury_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    import json
    picks_count = len(json.loads(picks_today["picks_json"])) if picks_today else 0

    return {
        "total_users": total_users,
        "waitlist": waitlist,
        "picks_today": picks_count,
        "last_injury_update": last_injury["updated_at"] if last_injury else "Nunca"
    }


def save_picks(picks: list):
    conn = get_conn()
    import json
    conn.execute("""
        INSERT OR REPLACE INTO picks_log (date, picks_json, generated_at)
        VALUES (?, ?, ?)
    """, (date.today().isoformat(), json.dumps(picks), datetime.now().isoformat()))
    conn.commit()
    conn.close()


def log_injury_update():
    conn = get_conn()
    conn.execute("INSERT INTO injury_log (updated_at) VALUES (?)", (datetime.now().isoformat(),))
    conn.commit()
    conn.close()


# Init on import
init_db()
