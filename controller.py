#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WYM Radio / scldl Control Bot + Worker (single process, Railway)
- Polls Bale for admin commands (admin-only whitelist)
- Worker thread downloads playlist episodes → PicoFile (original quality) → Bale post
- Full control: start/stop, pause/resume, change playlist, goto/skip, link-to-music,
  send single/group, view playlist, resend from PicoFile, stats, interval, delete/reset
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
DB_PATH = os.environ.get("DB_PATH", "/app/data/synced_episodes.db" if os.path.isdir("/app") else "/home/ersaz/scldl-bot/synced_episodes.db")
try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except Exception:
    pass

BOT = f"https://tapi.bale.ai/bot{BALE_TOKEN}"
_handlers = [logging.StreamHandler(sys.stdout)]
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=_handlers)
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
    c.execute("""CREATE TABLE IF NOT EXISTS skipped (url TEXT PRIMARY KEY)""")
    c.commit(); c.close()
    init_db()  # synced table

def add_skip(url):
    c = conn(); c.execute("INSERT OR IGNORE INTO skipped(url) VALUES(?)", (url,)); c.commit(); c.close()

def is_skipped(url):
    c = conn(); r = c.execute("SELECT 1 FROM skipped WHERE url=?", (url,)).fetchone(); c.close(); return bool(r)

def synced_count():
    c = conn(); r = c.execute("SELECT COUNT(*) FROM synced").fetchone(); c.close(); return r[0] if r else 0

def envelope(text, chat_id, parse_mode="HTML"):
    try:
        requests.post(f"{BOT}/sendMessage", data={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, timeout=30)
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
        pico_url, p_err = picofile.upload_to_picofile(raw_path)
        if not pico_url:
            log.error(f"[{idx}/{total}] PicoFile error: {p_err}")
            return False, p_err
        log.info(f"[{idx}/{total}] PicoFile: {pico_url}")
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
            # 1) if paused → wait
            if not is_running():
                time.sleep(3)
                continue
            # 2) next playlist
            playlist = cfg_get("playlist_url", "https://soundcloud.com/cosmicgateofficial/sets/cosmic-gate-wym-radio")
            chat_id = int(cfg_get("chat_id", str(DEFAULT_CHAT)))
            interval = int(cfg_get("interval_sec", "0"))
            entries = get_playlist_entries(playlist)
            total = len(entries)
            with _lock:
                _state["current_total"] = total
            log.info(f"Playlist {playlist}: {total} entries")
            for idx, entry in enumerate(entries, 1):
                if not is_running():
                    set_running(False)
                    log.info("Worker paused/stopped mid-playlist")
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
                # pacing
                el = 0
                try:
                    st = time.time()
                except Exception:
                    st = 0
                if interval > 0:
                    left = max(5, int(interval - (time.time() - iter_start_time() if False else 0)))
                    time.sleep(left if left > 0 else 5)
            # playlist drained (or all synced)
            left = [e for e in entries if not is_synced(e.get("url", "")) and not is_skipped(e.get("url", ""))]
            if not left:
                log.info("All entries done → auto-stop")
                set_running(False)
            time.sleep(4)
        except Exception as e:
            log.exception(f"worker_loop error: {e}")
            time.sleep(10)

def iter_start_time():
    return time.time()

def link_queue_loop():
    """Dedicated thread: process link tasks immediately, independent of playlist worker."""
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
    try:
        tmp = tempfile.mkdtemp(prefix="link_")
        out_tmpl = os.path.join(tmp, "%(title).80s.%(ext)s")
        r = subprocess.run(["yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3",
                            "--audio-quality", "0", "--embed-metadata", "--write-thumbnail",
                            "--convert-thumbnails", "jpg", "-o", out_tmpl, url],
                           capture_output=True, text=True, timeout=600)
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
        # duration probe for nice caption
        dur = 0
        try:
            pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                                capture_output=True, text=True, timeout=15)
            dur = float(json.loads(pr.stdout).get("format", {}).get("duration", 0))
        except Exception:
            pass
        # upload to picofile (always, gives direct full-quality link)
        pico_url, p_err = picofile.upload_to_picofile(path)
        if not pico_url:
            raise RuntimeError(p_err)
        # send to bale (respects 50MB guard; >50MB → cover+link post)
        send_to_bale(path, os.path.basename(os.path.splitext(path)[0]), pico_url, dur, cover)
        msg = f"✅ <b>{os.path.basename(os.path.splitext(path)[0])}</b>\n📦 {sz:.1f} MB\n🔗 {pico_url}"
        ok = True
    except Exception as e:
        log.exception(f"link task failed: {e}")
        msg = f"❌ پردازش لینک ناموفق:\n<code>{str(e)[:500]}</code>"
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    envelope(msg, chat_id)
    c = conn()
    c.execute("UPDATE link_queue SET status='done' WHERE url=?", (url,))
    c.commit(); c.close()
    if ok:
        c = conn()
        c.execute("DELETE FROM link_queue WHERE url=?", (url,))
        c.commit(); c.close()

