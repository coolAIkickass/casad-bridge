# db.py — PostgreSQL session storage
#
# Replaces the old SQLite backend.  Render's ephemeral filesystem wipes
# casad.db on every dyno sleep/restart (~15 min inactivity on free tier),
# losing in-progress inspection sessions.  PostgreSQL persists across restarts.
#
# Connection: set DATABASE_URL env var (Render auto-sets this when you add a
#             Postgres add-on to the service).
#
# Pool: ThreadedConnectionPool(2, 10) — safe for 4 concurrent report-gen
#       threads + webhook handlers without exhausting the free-tier 25-conn limit.
#
# Public API is unchanged — server.py needs no edits.

import os
import psycopg2
import psycopg2.pool
from contextlib import contextmanager
from datetime import datetime

DATABASE_URL = os.getenv('DATABASE_URL')

_pool: psycopg2.pool.ThreadedConnectionPool = None   # initialised by init_db()


# ─────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────

def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_db() first")
    return _pool


@contextmanager
def _db(dict_rows: bool = False):
    """Yield a cursor from the pool.

    Commits on clean exit, rolls back on exception, always returns the
    connection to the pool.  Use dict_rows=True to get column-name dicts.
    """
    pool = _get_pool()
    con  = pool.getconn()
    try:
        if dict_rows:
            import psycopg2.extras
            cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = con.cursor()
        try:
            yield cur
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            cur.close()
    finally:
        pool.putconn(con)


def _rows_as_dicts(cur) -> list:
    """Convert all fetched rows to plain dicts (works with a regular cursor)."""
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _row_as_dict(cur) -> dict:
    cols = [desc[0] for desc in cur.description]
    row  = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def _active_session_id(cur, phone: str):
    """Return the id of the current active (not ended) session for this phone."""
    cur.execute(
        'SELECT id FROM sessions WHERE phone=%s AND ended_at IS NULL '
        'ORDER BY started_at DESC LIMIT 1',
        (phone,)
    )
    row = cur.fetchone()
    return row[0] if row else None


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────

def init_db():
    """Create the connection pool and ensure tables exist."""
    global _pool
    if not DATABASE_URL:
        raise EnvironmentError(
            "DATABASE_URL environment variable not set. "
            "Add a Postgres add-on in Render and it will be set automatically."
        )
    _pool = psycopg2.pool.ThreadedConnectionPool(2, 10, DATABASE_URL)

    with _db() as cur:
        # sessions
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id           SERIAL PRIMARY KEY,
                phone        TEXT,
                bridge       TEXT,
                status       TEXT,
                state        TEXT,
                photo_count  INTEGER DEFAULT 0,
                started_at   TEXT,
                ended_at     TEXT,
                report_path  TEXT,
                report_format TEXT
            )
        ''')
        # messages — image_data is BYTEA so photos survive restarts
        cur.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id          SERIAL PRIMARY KEY,
                phone       TEXT,
                session_id  INTEGER,
                type        TEXT,
                content     TEXT,
                media_path  TEXT,
                category    TEXT,
                photo_num   INTEGER,
                seq         INTEGER,
                created_at  TEXT,
                image_data  BYTEA
            )
        ''')
        # Index for the common "active session for phone" query
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_sessions_phone_active
            ON sessions (phone, ended_at)
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages (session_id, seq, created_at)
        ''')


def store_message(msg: dict):
    with _db() as cur:
        sid = _active_session_id(cur, msg['phone'])
        if sid is None:
            cur.execute(
                '''INSERT INTO sessions
                   (phone, bridge, status, state, photo_count, started_at)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id''',
                (msg['phone'], msg.get('bridge', ''), 'active', 'menu', 0,
                 datetime.utcnow().isoformat())
            )
            sid = cur.fetchone()[0]

        # image_data may be bytes (psycopg2 maps Python bytes ↔ BYTEA automatically)
        cur.execute(
            '''INSERT INTO messages
               (phone, session_id, type, content, media_path, category,
                photo_num, seq, created_at, image_data)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
            (msg['phone'], sid, msg['type'], msg.get('content'),
             msg.get('media_path'), msg.get('category'),
             msg.get('photo_num'), msg.get('seq', 0),
             datetime.utcnow().isoformat(), msg.get('image_data'))
        )


