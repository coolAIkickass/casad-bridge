# db.py — SQLite session storage
import sqlite3, os
from datetime import datetime

DB = os.getenv('DB_PATH', 'casad.db')


def init_db():
    con = sqlite3.connect(DB)
    con.execute('''CREATE TABLE IF NOT EXISTS sessions
        (phone TEXT PRIMARY KEY, bridge TEXT, status TEXT,
         started_at TEXT, reminded_at TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS messages
        (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT,
         type TEXT, content TEXT, media_path TEXT,
         seq INTEGER, created_at TEXT, image_data BLOB)''')
    for col_sql in (
        'ALTER TABLE messages ADD COLUMN image_data BLOB',
        'ALTER TABLE sessions ADD COLUMN reminded_at TEXT',
    ):
        try:
            con.execute(col_sql)
        except Exception:
            pass
    con.commit()
    con.close()


def store_message(msg):
    con = sqlite3.connect(DB)
    con.execute(
        'INSERT OR IGNORE INTO sessions VALUES (?,?,?,?,?)',
        (msg['phone'], msg.get('bridge', ''), 'active',
         datetime.utcnow().isoformat(), None)
    )
    con.execute(
        'INSERT INTO messages VALUES (NULL,?,?,?,?,?,datetime("now"),?)',
        (msg['phone'], msg['type'], msg.get('content'),
         msg.get('media_path'), msg.get('seq', 0), msg.get('image_data'))
    )
    con.commit()
    con.close()


def get_session(phone):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    session  = con.execute('SELECT * FROM sessions WHERE phone=?', (phone,)).fetchone()
    messages = con.execute(
        'SELECT * FROM messages WHERE phone=? ORDER BY seq, created_at', (phone,)
    ).fetchall()
    con.close()
    if not session:
        return None

    rows = [dict(m) for m in messages]

    # Restore image files from BLOB if deleted by a redeploy
    os.makedirs(os.getenv('MEDIA_DIR', 'media'), exist_ok=True)
    for m in rows:
        if m.get('image_data') and m.get('media_path') and not os.path.exists(m['media_path']):
            with open(m['media_path'], 'wb') as f:
                f.write(m['image_data'])

    return {**dict(session), 'messages': rows}


def get_session_status(phone: str):
    con = sqlite3.connect(DB)
    row = con.execute('SELECT status FROM sessions WHERE phone=?', (phone,)).fetchone()
    con.close()
    return row[0] if row else None


def reset_session(phone: str):
    con = sqlite3.connect(DB)
    con.execute('DELETE FROM messages WHERE phone=?', (phone,))
    con.execute('DELETE FROM sessions WHERE phone=?', (phone,))
    con.commit()
    con.close()


def mark_done(phone: str):
    con = sqlite3.connect(DB)
    con.execute("UPDATE sessions SET status='done' WHERE phone=?", (phone,))
    con.commit()
    con.close()


def mark_reminded(phone: str):
    con = sqlite3.connect(DB)
    con.execute("UPDATE sessions SET reminded_at=datetime('now') WHERE phone=?", (phone,))
    con.commit()
    con.close()


def get_stale_sessions() -> list:
    """Return phones with active sessions idle for 3+ hours and not yet reminded."""
    con = sqlite3.connect(DB)
    rows = con.execute('''
        SELECT s.phone FROM sessions s
        WHERE s.status = 'active'
          AND s.reminded_at IS NULL
          AND (SELECT COUNT(*) FROM messages m WHERE m.phone = s.phone) > 0
          AND (SELECT MAX(m.created_at) FROM messages m WHERE m.phone = s.phone)
              < datetime('now', '-3 hours')
    ''').fetchall()
    con.close()
    return [r[0] for r in rows]
