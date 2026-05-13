# db.py — SQLite session storage
import sqlite3, os
from datetime import datetime

DB = os.getenv('DB_PATH', 'casad.db')


def init_db():
    con = sqlite3.connect(DB)

    # ── sessions table ────────────────────────────────────────────────────────
    existing_sess_cols = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}

    if not existing_sess_cols:
        # Fresh DB — create with full schema
        con.execute('''CREATE TABLE sessions
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
             phone TEXT, bridge TEXT, status TEXT,
             state TEXT, photo_count INTEGER,
             started_at TEXT, ended_at TEXT, report_path TEXT)''')
    else:
        # Old schema had phone as PRIMARY KEY — rebuild with id as PK
        needs_rebuild = 'id' not in existing_sess_cols or 'report_path' not in existing_sess_cols
        if needs_rebuild:
            copy_cols = [c for c in ('phone','bridge','status','state','photo_count','started_at','ended_at','report_path')
                         if c in existing_sess_cols]
            col_list = ', '.join(copy_cols)
            con.execute('''CREATE TABLE sessions_new
                (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 phone TEXT, bridge TEXT, status TEXT,
                 state TEXT, photo_count INTEGER,
                 started_at TEXT, ended_at TEXT, report_path TEXT)''')
            con.execute(f'INSERT INTO sessions_new ({col_list}) SELECT {col_list} FROM sessions')
            con.execute('DROP TABLE sessions')
            con.execute('ALTER TABLE sessions_new RENAME TO sessions')

    # ── messages table ────────────────────────────────────────────────────────
    existing_msg_cols = {row[1] for row in con.execute("PRAGMA table_info(messages)")}

    if not existing_msg_cols:
        con.execute('''CREATE TABLE messages
            (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT,
             session_id INTEGER,
             type TEXT, content TEXT, media_path TEXT,
             category TEXT, photo_num INTEGER,
             seq INTEGER, created_at TEXT, image_data BLOB)''')
    else:
        for col_sql in (
            'ALTER TABLE messages ADD COLUMN image_data BLOB',
            'ALTER TABLE messages ADD COLUMN category TEXT',
            'ALTER TABLE messages ADD COLUMN photo_num INTEGER',
            'ALTER TABLE messages ADD COLUMN session_id INTEGER',
        ):
            try:
                con.execute(col_sql)
            except Exception:
                pass

    con.commit()
    con.close()


def _active_session_id(con, phone):
    """Return the rowid of the current active (not ended) session for this phone."""
    row = con.execute(
        'SELECT id FROM sessions WHERE phone=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1',
        (phone,)
    ).fetchone()
    return row[0] if row else None


def store_message(msg):
    con = sqlite3.connect(DB)
    # Create session row if none exists yet
    sid = _active_session_id(con, msg['phone'])
    if sid is None:
        con.execute(
            'INSERT INTO sessions (phone, bridge, status, state, photo_count, started_at) VALUES (?,?,?,?,?,?)',
            (msg['phone'], msg.get('bridge', ''), 'active', 'menu', 0, datetime.utcnow().isoformat())
        )
        sid = con.execute('SELECT last_insert_rowid()').fetchone()[0]
    con.execute(
        'INSERT INTO messages (phone, session_id, type, content, media_path, category, photo_num, seq, created_at, image_data) '
        'VALUES (?,?,?,?,?,?,?,?,datetime("now"),?)',
        (msg['phone'], sid, msg['type'], msg.get('content'),
         msg.get('media_path'), msg.get('category'),
         msg.get('photo_num'), msg.get('seq', 0), msg.get('image_data'))
    )
    con.commit()
    con.close()


def get_session(phone):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    sid = _active_session_id(con, phone)
    if sid is None:
        con.close()
        return None
    session  = con.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
    messages = con.execute(
        'SELECT * FROM messages WHERE session_id=? ORDER BY seq, created_at', (sid,)
    ).fetchall()
    con.close()

    rows = [dict(m) for m in messages]
    os.makedirs(os.getenv('MEDIA_DIR', 'media'), exist_ok=True)
    for m in rows:
        if m.get('image_data') and m.get('media_path') and not os.path.exists(m['media_path']):
            with open(m['media_path'], 'wb') as f:
                f.write(m['image_data'])

    return {**dict(session), 'messages': rows}


def get_session_status(phone: str):
    con = sqlite3.connect(DB)
    row = con.execute(
        'SELECT status FROM sessions WHERE phone=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1',
        (phone,)
    ).fetchone()
    con.close()
    return row[0] if row else None


def get_session_state(phone: str):
    con = sqlite3.connect(DB)
    row = con.execute(
        'SELECT state, photo_count FROM sessions WHERE phone=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1',
        (phone,)
    ).fetchone()
    con.close()
    return (row[0] or 'menu', row[1] or 0) if row else ('menu', 0)


def set_session_state(phone: str, state: str):
    con = sqlite3.connect(DB)
    sid = _active_session_id(con, phone)
    if sid:
        con.execute('UPDATE sessions SET state=? WHERE id=?', (state, sid))
    con.commit()
    con.close()


def increment_photo_count(phone: str) -> int:
    con = sqlite3.connect(DB)
    sid = _active_session_id(con, phone)
    if sid:
        con.execute(
            'UPDATE sessions SET photo_count = COALESCE(photo_count, 0) + 1 WHERE id=?', (sid,)
        )
    row = con.execute('SELECT photo_count FROM sessions WHERE id=?', (sid,)).fetchone()
    con.commit()
    con.close()
    return row[0] if row else 1


def reset_session(phone: str):
    """End the current active session (stamp ended_at) rather than deleting it."""
    con = sqlite3.connect(DB)
    con.execute(
        "UPDATE sessions SET ended_at=?, status='exited' WHERE phone=? AND ended_at IS NULL",
        (datetime.utcnow().isoformat(), phone)
    )
    con.commit()
    con.close()


def has_bridge_details(phone: str) -> bool:
    """Return True if the active session has at least one bridge_details message."""
    con = sqlite3.connect(DB)
    sid = _active_session_id(con, phone)
    if sid is None:
        con.close()
        return False
    row = con.execute(
        "SELECT 1 FROM messages WHERE session_id=? AND category='bridge_details' LIMIT 1",
        (sid,)
    ).fetchone()
    con.close()
    return row is not None


def mark_done(phone: str, report_path: str = None):
    con = sqlite3.connect(DB)
    sid = _active_session_id(con, phone)
    if sid:
        con.execute(
            "UPDATE sessions SET status='done', ended_at=?, report_path=? WHERE id=?",
            (datetime.utcnow().isoformat(), report_path, sid)
        )
    con.commit()
    con.close()
