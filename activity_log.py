"""
activity_log.py — Journalisation des connexions et actions des utilisateurs.
Table SQLite indépendante, créée automatiquement dans la même base (registry.sqlite3).
"""
import sqlite3
from datetime import datetime
from pathlib import Path


def _get_conn(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            username  TEXT NOT NULL,
            role      TEXT NOT NULL,
            action    TEXT NOT NULL,
            details   TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    return conn


def log_action(db_path: str, username: str, role: str, action: str, details: str = ""):
    conn = _get_conn(db_path)
    conn.execute(
        "INSERT INTO activity_log (username, role, action, details, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, role, action, details, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def get_logs(db_path: str, limit: int = 500):
    conn = _get_conn(db_path)
    cur = conn.execute(
        "SELECT username, role, action, details, timestamp "
        "FROM activity_log ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"username": r[0], "role": r[1], "action": r[2], "details": r[3], "timestamp": r[4]}
        for r in rows
    ]