def get_session(phone: str) -> dict | None:
    with _db() as cur:
        sid = _active_session_id(cur, phone)
        if sid is None:
            return None

        cur.execute('SELECT * FROM sessions WHERE id=%s', (sid,))
        session = _row_as_dict(cur)
        if session is None:
            return None

        cur.execute(
            'SELECT * FROM messages WHERE session_id=%s ORDER BY seq, created_at',
            (sid,)
        )
        messages = _rows_as_dicts(cur)

    # Restore media files to disk if they were lost (e.g. after a restart)
    os.makedirs(os.getenv('MEDIA_DIR', 'media'), exist_ok=True)
    for m in messages:
        data = m.get('image_data')
        if data and m.get('media_path') and not os.path.exists(m['media_path']):
            # psycopg2 returns BYTEA as memoryview — convert to bytes
            raw = bytes(data) if isinstance(data, memoryview) else data
            with open(m['media_path'], 'wb') as f:
                f.write(raw)

    return {**session, 'messages': messages}


def get_session_status(phone: str) -> str | None:
    with _db() as cur:
        cur.execute(
            'SELECT status FROM sessions WHERE phone=%s AND ended_at IS NULL '
            'ORDER BY started_at DESC LIMIT 1',
            (phone,)
        )
        row = cur.fetchone()
    return row[0] if row else None


def get_session_state(phone: str) -> tuple[str, int]:
    with _db() as cur:
        cur.execute(
            'SELECT state, photo_count FROM sessions WHERE phone=%s AND ended_at IS NULL '
            'ORDER BY started_at DESC LIMIT 1',
            (phone,)
        )
        row = cur.fetchone()
    return (row[0] or 'menu', row[1] or 0) if row else ('menu', 0)


def set_session_state(phone: str, state: str):
    with _db() as cur:
        sid = _active_session_id(cur, phone)
        if sid:
            cur.execute('UPDATE sessions SET state=%s WHERE id=%s', (state, sid))


def increment_photo_count(phone: str) -> int:
    with _db() as cur:
        sid = _active_session_id(cur, phone)
        if sid:
            cur.execute(
                'UPDATE sessions SET photo_count = COALESCE(photo_count, 0) + 1 '
                'WHERE id=%s', (sid,)
            )
            cur.execute('SELECT photo_count FROM sessions WHERE id=%s', (sid,))
            row = cur.fetchone()
            return row[0] if row else 1
    return 1


def reset_session(phone: str):
    """End the current active session (stamp ended_at) rather than deleting it."""
    with _db() as cur:
        cur.execute(
            "UPDATE sessions SET ended_at=%s, status='exited' "
            "WHERE phone=%s AND ended_at IS NULL",
            (datetime.utcnow().isoformat(), phone)
        )


def has_bridge_details(phone: str) -> bool:
    """Return True if the active session has at least one bridge_details message."""
    with _db() as cur:
        sid = _active_session_id(cur, phone)
        if sid is None:
            return False
        cur.execute(
            "SELECT 1 FROM messages WHERE session_id=%s AND category='bridge_details' LIMIT 1",
            (sid,)
        )
        return cur.fetchone() is not None


def set_report_format(phone: str, fmt: str):
    """Store report format ('word' or 'excel') for the active session."""
    with _db() as cur:
        sid = _active_session_id(cur, phone)
        if sid:
            cur.execute(
                'UPDATE sessions SET report_format=%s WHERE id=%s', (fmt, sid)
            )


def get_report_format(phone: str) -> str:
    """Return 'word' (default) or 'excel' for the active session."""
    with _db() as cur:
        cur.execute(
            'SELECT report_format FROM sessions WHERE phone=%s AND ended_at IS NULL '
            'ORDER BY started_at DESC LIMIT 1',
            (phone,)
        )
        row = cur.fetchone()
    return row[0] if (row and row[0]) else 'word'


def mark_done(phone: str, report_path: str = None):
    with _db() as cur:
        sid = _active_session_id(cur, phone)
        if sid:
            cur.execute(
                "UPDATE sessions SET status='done', ended_at=%s, report_path=%s "
                "WHERE id=%s",
                (datetime.utcnow().isoformat(), report_path, sid)
            )
