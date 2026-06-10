"""Хранилище задач на SQLite. Просто и без внешних зависимостей."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import config

STATUS_NEW = "new"                  # поставлена, ещё не принята
STATUS_IN_PROGRESS = "in_progress"  # принята, в работе
STATUS_DONE = "done"                # завершена
STATUS_REJECTED = "rejected"        # отклонена

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    created_at  TEXT NOT NULL,
    accepted_at TEXT,
    done_at     TEXT,
    chat_id     INTEGER,
    message_id  INTEGER
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


@contextmanager
def _conn():
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.execute(_SCHEMA)


def add_task(text: str, chat_id: int) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO tasks (text, status, created_at, chat_id) VALUES (?, ?, ?, ?)",
            (text, STATUS_NEW, _now(), chat_id),
        )
        return cur.lastrowid


def set_card(task_id: int, message_id: int) -> None:
    with _conn() as con:
        con.execute("UPDATE tasks SET message_id=? WHERE id=?", (message_id, task_id))


def get_task(task_id: int):
    with _conn() as con:
        row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def set_status(task_id: int, status: str) -> None:
    stamp_field = None
    if status == STATUS_IN_PROGRESS:
        stamp_field = "accepted_at"
    elif status == STATUS_DONE:
        stamp_field = "done_at"
    with _conn() as con:
        if stamp_field:
            con.execute(
                f"UPDATE tasks SET status=?, {stamp_field}=? WHERE id=?",
                (status, _now(), task_id),
            )
        else:
            con.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))


def list_by_status(status: str):
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM tasks WHERE status=? ORDER BY id", (status,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_active():
    """Все задачи, кроме отклонённых."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM tasks WHERE status!=? ORDER BY id", (STATUS_REJECTED,)
        ).fetchall()
        return [dict(r) for r in rows]
