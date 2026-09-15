#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WYM Radio / scldl Control Bot + Worker (single process, Railway)
- Polls Bale for updates: commands (/...), inline buttons (callback_data), and raw links
- Auto-detects link type: single track vs playlist/album/set → offers download choice
- Worker thread downloads playlist episodes → PicoFile (original quality) → Bale post
- Full control via buttons and commands: start/stop/resume, change playlist, goto/skip,
  link-to-music, send single/group, view playlist, resend from PicoFile, stats, interval
"""
import os
import sys
import time
import json
import shutil
import sqlite3
import logging
import tempfile
import threading
import subprocess
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import picofile
import toolschi


def upload_best(path):
    """Upload to toolschi (≤100MB, free) else PicoFile fallback (>100MB).
    Returns (url, uploader_name)."""
    sz = os.path.getsize(path) / (1024 * 1024)
    if sz <= toolschi.MAX_SIZE / (1024 * 1024):
        url, err = toolschi.upload_to_toolschi(path)
        if url:
            return url, "toolschi"
        log.warning(f"toolschi failed ({err}); falling back to picofile")
    url, err = picofile.upload_to_picofile(path)
    if url:
        return url, "picofile"
    return None, err
import sync_playlist  # reuse download_raw_track, send_to_bale, etc.
from sync_playlist import (
    init_db, is_synced, mark_synced, get_playlist_entries,
    download_raw_track, send_to_bale, logger
)

# ---------------------------------------------------------------- config
HOME = os.path.expanduser("~")
os.environ["PATH"] = f"{HOME}/bin:{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")

BALE_TOKEN = os.environ.get("BALE_TOKEN", "1629720660:c-U5awHAgXHUm7XqzR5HjHa4CMRFGmHBilI")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "8874504954").split(",") if x.strip()]
DEFAULT_CHAT = int(os.environ.get("BALE_CHAT_ID", "1446119540"))
DEFAULT_PLAYLIST = "https://soundcloud.com/cosmicgateofficial/sets/cosmic-gate-wym-radio"
DB_PATH = os.environ.get("DB_PATH", "/app/data/synced_episodes.db" if os.path.isdir("/app") else "/home/ersaz/scldl-bot/synced_episodes.db")
try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except Exception:
    pass

BOT = f"https://tapi.bale.ai/bot{BALE_TOKEN}"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("control")

# ---------------------------------------------------------------- db helpers
def conn():
    return sqlite3.connect(DB_PATH, timeout=30)

def cfg_get(key, default=None):
    try:
        c = conn(); r = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone(); c.close()
        return r[0] if r else default
    except Exception:
        return default

def cfg_set(key, value):
    c = conn()
    c.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    c.commit(); c.close()

def ensure_schema():
    c = conn()
    c.execute("""CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS link_queue (
        url TEXT PRIMARY KEY, title TEXT, chat_id INTEGER, status TEXT DEFAULT 'pending',
        msg TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, chat_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS skipped (url TEXT PRIMARY KEY)""")
    c.commit(); c.close()
    init_db()  # synced table

def add_skip(url):
    c = conn(); c.execute("INSERT OR IGNORE INTO skipped(url) VALUES(?)", (url,)); c.commit(); c.close()

def is_skipped(url):
    c = conn(); r = c.execute("SELECT 1 FROM skipped WHERE url=?", (url,)).fetchone(); c.close(); return bool(r)

def synced_count():
    c = conn(); r = c.execute("SELECT COUNT(*) FROM synced").fetchone(); c.close(); return r[0] if r else 0

def store_pending_link(url, chat_id):
    c = conn()
    cur = c.execute("INSERT INTO pending_links(url,chat_id) VALUES(?,?)", (url, chat_id))
    c.commit(); _id = cur.lastrowid; c.close()
    return _id

def get_pending_link(pid):
    c = conn(); r = c.execute("SELECT url FROM pending_links WHERE id=?", (pid,)).fetchone(); c.close()
    return r[0] if r else None

# ---------------------------------------------------------------- bale send helpers
def inline(rows):
    return {"inline_keyboard": rows}

def envelope(text, chat_id, parse_mode="HTML", reply_markup=None):
    try:
        data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        requests.post(f"{BOT}/sendMessage", data=data, timeout=30)
    except Exception as e:
        log.error(f"sendMessage failed: {e}")

def send_doc(chat_id, path, caption=None):
    try:
        with open(path, "rb") as f:
            r = requests.post(f"{BOT}/sendDocument", data={"chat_id": chat_id, "caption": caption or ""},
                              files={"document": (os.path.basename(path), f)}, timeout=120)
        return r.json().get("ok"), r.json().get("result", {}).get("message_id")
    except Exception as e:
        return False, str(e)

def answer_cb(cq_id):
    try:
        requests.post(f"{BOT}/answerCallbackQuery", data={"callback_query_id": cq_id}, timeout=15)
    except Exception:
        pass

# ---------------------------------------------------------------- worker state
_state = {"running": False, "current_link": None, "current_title": None,
          "current_idx": 0, "current_total": 0, "last_done": "", "started_at": None}
_lock = threading.Lock()

def set_running(v):
    with _lock:
        _state["running"] = bool(v)
    cfg_set("running", "1" if v else "0")

def is_running():
    with _lock:
        return _state["running"]

# ---------------------------------------------------------------- worker: playlist sync
def process_one_entry(entry, idx, total, chat_id, pico_only=False):
    """Download → PicoFile (original) → Bale post. Returns (ok, info)."""
    url = entry.get("url")
    if not url:
        return False, "no url"
    iter_start = time.time()
    tmp_dir = tempfile.mkdtemp(prefix="wym_")
    try:
        raw_path, title, duration_s, cover_path = download_raw_track(url, tmp_dir)
        raw_size = os.path.getsize(raw_path)
        log.info(f"[{idx}/{total}] raw original: {title} ({raw_size/(1024*1024):.1f} MB)")
        pico_url, p_err = upload_best(raw_path)
        if not pico_url:
            log.error(f"[{idx}/{total}] Upload error: {p_err}")
            return False, p_err
        log.info(f"[{idx}/{total}] uploaded: {pico_url}")
        bale_msg_id = None
        if not pico_only:
            bale_msg_id = send_to_bale(raw_path, title, pico_url, duration_s, cover_path)
        mark_synced(url, title, pico_url, bale_msg_id)
        info = {"title": title, "pico": pico_url, "msg": bale_msg_id, "secs": round(time.time() - iter_start, 1)}
        return True, info
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def worker_loop():
    ensure_schema()
    log.info("Worker thread started")
    while True:
        try:
            if not is_running():
                time.sleep(3)
                continue
            playlist = cfg_get("playlist_url", DEFAULT_PLAYLIST)
            chat_id = int(cfg_get("chat_id", str(DEFAULT_CHAT)))
            interval = int(cfg_get("interval_sec", "0"))
            entries = get_playlist_entries(playlist)
            total = len(entries)
            with _lock:
                _state["current_total"] = total
            log.info(f"Playlist {playlist[:70]}: {total} entries")
            changed = False
            for idx, entry in enumerate(entries, 1):
                if not is_running():
                    set_running(False)
                    log.info("Worker paused/stopped mid-playlist")
                    break
                # если playlist changed mid-run → restart from new playlist immediately
                cur_pl = cfg_get("playlist_url", DEFAULT_PLAYLIST)
                if cur_pl != playlist:
                    log.info("Playlist changed mid-run → switching")
                    changed = True
                    break
                url = entry.get("url")
                if not url:
                    continue
                if is_synced(url) or is_skipped(url):
                    continue
                title = entry.get("title") or entry.get("id") or url
                with _lock:
                    _state["current_link"] = url
                    _state["current_title"] = title
                    _state["current_idx"] = idx
                log.info(f"[{idx}/{total}] processing: {title}")
                try:
                    ok, info = process_one_entry(entry, idx, total, chat_id)
                except Exception as e:
                    log.exception(f"[{idx}/{total}] error: {e}")
                    time.sleep(10)
                    continue
                if ok:
                    with _lock:
                        _state["last_done"] = f"{info['title']} ✅"
                    envelope(f"✅ <b>{info['title']}</b>\n📦 {info['secs']}s\n🔗 {info['pico']}", chat_id)
                if interval > 0:
                    time.sleep(interval)
            if changed:
                continue
            left = [e for e in entries if not is_synced(e.get("url", "")) and not is_skipped(e.get("url", ""))]
            if not left:
                log.info("All entries done → auto-stop")
                set_running(False)
            time.sleep(4)
        except Exception as e:
            log.exception(f"worker_loop error: {e}")
            time.sleep(10)

# ---------------------------------------------------------------- link queue thread
def link_queue_loop():
    ensure_schema()
    while True:
        try:
            drain_link_queue_once()
        except Exception as e:
            log.exception("link_queue_loop error: %s", e)
        time.sleep(2)

def drain_link_queue_once():
    c = conn()
    pending = c.execute("SELECT url, chat_id FROM link_queue WHERE status='pending' LIMIT 1").fetchall()
    c.close()
    if not pending:
        return
    url, chat_id = pending[0]
    c = conn()
    c.execute("UPDATE link_queue SET status='working' WHERE url=?", (url,))
    c.commit(); c.close()
    envelope(f"🎵 شروع پردازش لینک: <code>{url}</code>", chat_id)
    ok = False
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="link_")
        out_tmpl = os.path.join(tmp, "%(title).80s.%(ext)s")
        r = subprocess.run(["yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3",
                            "--audio-quality", "0", "--embed-metadata", "--write-thumbnail",
                            "--convert-thumbnails", "jpg", "-o", out_tmpl, url],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-400:])
        mp3s = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".mp3")]
        if not mp3s:
            raise RuntimeError("no mp3 produced")
        path = mp3s[0]
        covers = [os.path.join(tmp, f) for f in os.listdir(tmp)
                  if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and os.path.getsize(os.path.join(tmp, f)) > 1024]
        cover = covers[0] if covers else None
        sz = os.path.getsize(path) / (1024 * 1024)
        dur = 0
        try:
            pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                                capture_output=True, text=True, timeout=15)
            dur = float(json.loads(pr.stdout).get("format", {}).get("duration", 0))
        except Exception:
            pass
        pico_url, p_err = upload_best(path)
        if not pico_url:
            raise RuntimeError(p_err)
        send_to_bale(path, os.path.basename(os.path.splitext(path)[0]), pico_url, dur, cover)
        msg = f"✅ <b>{os.path.basename(os.path.splitext(path)[0])}</b>\n📦 {sz:.1f} MB\n🔗 {pico_url}"
        ok = True
    except Exception as e:
        log.exception(f"link task failed: {e}")
        msg = f"❌ پردازش لینک ناموفق:\n<code>{str(e)[:500]}</code>"
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    envelope(msg, chat_id)
    c = conn()
    c.execute("UPDATE link_queue SET status='done' WHERE url=?", (url,))
    c.commit(); c.close()
    if ok:
        c = conn()
        c.execute("DELETE FROM link_queue WHERE url=?", (url,))
        c.commit(); c.close()

# ---------------------------------------------------------------- link type detection
def detect_link(url):
    """Return (kind, count, title) where kind ∈ {'track','playlist'}."""
    cmd = ["yt-dlp", "--flat-playlist", "--dump-single-json", "--no-download", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    d = json.loads(r.stdout)
    entries = d.get("entries") or []
    title = d.get("title") or d.get("id") or url
    if len(entries) > 1:
        return "playlist", len(entries), title
    if len(entries) == 1:
        # single-entry playlist link (e.g. album page with 1 song) → treat as track
        return "track", 1, title
    return "track", 1, title

def handle_incoming_link(text, chat_id):
    """A raw http(s) link was sent → detect type → offer buttons."""
    envelope("🔍 در حال تشخیص نوع لینک…", chat_id)
    try:
        kind, count, title = detect_link(text)
    except Exception as e:
        envelope(f"❌ نتونستم نوع لینک رو تشخیص بدم:\n<code>{str(e)[:250]}</code>", chat_id)
        return
    if kind == "playlist":
        pid = store_pending_link(text, chat_id)
        kb = inline([
            [{"text": f"📥 دانلود کل پلی‌لیست ({count} مورد)", "callback_data": f"dl:pl:{pid}"}],
            [{"text": "🎵 فقط این‌یکی به‌عنوان تک‌آهنگ", "callback_data": f"dl:track:{pid}"}],
        ])
        envelope(f"📃 <b>پلی‌لیست/آلبوم تشخیص داده شد</b>\n🎶 <code>{title[:60]}</code>\n🔢 <b>{count}</b> مورد\n\nچه‌کار کنم؟", chat_id, reply_markup=kb)
    else:
        pid = store_pending_link(text, chat_id)
        kb = inline([
            [{"text": "⬇️ دانلود تک‌آهنگ", "callback_data": f"dl:track:{pid}"}],
        ])
        envelope(f"🎵 <b>تک‌آهنگ تشخیص داده شد</b>\n🎶 <code>{title[:60]}</code>\n\nدانلودش کنم؟", chat_id, reply_markup=kb)

# ---------------------------------------------------------------- bot commands
def cmd_menu(chat_id):
    kb = inline([
        [{"text": "▶️ شروع", "callback_data": "m:start"},
         {"text": "⏸ توقف", "callback_data": "m:stop"},
         {"text": "🔄 ادامه", "callback_data": "m:resume"}],
        [{"text": "📊 وضعیت", "callback_data": "m:status"},
         {"text": "📈 آمار", "callback_data": "m:stats"}],
        [{"text": "📃 لیست پلی‌لیست", "callback_data": "m:list"},
         {"text": "🗑 پاک‌کردن همه", "callback_data": "m:reset"}],
        [{"text": "⏭ رد ۱", "callback_data": "m:skip1"},
         {"text": "⏭ رد ۵", "callback_data": "m:skip5"},
         {"text": "⏭ رد ۱۰", "callback_data": "m:skip10"}],
        [{"text": "⏱ فاصله ۰", "callback_data": "m:int0"},
         {"text": "⏱ فاصله ۳۰۰", "callback_data": "m:int300"},
         {"text": "⏱ فاصله ۶۰۰", "callback_data": "m:int600"}],
        [{"text": "🔗 ارسال لینک", "callback_data": "m:link"},
         {"text": "📨 تغییر مقصد", "callback_data": "m:chat"},
         {"text": "📖 راهنما", "callback_data": "m:help"}],
    ])
    envelope(
        "🤖 <b>منوی کنترل ربات</b>\n"
        "───────\n"
        "از دکمه‌ها استفاده کن، یا هر لینکی بفرست تا خودم تشخیص بدم تک‌آهنگه یا پلی‌لیست.\n"
        "دستورات متنی هم فعال‌اند: /start /stop /status /playlist /goto /skip …",
        chat_id, reply_markup=kb)

def cmd_help(chat_id):
    env = envelope
    env("🤖 <b>ربات دانلودر ابری</b>\n"
        "───────\n"
        "🎛 <b>کنترل دانلود:</b>\n"
        "/start — شروع دانلود پلی‌لیست\n"
        "/stop — توقف (مکث) دانلود\n"
        "/resume — ادامه از همان‌جا\n"
        "/interval <ثانیه> — فاصله بین اپیزودها (0=بدون توقف)\n\n"
        "📃 <b>پلی‌لیست:</b>\n"
        "/playlist <url> — تغییر پلی‌لیست\n"
        "/list — مشاهده پلی‌لیست فعلی و وضعیت\n"
        "/goto <n> — شروع از اپیزود n\n"
        "/skip <n> — رد کردن n اپیزود بعدی\n"
        "/reset — پاک‌کردن همه و شروع از اول\n\n"
        "🔗 <b>تبدیل لینک به موزیک:</b>\n"
        "«هر لینکی بفرست» — خودم تشخیص می‌دم تک‌آهنگ یا پلی‌لیست\n"
        "/link <url> [chat_id] — دانلود و ارسال به تکی/گروه\n"
        "/send <pico_url> — دانلود از پیکوفایل و ارسال مجدد به بله\n"
        "/chat <id> — تغییر مقصد ارسال (تکی یا گروه)\n\n"
        "📊 <b>وضعیت:</b>\n"
        "/status — وضعیت کامل\n"
        "/stats — آمار سینک‌شده\n"
        "/del <پیکوurl> — حذف از لیست سینک‌شده (دانلود مجدد)\n"
        "/help — این راهنما\n\n"
        "🔐 فقط مدیر دسترسی دارد", chat_id)

def cmd_status(chat):
    with _lock:
        running = _state["running"]
        cur = _state["current_title"] or "-"
        idx = _state["current_idx"]
        total = _state["current_total"]
        last = _state["last_done"] or "-"
    playlist = cfg_get("playlist_url", "پیش‌فرض Cosmic Gate WYM")
    chat_id = cfg_get("chat_id", str(DEFAULT_CHAT))
    interval = cfg_get("interval_sec", "0")
    n = synced_count()
    kb = inline([[{"text": "▶️ شروع", "callback_data": "m:start"},
                  {"text": "⏸ توقف", "callback_data": "m:stop"},
                  {"text": "🔄 ادامه", "callback_data": "m:resume"}]])
    envelope(f"📊 <b>وضعیت ربات</b>\n"
        f"───────\n"
        f"⏯ وضعیت: {'🟢 در حال دانلود' if running else '🟤 متوقف'}\n"
        f"📃 پلی‌لیست: <code>{playlist[:60]}</code>\n"
        f"➡️ در حال: <code>{cur}</code>\n"
        f"📍 موقعیت: {idx}/{total}\n"
        f"✅ سینک‌شده: {n} اپیزود\n"
        f"📨 مقصد: <code>{chat_id}</code>\n"
        f"⏱ فاصله: {interval}s\n"
        f"🕒 آخرین: {last}", chat, reply_markup=kb)

def cmd_stats(chat):
    n = synced_count()
    c = conn()
    recent = c.execute("SELECT title, synced_at FROM synced ORDER BY synced_at DESC LIMIT 6").fetchall()
    c.close()
    lines = "\n".join(f"• {t} (<i>{st[:16]}</i>)" for t, st in recent) or "—"
    envelope(f"📈 <b>آمار</b>\nسینک‌شده: <b>{n}</b> اپیزود\n───────\nآخرین‌ها:\n{lines}", chat)

def cmd_list(chat):
    playlist = cfg_get("playlist_url", DEFAULT_PLAYLIST)
    try:
        entries = get_playlist_entries(playlist)
    except Exception as e:
        envelope(f"❌ خطا در دریافت پلی‌لیست: {str(e)[:200]}", chat)
        return
    total = len(entries)
    n = synced_count()
    pending = 0
    for e in entries:
        u = e.get("url", "")
        if not is_synced(u) and not is_skipped(u):
            pending += 1
    msg = [f"📃 <b>پلی‌لیست فعلی</b>\n<code>{playlist[:60]}</code>\n"
           f"کل: {total} • ✅ {n} • ⏳ باقی‌مانده: {pending}\n───────"]
    shown = 0
    for i, e in enumerate(entries, 1):
        u = e.get("url", "")
        if is_synced(u):
            mark = "✅"
        elif is_skipped(u):
            mark = "⏭"
        else:
            mark = "⬇️"
        msg.append(f"{i}. {mark} {e.get('title', '')[:50]}")
        shown += 1
        if shown >= 15:
            msg.append("…")
            break
    envelope("\n".join(msg), chat)

def cmd_goto(chat, n):
    try:
        n = int(n)
    except Exception:
        return envelope("عدد بفرست: /goto 5", chat)
    playlist = cfg_get("playlist_url", DEFAULT_PLAYLIST)
    try:
        entries = get_playlist_entries(playlist)
    except Exception as e:
        return envelope(f"❌ {str(e)[:200]}", chat)
    if n < 1 or n > len(entries):
        return envelope(f"اپیزود باید بین 1 تا {len(entries)} باشد", chat)
    mark_synced_bulk(entries[: n - 1])
    envelope(f"⏭ شروع از اپیزود <b>{n}</b> ({entries[n - 1].get('title', '')[:40]})", chat)
    if not is_running():
        set_running(True)

def mark_synced_bulk(entries):
    c = conn()
    for e in entries:
        u = e.get("url")
        if not u:
            continue
        t = e.get("title", "")
        c.execute("INSERT OR IGNORE INTO synced(url,title,pico_url,bale_msg_id) VALUES(?,?,?,?)", (u, t, "", None))
    c.commit(); c.close()

def cmd_skip(chat, n):
    try:
        n = int(n)
    except Exception:
        return envelope("عدد بفرست: /skip 3", chat)
    playlist = cfg_get("playlist_url", DEFAULT_PLAYLIST)
    try:
        entries = get_playlist_entries(playlist)
    except Exception as e:
        return envelope(f"❌ {str(e)[:200]}", chat)
    skipped = 0
    for e in entries:
        if skipped >= n:
            break
        u = e.get("url", "")
        if not is_synced(u) and not is_skipped(u):
            add_skip(u)
            skipped += 1
    envelope(f"⏭ <b>{skipped}</b> اپیزود بعدی رد شد", chat)

def cmd_playlist(chat, url):
    if not url.startswith("http"):
        return envelope("لینک معتبر بفرست: /playlist https://…", chat)
    cfg_set("playlist_url", url)
    set_running(False)
    envelope(f"📃 پلی‌لیست تغییر کرد:\n<code>{url}</code>\nبرای شروع: /start", chat)

def cmd_link(chat, arg):
    parts = arg.split()
    url = parts[0] if parts else ""
    if not url.startswith("http"):
        return envelope("لینک بفرست: /link https://… [chat_id]", chat)
    target = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else int(cfg_get("chat_id", str(DEFAULT_CHAT)))
    c = conn()
    c.execute("INSERT OR REPLACE INTO link_queue(url,chat_id,status) VALUES(?,?,'pending')", (url, target))
    c.commit(); c.close()
    envelope(f"🔗 لینک در صف قرار گرفت:\n<code>{url}</code>\n🎯 ارسال به: <code>{target}</code>", chat)

def cmd_send_pico(chat, pico_url):
    if not pico_url.startswith("http"):
        return envelope("لینک پیکوفایل بفرست: /send https://www.picofile.com/f/…", chat)
    def job():
        envelope("⬇️ در حال دریافت از پیکوفایل…", int(chat))
        try:
            dest = tempfile.mktemp(suffix=".mp3")
            sync_playlist.download_from_picofile_resume(pico_url, dest)
            sz = os.path.getsize(dest) / (1024 * 1024)
            send_to_bale(dest, os.path.basename(dest), pico_url, 0, None)
            envelope(f"✅ ارسال شد ({sz:.1f} MB)\n🔗 {pico_url}", int(chat))
            os.remove(dest)
        except Exception as e:
            envelope(f"❌ خطا: {str(e)[:300]}", int(chat))
    threading.Thread(target=job, daemon=True).start()
    envelope("🔄 در حال ارسال…", chat)

def cmd_reset(chat):
    set_running(False)
    c = conn()
    c.execute("DELETE FROM synced"); c.execute("DELETE FROM skipped"); c.execute("DELETE FROM link_queue")
    c.commit(); c.close()
    envelope("🗑 همه‌چیز پاک شد. با /start از اول شروع کن.", chat)

def cmd_del(chat, pico_url):
    c = conn()
    r = c.execute("SELECT url, title FROM synced WHERE pico_url=?", (pico_url,)).fetchone()
    found = False
    if r:
        c.execute("DELETE FROM synced WHERE pico_url=?", (pico_url,))
        found = True
    c.commit(); c.close()
    if found:
        envelope(f"🗑 حذف شد: <code>{r[1]}</code> — دوباره دانلود می‌شود", chat)
    else:
        envelope("در لیست سینک‌شده پیدا نشد (لینک دقیق /f/…/filename.mp3 بفرست)", chat)

# ---------------------------------------------------------------- command / callback dispatch
def handle_command(text, chat_id):
    text = text.strip()
    if not text.startswith("/"):
        if text.startswith("http"):
            handle_incoming_link(text, chat_id)
        else:
            envelope("🤖 فقط دستورات / یا لینک بفرست. /help", chat_id)
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "/start" and arg == "":
        set_running(True)
        envelope("🟢 دانلود شروع شد!", chat_id)
    elif cmd == "/stop":
        set_running(False)
        envelope("🟤 متوقف شد. /resume برای ادامه", chat_id)
    elif cmd == "/resume":
        set_running(True)
        envelope("🟢 ادامه دادم!", chat_id)
    elif cmd in ("/help", "/menu"):
        cmd_menu(chat_id)
    elif cmd == "/start":
        cmd_menu(chat_id)
    elif cmd == "/status":
        cmd_status(chat_id)
    elif cmd == "/stats":
        cmd_stats(chat_id)
    elif cmd == "/list":
        cmd_list(chat_id)
    elif cmd == "/goto":
        cmd_goto(chat_id, arg)
    elif cmd == "/skip":
        cmd_skip(chat_id, arg)
    elif cmd == "/playlist":
        cmd_playlist(chat_id, arg)
    elif cmd == "/link":
        cmd_link(chat_id, arg)
    elif cmd == "/send":
        cmd_send_pico(chat_id, arg)
    elif cmd == "/reset":
        cmd_reset(chat_id)
    elif cmd == "/del":
        cmd_del(chat_id, arg)
    elif cmd == "/chat":
        if arg.lstrip("-").isdigit():
            cfg_set("chat_id", arg)
            envelope(f"📨 مقصد ارسال: <code>{arg}</code>", chat_id)
        else:
            envelope("آیدی بفرست: /chat 1446119540 (عدد منفی = گروه)", chat_id)
    elif cmd == "/interval":
        if arg.isdigit():
            cfg_set("interval_sec", arg)
            envelope(f"⏱ فاصله بین اپیزودها: {arg} ثانیه", chat_id)
        else:
            envelope("عدد بفرست: /interval 300", chat_id)
    else:
        envelope("دستور ناشناخته. /help", chat_id)

def handle_callback(data, chat_id):
    """Callback button presses."""
    if data.startswith("m:"):
        m = data[2:]
        if m == "start":
            set_running(True); envelope("🟢 دانلود شروع شد!", chat_id)
        elif m == "stop":
            set_running(False); envelope("🟤 متوقف شد.", chat_id)
        elif m == "resume":
            set_running(True); envelope("🟢 ادامه دادم!", chat_id)
        elif m == "status":
            cmd_status(chat_id)
        elif m == "stats":
            cmd_stats(chat_id)
        elif m == "list":
            cmd_list(chat_id)
        elif m == "reset":
            cmd_reset(chat_id)
        elif m == "help":
            cmd_help(chat_id)
        elif m == "skip1": cmd_skip(chat_id, "1")
        elif m == "skip5": cmd_skip(chat_id, "5")
        elif m == "skip10": cmd_skip(chat_id, "10")
        elif m == "int0":
            cfg_set("interval_sec", "0"); envelope("⏱ فاصله: ۰ (بدون توقف)", chat_id)
        elif m == "int300":
            cfg_set("interval_sec", "300"); envelope("⏱ فاصله: ۳۰۰ ثانیه", chat_id)
        elif m == "int600":
            cfg_set("interval_sec", "600"); envelope("⏱ فاصله: ۶۰۰ ثانیه", chat_id)
        elif m == "link":
            envelope("🔗 لینک آهنگ یا پلی‌لیست رو بفرست — خودم تشخیص می‌دم.", chat_id)
        elif m == "chat":
            envelope("📨 آیدی مقصد رو بفرست: <code>/chat 1446119540</code> (عدد منفی = گروه)", chat_id)
        else:
            envelope("دکمه ناشناخته.", chat_id)
    elif data.startswith("dl:track:") or data.startswith("dl:pl:"):
        try:
            pid = int(data.split(":")[2])
            url = get_pending_link(pid)
        except Exception:
            envelope("❌ لینک منقضی شده — دوباره بفرست.", chat_id)
            return
        if not url:
            envelope("❌ لینک پیدا نشد — دوباره بفرست.", chat_id)
            return
        if data.startswith("dl:track:"):
            cmd_link(chat_id, url)
        else:
            cfg_set("playlist_url", url)
            set_running(True)
            envelope(f"📃 پلی‌لیست تنظیم شد و دانلود شروع شد:\n<code>{url[:80]}</code>", chat_id)

# ---------------------------------------------------------------- polling
def poll_loop():
    ensure_schema()
    offset = 0
    log.info(f"Bot polling started. Admins: {ADMIN_IDS}")
    while True:
        try:
            r = requests.get(f"{BOT}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=45)
            d = r.json()
            if not d.get("ok"):
                time.sleep(5)
                continue
            for upd in d.get("result", []):
                offset = upd["update_id"] + 1
                # callback (button press)
                cq = upd.get("callback_query")
                if cq:
                    cq_id = cq.get("id")
                    answer_cb(cq_id)
                    from_id = (cq.get("from") or {}).get("id")
                    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id") or from_id
                    data = cq.get("data") or ""
                    if from_id not in ADMIN_IDS:
                        envelope("⛔ فقط مدیر دسترسی دارد", chat_id)
                        continue
                    log.info(f"callback from {from_id}: {data[:60]}")
                    try:
                        handle_callback(data, chat_id)
                    except Exception as e:
                        log.exception("callback error")
                        envelope(f"❌ خطای داخلی: {str(e)[:200]}", chat_id)
                    continue
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = (msg.get("text") or "").strip()
                if not text or not chat_id:
                    continue
                if chat_id not in ADMIN_IDS:
                    envelope("⛔ فقط مدیر دسترسی دارد", chat_id)
                    continue
                log.info(f"cmd from {chat_id}: {text[:80]}")
                try:
                    handle_command(text, chat_id)
                except Exception as e:
                    log.exception("handle_command error")
                    envelope(f"❌ خطای داخلی: {str(e)[:200]}", chat_id)
        except Exception as e:
            log.exception("poll_loop error")
            time.sleep(5)

# ---------------------------------------------------------------- main
if __name__ == "__main__":
    ensure_schema()
    if cfg_get("running") is None:
        cfg_set("running", "0")
    set_running(cfg_get("running", "0") == "1")
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=link_queue_loop, daemon=True).start()
    poll_loop()