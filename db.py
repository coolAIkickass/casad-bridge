# db.py — SQLite session storage
import sqlite3, os
from datetime import datetime

DB = os.getenv('DB_PATH', 'casad.db')


def init_db():
    con = sqlite3.connect(DB)
    con.execute('''CREATE TABLE IF NOT EXISTS sessions
        (phone TEXT PRIMARY KEY, bridge TEXT, status TEXT, started_at TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS messages
        (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT,
         type TEXT, content TEXT, media_path TEXT,
         seq INTEGER, created_at TEXT)''')
    con.commit()
    con.close()


def store_message(msg):
    con = sqlite3.connect(DB)
    # Upsert session row if this is the first message from this phone
    con.execute(
        'INSERT OR IGNORE INTO sessions VALUES (?,?,?,?)',
        (msg['phone'], msg.get('bridge', ''), 'active', datetime.utcnow().isoformat())
    )
    con.execute(
        'INSERT INTO messages VALUES (NULL,?,?,?,?,?,datetime("now"))',
        (msg['phone'], msg['type'], msg.get('content'),
         msg.get('media_path'), msg.get('seq', 0))
    )
    con.commit()
    con.close()


def get_session(phone):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    session = con.execute('SELECT * FROM sessions WHERE phone=?', (phone,)).fetchone()
    messages = con.execute(
        'SELECT * FROM messages WHERE phone=? ORDER BY seq, created_at', (phone,)
    ).fetchall()
    con.close()
    if not session:
        return None
    return {**dict(session), 'messages': [dict(m) for m in messages]}


def mark_done(phone):
    con = sqlite3.connect(DB)
    con.execute("UPDATE sessions SET status='done' WHERE phone=?", (phone,))
    con.commit()
    con.close()
