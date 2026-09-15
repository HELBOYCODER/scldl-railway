#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Batch Downloader & Dual Cloud Publisher
Syncs all episodes of Cosmic Gate - WYM Radio (469 episodes)
to Bale (@cloudmelodbot -> chat 1446119540) and PicoFile.
Attaches PicoFile direct download link inside Bale audio caption!
Runs as background daemon on Alwaysdata.
"""
import os
import sys
import time
import json
import shutil
import sqlite3
import logging
import tempfile
import subprocess
import requests

import picofile

HOME = os.path.expanduser("~")
os.environ["PATH"] = f"{HOME}/bin:{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")

PLAYLIST_URL = "https://soundcloud.com/cosmicgateofficial/sets/cosmic-gate-wym-radio"
BALE_TOKEN = os.environ.get("BALE_TOKEN", "1629720660:c-U5awHAgXHUm7XqzR5HjHa4CMRFGmHBilI")
BALE_CHAT_ID = os.environ.get("BALE_CHAT_ID", "1446119540")
DB_PATH = os.environ.get("DB_PATH", "/app/data/synced_episodes.db" if os.path.isdir("/app") else "/home/ersaz/scldl-bot/synced_episodes.db")
LOG_PATH = os.environ.get("LOG_PATH", "/app/data/sync.log" if os.path.isdir("/app") else "/home/ersaz/scldl-bot/sync.log")
# ensure dirs exist for Railway (/app/data) and legacy Alwaysdata
for _p in (DB_PATH, LOG_PATH):
    try: os.makedirs(os.path.dirname(_p), exist_ok=True)
    except Exception: pass
_log_handlers = [logging.StreamHandler(sys.stdout)]
if os.path.isdir(os.path.dirname(LOG_PATH)):
    try: _log_handlers.append(logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8"))
    except Exception: pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=_log_handlers)
logger = logging.getLogger("sync-wym")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS synced (
            url TEXT PRIMARY KEY,
            title TEXT,
            pico_url TEXT,
            bale_msg_id INTEGER,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_synced(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM synced WHERE url = ?", (url,))
    res = c.fetchone()
    conn.close()
    return bool(res)

def mark_synced(url, title, pico_url, bale_msg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO synced (url, title, pico_url, bale_msg_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            pico_url = excluded.pico_url,
            bale_msg_id = excluded.bale_msg_id,
            synced_at = CURRENT_TIMESTAMP
    """, (url, title, pico_url, bale_msg_id))
    conn.commit()
    conn.close()

def get_playlist_entries():
    logger.info(f"Extracting playlist entries from: {PLAYLIST_URL}")
    cmd = ["yt-dlp", "--flat-playlist", "--dump-single-json", PLAYLIST_URL]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"Failed to fetch playlist: {p.stderr[:300]}")
    data = json.loads(p.stdout)
    entries = data.get("entries", [])
    logger.info(f"Found {len(entries)} total episodes in playlist.")
    return entries

def download_raw_track(track_url, tmp_dir):
    out_tmpl = os.path.join(tmp_dir, "%(title).80s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--remote-components", "ejs:github",
        "--no-playlist",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",  # Highest bitrate / 100% original quality
        "--embed-metadata",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "-o", out_tmpl,
        track_url
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"Download failed: {p.stderr[-300:]}")

    files = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir) if f.endswith(".mp3")]
    if not files:
        raise RuntimeError("No MP3 file produced")
    raw_path = files[0]
    # find cover if any
    covers = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir) if f.lower().endswith((".jpg",".jpeg",".webp",".png")) and os.path.getsize(os.path.join(tmp_dir,f))>1024]
    cover_path = covers[0] if covers else None
    # if webp keep but Bale prefers jpg — we already converted to jpg

    # Get title and metadata
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=title,artist:format=duration", "-of", "json", raw_path],
        capture_output=True, text=True
    )
    title = "WYM Radio Episode"
    duration_s = 3600
    try:
        pdata = json.loads(probe.stdout)
        title = pdata.get("format", {}).get("tags", {}).get("title") or title
        duration_s = float(pdata.get("format", {}).get("duration", 3600))
    except Exception:
        pass

    return raw_path, title, duration_s, cover_path


