# db.py — SQLite session storage
import sqlite3, os
from datetime import datetime

DB = os.getenv('DB_PATH', 'casad.db')


def init_db():
    con = sqlite3.connect(DB)
    con.execute('''CREATE TABLE IF NOT EXISTS sessions
        (phone TEXT PRIMARY KEY, bridge TEXT, status TEXT,
         state TEXT, photo_count INTEGER,
         started_at TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS messages
        (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT,
         type TEXT, content TEXT, media_path TEXT,
         category TEXT, photo_num INTEGER,
         seq INTEGER, created_at TEXT, image_data BLOB)''')
    # Safe migrations for existing DBs
    for col_sql in (
        'ALTER TABLE messages ADD COLUMN image_data BLOB',
        'ALTER TABLE messages ADD COLUMN category TEXT',
        'ALTER TABLE messages ADD COLUMN photo_num INTEGER',
        'ALTER TABLE sessions ADD COLUMN state TEXT',
        'ALTER TABLE sessions ADD COLUMN photo_count INTEGER',
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
        'INSERT OR IGNORE INTO sessions VALUES (?,?,?,?,?,?)',
        (msg['phone'], msg.get('bridge', ''), 'active',
         'menu', 0, datetime.utcnow().isoformat())
    )
    con.execute(
        'INSERT INTO messages VALUES (NULL,?,?,?,?,?,?,?,datetime("now"),?)',
        (msg['phone'], msg['type'], msg.get('content'),
         msg.get('media_path'), msg.get('category'),
         msg.get('photo_num'), msg.get('seq', 0), msg.get('image_data'))
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


def get_session_state(phone: str):
    con = sqlite3.connect(DB)
    row = con.execute('SELECT state, photo_count FROM sessions WHERE phone=?', (phone,)).fetchone()
    con.close()
    return (row[0] or 'menu', row[1] or 0) if row else ('menu', 0)


def set_session_state(phone: str, state: str):
    con = sqlite3.connect(DB)
    con.execute('UPDATE sessions SET state=? WHERE phone=?', (state, phone))
    con.commit()
    con.close()


def increment_photo_count(phone: str) -> int:
    """Increment and return the new photo count for this session."""
    con = sqlite3.connect(DB)
    con.execute(
        'UPDATE sessions SET photo_count = COALESCE(photo_count, 0) + 1 WHERE phone=?',
        (phone,)
    )
    row = con.execute('SELECT photo_count FROM sessions WHERE phone=?', (phone,)).fetchone()
    con.commit()
    con.close()
    return row[0] if row else 1


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