# ---------------------------------------------------------------- bot commands
def cmd_help(chat):
    envelope(
        "🤖 <b>ربات دانلودر ابری</b>\n"
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
        "/link <url> [chat_id] — دانلود و ارسال به تکی/گروه\n"
        "/send <pico_url> — دانلود از پیکوفایل و ارسال مجدد به بله\n"
        "/chat <id> — تغییر مقصد ارسال (تکی یا گروه)\n\n"
        "📊 <b>وضعیت:</b>\n"
        "/status — وضعیت کامل\n"
        "/stats — آمار سینک‌شده\n"
        "/del <پیکوurl> — حذف از لیست سینک‌شده (دانلود مجدد)\n"
        "/help — این راهنما\n\n"
        "🔐 فقط مدیر دسترسی دارد", chat)

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
    env = envelope
    env(f"📊 <b>وضعیت ربات</b>\n"
        f"───────\n"
        f"⏯ وضعیت: {'🟢 در حال دانلود' if running else '🟤 متوقف'}\n"
        f"📃 پلی‌لیست: <code>{playlist[:60]}</code>\n"
        f"➡️ در حال: <code>{cur}</code>\n"
        f"📍 موقعیت: {idx}/{total}\n"
        f"✅ سینک‌شده: {n} اپیزود\n"
        f"📨 مقصد: <code>{chat_id}</code>\n"
        f"⏱ فاصله: {interval}s\n"
        f"🕒 آخرین: {last}", chat)

def cmd_stats(chat):
    n = synced_count()
    c = conn()
    recent = c.execute("SELECT title, synced_at FROM synced ORDER BY synced_at DESC LIMIT 6").fetchall()
    c.close()
    lines = "\n".join(f"• {t} (<i>{st[:16]}</i>)" for t, st in recent) or "—"
    envelope(f"📈 <b>آمار</b>\nسینک‌شده: <b>{n}</b> اپیزود\n───────\nآخرین‌ها:\n{lines}", chat)

def cmd_list(chat):
    playlist = cfg_get("playlist_url", "https://soundcloud.com/cosmicgateofficial/sets/cosmic-gate-wym-radio")
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
    playlist = cfg_get("playlist_url", "https://soundcloud.com/cosmicgateofficial/sets/cosmic-gate-wym-radio")
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
    playlist = cfg_get("playlist_url", "https://soundcloud.com/cosmicgateofficial/sets/cosmic-gate-wym-radio")
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

def handle_command(text, chat_id):
    text = text.strip()
    if not text.startswith("/"):
        envelope("🤖 فقط دستورات / مجاز است. /help", chat_id)
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
    elif cmd == "/help" or cmd == "/start":
        cmd_help(chat_id)
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