"""
database.py — PostgreSQL helpers para slack-reporter
"""

import json
import logging
import os

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

_DSN = os.environ.get("DATABASE_URL", "")


def _conn():
    return psycopg2.connect(_DSN, cursor_factory=psycopg2.extras.RealDictCursor)


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db():
    sql = """
    CREATE TABLE IF NOT EXISTS users (
        slack_user_id   TEXT PRIMARY KEY,
        team_id         TEXT NOT NULL,
        email           TEXT,
        display_name    TEXT,
        access_token    TEXT NOT NULL,
        active          BOOLEAN DEFAULT TRUE,
        report_schedule TEXT DEFAULT '{}',
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        last_report_at  TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS reports (
        id              SERIAL PRIMARY KEY,
        slack_user_id   TEXT NOT NULL,
        generated_at    TIMESTAMPTZ DEFAULT NOW(),
        period_start    TIMESTAMPTZ,
        period_end      TIMESTAMPTZ,
        message_count   INT,
        channel_count   INT
    );
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    log.info("DB schema OK")


# ── Users ─────────────────────────────────────────────────────────────────────

def upsert_user(slack_user_id: str, team_id: str, email: str,
                display_name: str, access_token: str):
    sql = """
    INSERT INTO users (slack_user_id, team_id, email, display_name, access_token)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (slack_user_id) DO UPDATE SET
        access_token  = EXCLUDED.access_token,
        email         = EXCLUDED.email,
        display_name  = EXCLUDED.display_name,
        active        = TRUE
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (slack_user_id, team_id, email, display_name, access_token))
        conn.commit()


def get_user(slack_user_id: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE slack_user_id = %s", (slack_user_id,))
            return cur.fetchone()


def get_all_users() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY display_name")
            return cur.fetchall()


def get_active_users() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE active = TRUE ORDER BY display_name")
            return cur.fetchall()


def deactivate_user(slack_user_id: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET active = FALSE WHERE slack_user_id = %s",
                        (slack_user_id,))
        conn.commit()


def update_last_report(slack_user_id: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_report_at = NOW() WHERE slack_user_id = %s",
                (slack_user_id,)
            )
        conn.commit()


def update_report_schedule(slack_user_id: str, schedule: dict):
    """schedule = {"1": 9, "3": 17}  (weekday ISO → hour)"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET report_schedule = %s WHERE slack_user_id = %s",
                (json.dumps(schedule), slack_user_id)
            )
        conn.commit()


def get_effective_schedule(user: dict) -> dict:
    """Devuelve {weekday_str: hour} del usuario, o default lunes-viernes 9h."""
    raw = user.get("report_schedule") or "{}"
    try:
        sched = json.loads(raw) if isinstance(raw, str) else raw
        if sched:
            return {str(k): int(v) for k, v in sched.items()}
    except Exception:
        pass
    # Default: lunes a viernes a las 9
    return {"0": 9, "1": 9, "2": 9, "3": 9, "4": 9}


def get_users_due_now(weekday: int, hour: int) -> list:
    """Devuelve usuarios activos cuyo schedule incluye (weekday, hour)."""
    all_users = get_active_users()
    due = []
    for user in all_users:
        sched = get_effective_schedule(user)
        if sched.get(str(weekday)) == hour:
            due.append(user)
    return due


# ── Reports log ───────────────────────────────────────────────────────────────

def log_report(slack_user_id: str, period_start, period_end,
               message_count: int, channel_count: int):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO reports
                   (slack_user_id, period_start, period_end, message_count, channel_count)
                   VALUES (%s, %s, %s, %s, %s)""",
                (slack_user_id, period_start, period_end, message_count, channel_count)
            )
        conn.commit()
