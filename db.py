import sqlite3
import os

DB_PATH = "/home/ersaz/scldl-bot/users.db"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            zinc_api_key TEXT,
            pico_cookie TEXT,
            bale_token TEXT,
            bale_chat_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Add columns if migrating
    try:
        c.execute("ALTER TABLE users ADD COLUMN pico_cookie TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN bale_token TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN bale_chat_id TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

def get_user_key(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT zinc_api_key FROM users WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else None

def set_user_key(chat_id, key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (chat_id, zinc_api_key) VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET zinc_api_key = excluded.zinc_api_key
    """, (chat_id, key.strip()))
    conn.commit()
    conn.close()

def set_bale_config(chat_id, token, target_chat):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (chat_id, bale_token, bale_chat_id) VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET bale_token = excluded.bale_token, bale_chat_id = excluded.bale_chat_id
    """, (chat_id, token.strip(), target_chat.strip()))
    conn.commit()
    conn.close()

def get_bale_config(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT bale_token, bale_chat_id FROM users WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0] and row[1]:
        return row[0], row[1]
    return None, None

def set_pico_cookie(chat_id, cookie):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (chat_id, pico_cookie) VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET pico_cookie = excluded.pico_cookie
    """, (chat_id, cookie.strip()))
    conn.commit()
    conn.close()

def get_pico_cookie(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT pico_cookie FROM users WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else None