def download_from_picofile_resume(pico_url, dest_path, chunk=1024*256):
    """Download file from PicoFile URL with HTTP Range resume (تیکه‌تیکه) — for re-sending to Bale."""
    # PicoFile direct link is the final /f/{slug}/{filename} redirect; we follow redirects via Range loop
    tmp = dest_path + ".part"
    start = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
    while True:
        hdr = {"Range": f"bytes={start}-"} if start else {}
        with requests.get(pico_url, headers=hdr, stream=True, timeout=60, allow_redirects=True) as r:
            if r.status_code not in (200,206):
                raise RuntimeError(f"PicoFile download HTTP {r.status_code}")
            total = int(r.headers.get("Content-Range","/").split("/")[-1] or r.headers.get("Content-Length","0")) if r.headers.get("Content-Range") or r.headers.get("Content-Length") else None
            mode = "ab" if start and r.status_code==206 else "wb"
            if mode=="wb": start=0
            with open(tmp, mode) as f:
                for c in r.iter_content(chunk_size=chunk):
                    if c: f.write(c); start+=len(c)
            if total and start>=total: break
            if r.status_code==200: break
            # need next range? loop again if server returned 206 with remaining
            if r.headers.get("Content-Range"): break
            break
    os.rename(tmp, dest_path)
    return dest_path

def send_to_bale(filepath, title, pico_url, duration_s, cover_path=None):
    """Send full file without split, with cover as photo post (ponytail: Bale 50MB nginx hard limit — if 413, we keep PicoFile full link + cover photo as complete post)."""
    bale_url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendAudio"
    m, s = divmod(int(duration_s), 60)
    h, m = divmod(m, 60)
    dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    # detect size for caption
    try: sz_mb = os.path.getsize(filepath)/(1024*1024)
    except: sz_mb = 0
    caption = (
        f"🎧 <b>{title}</b>\n"
        f"⏱ مدت: {dur_str} • 📦 {sz_mb:.1f} MB\n\n"
        f"📥 <b>دانلود با کیفیت اصلی (کامل):</b>\n"
        f"{pico_url}\n\n"
        f"🤖 @cloudmelodbot"
    )
    filename = os.path.basename(filepath)
    # 1) If cover exists, send it as photo post first (like a complete post)
    photo_msg_id = None
    if cover_path and os.path.isfile(cover_path):
        try:
            with open(cover_path, "rb") as cf:
                rr = requests.post(f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendPhoto",
                    data={"chat_id": BALE_CHAT_ID, "caption": caption, "parse_mode":"HTML"},
                    files={"photo": (os.path.basename(cover_path), cf, "image/jpeg")}, timeout=60)
                dd = rr.json()
                if dd.get("ok"):
                    photo_msg_id = dd["result"]["message_id"]
                    logger.info(f"Cover photo sent (msg {photo_msg_id})")
        except Exception as e:
            logger.warning(f"Cover photo send failed: {e}")
    # 2) Try to send full audio file directly (no split). If Bale 413, we keep photo post as complete fallback.
    try:
        with open(filepath, "rb") as f:
            res = requests.post(
                bale_url,
                data={"chat_id": BALE_CHAT_ID, "caption": caption, "parse_mode":"HTML", "title": title, "performer": "Cosmic Gate"},
                files={"audio": (filename, f, "audio/mpeg")}, timeout=600)
        d = res.json()
        if d.get("ok"):
            return d["result"]["message_id"]
        desc = d.get("description","")
        if "413" in str(desc) or "too large" in str(desc).lower() or "Request Entity Too Large" in str(desc):
            logger.warning(f"Bale 413 for full {sz_mb:.1f}MB — keeping cover+link as complete post (PicoFile has full file)")
            return photo_msg_id  # fallback: photo post is the complete post
        raise RuntimeError(f"Bale send failed: {desc}")
    except Exception as e:
        if "413" in str(e) or "too large" in str(e).lower():
            logger.warning(f"Bale 413 fallback to cover+link: {e}")
            return photo_msg_id
        raise

INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "0"))  # 0 = zero-delay from start (user asked), was 300

def sync_all():
    # user asked: zero schedule + restart from beginning — RESET before init_db (ponytail: one-time wipe)
    if os.environ.get("RESET_DB", "1") == "1":
        try:
            if os.path.isfile(DB_PATH):
                os.remove(DB_PATH)
                logger.info(f"RESET_DB=1 → removed {DB_PATH} to restart from beginning")
        except Exception as e:
            logger.warning(f"RESET_DB delete failed: {e}")
    init_db()
    mode = "ZERO-DELAY lossless" if INTERVAL_SECONDS==0 else f"{INTERVAL_SECONDS}s interval"
    logger.info(f"=== Starting Automatic Batch Sync Daemon ({mode}) ===")
    
    entries = get_playlist_entries()
    total = len(entries)

    for idx, entry in enumerate(entries, 1):
        url = entry.get("url")
        if not url:
            continue

        if is_synced(url):
            logger.info(f"[{idx}/{total}] Already synced, skipping: {url}")
            continue

        iter_start = time.time()
        logger.info(f"[{idx}/{total}] Downloading & processing: {url}")
        tmp_dir = tempfile.mkdtemp(prefix="wym_")
        try:
            # 1. Download RAW uncompressed audio (100+ MB, original quality)
            raw_path, title, duration_s, cover_path = download_raw_track(url, tmp_dir)
            raw_size = os.path.getsize(raw_path)
            logger.info(f"[{idx}/{total}] Raw original file: {title} ({raw_size/(1024*1024):.1f} MB)")

            # 2. Upload the 100% UNTOUCHED RAW ORIGINAL file to PicoFile (No size limit on PicoFile!)
            logger.info(f"[{idx}/{total}] Uploading 100% RAW ORIGINAL file to PicoFile ({raw_size/(1024*1024):.1f} MB)...")
            pico_url, p_err = picofile.upload_to_picofile(raw_path)
            if not pico_url:
                logger.error(f"[{idx}/{total}] PicoFile upload error: {p_err}")
                pico_url = "https://www.picofile.com"
            else:
                logger.info(f"[{idx}/{total}] PicoFile ORIGINAL quality link: {pico_url}")

            # 3. Send full file with cover as complete post (no split — user asked)
            logger.info(f"[{idx}/{total}] Sending full file ({raw_size/(1024*1024):.1f} MB) with cover to Bale...")
            bale_msg_id = send_to_bale(raw_path, title, pico_url, duration_s, cover_path)
            logger.info(f"[{idx}/{total}] Successfully delivered to Bale! (msg_id: {bale_msg_id})")

            # 5. Mark synced in DB
            mark_synced(url, title, pico_url, bale_msg_id)
            logger.info(f"[{idx}/{total}] Synced successfully: {title}")

        except Exception as e:
            logger.exception(f"[{idx}/{total}] Error processing {url}: {e}")
            time.sleep(10)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # pacing: 0 = zero-delay start now (user asked), else INTERVAL_SECONDS
        elapsed = time.time() - iter_start
        if INTERVAL_SECONDS > 0:
            wait_sec = max(15, int(INTERVAL_SECONDS - elapsed))
            logger.info(f"[{idx}/{total}] Finished in {elapsed:.1f}s. Sleeping {wait_sec}s...")
            time.sleep(wait_sec)
        else:
            logger.info(f"[{idx}/{total}] Finished in {elapsed:.1f}s. ZERO-DELAY → next immediately.")

    logger.info("=== All episodes have been synced successfully! ===")

if __name__ == "__main__":
    sync_all()
