#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scldl-bot: Universal Media Downloader & Dual Iranian Cloud Storage
Simultaneously publishes to:
 1. PicoFile (20GB Iranian cloud storage, zero ads, TUS protocol)
 2. Bale Bot CDN (National cloud infrastructure, zero VPN, direct download)
Runs 24/7 on Alwaysdata Linux host
"""
import os
import sys
import re
import time
import json
import shutil
import logging
import tempfile
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor
import requests

import db
import picofile
import bale

# Setup environment PATH
HOME = os.path.expanduser("~")
os.environ["PATH"] = f"{HOME}/bin:{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")

TOKEN = "8949154021:AAGnfAWi6S5kk3dOb3auu-nMoQvMXhUkgq0"
API_URL = f"https://api.telegram.org/bot{TOKEN}"
FILE_BASE_URL = f"https://api.telegram.org/file/bot{TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("scldl-bot")

executor = ThreadPoolExecutor(max_workers=5)

def tg_call(method, data=None, files=None, timeout=60):
    url = f"{API_URL}/{method}"
    try:
        if files:
            res = requests.post(url, data=data or {}, files=files, timeout=timeout)
        elif data:
            res = requests.post(url, json=data, timeout=timeout)
        else:
            res = requests.get(url, timeout=timeout)
        return res.json()
    except Exception as e:
        logger.error(f"Telegram API call error ({method}): {e}")
        return {"ok": False, "description": str(e)}

def send_message(chat_id, text, reply_to_message_id=None, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call("sendMessage", payload)

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call("editMessageText", payload)

def answer_callback(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return tg_call("answerCallbackQuery", payload)

def extract_url(text):
    if not text:
        return None
    match = re.search(r"https?://[^\s<>\"']+", text)
    return match.group(0) if match else None

# Default Bale Cloud CDN Configuration
DEFAULT_BALE_TOKEN = "1629720660:c-U5awHAgXHUm7XqzR5HjHa4CMRFGmHBilI"
DEFAULT_BALE_CHAT = 1446119540

def get_effective_bale_config(chat_id):
    t, c = db.get_bale_config(chat_id)
    return (t or DEFAULT_BALE_TOKEN), (c or DEFAULT_BALE_CHAT)

def upload_dual_iranian_cloud(filepath, chat_id):
    """Uploads file to PicoFile and Bale simultaneously in parallel threads"""
    pico_cookie = db.get_pico_cookie(chat_id)
    bale_token, bale_chat = get_effective_bale_config(chat_id)

    results = {"picofile": None, "bale": None}

    def _up_pico():
        try:
            url, err = picofile.upload_to_picofile(filepath, pico_cookie)
            results["picofile"] = url
            if err:
                logger.warning(f"PicoFile upload info: {err}")
        except Exception as e:
            logger.error(f"PicoFile thread error: {e}")

    def _up_bale():
        if bale_token and bale_chat:
            try:
                url, err = bale.upload_to_bale(filepath, bale_token, bale_chat)
                results["bale"] = url
                if err:
                    logger.warning(f"Bale upload info: {err}")
            except Exception as e:
                logger.error(f"Bale thread error: {e}")

    t1 = threading.Thread(target=_up_pico)
    t2 = threading.Thread(target=_up_bale)
    t1.start()
    t2.start()
    t1.join(timeout=300)
    t2.join(timeout=300)

    return results

def make_cloud_keyboard(cloud_results):
    keyboard = []
    if cloud_results.get("picofile"):
        keyboard.append([{"text": "📦 دریافت از پیکوفایل (PicoFile)", "url": cloud_results["picofile"]}])
    if cloud_results.get("bale"):
        keyboard.append([{"text": "🇮🇷 دریافت از شبکه ابری بله (Bale CDN)", "url": cloud_results["bale"]}])
    return {"inline_keyboard": keyboard} if keyboard else None

def download_and_send(chat_id, url, status_msg_id, mode="audio", reply_to_id=None):
    tmp_dir = tempfile.mkdtemp(prefix="scldl_")
    try:
        logger.info(f"Processing URL: {url} | mode={mode} | chat={chat_id}")
        edit_message(
            chat_id,
            status_msg_id,
            f"⏳ <b>در حال دانلود رسانه ({'صوت' if mode == 'audio' else 'ویدیو'})...</b>\nلطفاً چند لحظه صبر کنید."
        )

        out_template = os.path.join(tmp_dir, "%(title).100s.%(ext)s")

        if mode == "audio":
            cmd = [
                "yt-dlp",
                "--remote-components", "ejs:github",
                "--no-playlist",
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "--embed-metadata",
                "-o", out_template,
                url
            ]
        else: # video
            cmd = [
                "yt-dlp",
                "--remote-components", "ejs:github",
                "--no-playlist",
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "-o", out_template,
                url
            ]

        p = subprocess.run(cmd, capture_output=True, text=True, timeout=360)

        files = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir) if os.path.isfile(os.path.join(tmp_dir, f))]
        if not files:
            err_msg = (p.stderr or p.stdout)[-300:].strip()
            logger.error(f"yt-dlp error: {err_msg}")
            edit_message(
                chat_id,
                status_msg_id,
                f"❌ <b>خطا در پردازش لینک:</b>\n<code>{err_msg[:200]}</code>"
            )
            return

        files.sort(key=lambda x: os.path.getsize(x), reverse=True)
        filepath = files[0]
        filesize = os.path.getsize(filepath)
        filename = os.path.basename(filepath)
        filesize_mb = filesize / (1024 * 1024)

        logger.info(f"Downloaded: {filename} ({filesize_mb:.2f} MB)")

        # Case 1: Filesize > 50MB (Cannot be sent directly through Telegram bot API)
        if filesize > 50 * 1024 * 1024:
            edit_message(
                chat_id,
                status_msg_id,
                f"☁️ <b>حجم فایل ({filesize_mb:.1f} MB) بالاتر از سقف تلگرام است.</b>\nدر حال انتشار همزمان در سرورهای ابری ایرانی (پیکوفایل و بله)..."
            )
            cloud_res = upload_dual_iranian_cloud(filepath, chat_id)
            markup = make_cloud_keyboard(cloud_res)

            links_text = ""
            if cloud_res.get("picofile"):
                links_text += f"\n📦 <b>پیکوفایل:</b> {cloud_res['picofile']}"
            if cloud_res.get("bale"):
                links_text += f"\n🇮🇷 <b>شبکه بله:</b> {cloud_res['bale']}"

            if not links_text:
                edit_message(chat_id, status_msg_id, f"❌ خطا در آپلود ابری فایل.")
                return

            edit_message(
                chat_id,
                status_msg_id,
                f"✅ <b>فایل با کیفیت اورجینال در ابر ایرانی منتشر شد:</b>\n\n📁 <b>نام فایل:</b> {filename}\n📦 <b>حجم:</b> {filesize_mb:.1f} MB\n{links_text}\n\n<i>بدون فیلترشکن و با اینترنت نیم‌بها قابل دانلود است.</i>",
                reply_markup=markup
            )
            return

        # Case 2: Filesize <= 50MB (Send in Telegram + publish to clouds)
        edit_message(chat_id, status_msg_id, f"📤 <b>در حال ارسال فایل به تلگرام...</b> ({filesize_mb:.1f} MB)")

        caption = f"✨ <b>{filename}</b>\n\n🤖 @scldlbot_bot"

        # Background upload to Iranian clouds for direct links
        cloud_res = upload_dual_iranian_cloud(filepath, chat_id)
        reply_markup = make_cloud_keyboard(cloud_res)

        with open(filepath, "rb") as f:
            if mode == "audio" or filepath.endswith(".mp3"):
                res = tg_call(
                    "sendAudio",
                    data={
                        "chat_id": chat_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                        "reply_to_message_id": reply_to_id,
                        "reply_markup": json.dumps(reply_markup) if reply_markup else None
                    },
                    files={"audio": (filename, f, "audio/mpeg")},
                    timeout=180
                )
            else:
                res = tg_call(
                    "sendVideo",
                    data={
                        "chat_id": chat_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                        "supports_streaming": True,
                        "reply_to_message_id": reply_to_id,
                        "reply_markup": json.dumps(reply_markup) if reply_markup else None
                    },
                    files={"video": (filename, f, "video/mp4")},
                    timeout=180
                )

        if res.get("ok"):
            tg_call("deleteMessage", {"chat_id": chat_id, "message_id": status_msg_id})
            logger.info(f"Delivered to {chat_id}")
        else:
            logger.error(f"Telegram upload error: {res}")
            edit_message(chat_id, status_msg_id, f"❌ <b>خطا در ارسال فایل:</b> {res.get('description')}")

    except subprocess.TimeoutExpired:
        edit_message(chat_id, status_msg_id, "⏱ <b>زمان پردازش دانلود به پایان رسید (Timeout).</b>")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        edit_message(chat_id, status_msg_id, f"❌ <b>خطای غیرمنتظره:</b> {str(e)[:150]}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def handle_file_forward(msg):
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    msg_id = msg.get("message_id")

    doc = msg.get("document") or msg.get("audio") or msg.get("voice") or (msg.get("photo")[-1] if msg.get("photo") else None)
    if not doc:
        return

    filename = doc.get("file_name") or f"file_{doc.get('file_unique_id')}.dat"
    file_id = doc.get("file_id")

    st = send_message(chat_id, "⏳ <b>در حال دریافت فایل و انتشار همزمان در سرورهای ابری ایرانی...</b>", reply_to_message_id=msg_id)
    status_msg_id = st.get("result", {}).get("message_id")

    def _task():
        tmp_dir = tempfile.mkdtemp(prefix="iran_cloud_")
        try:
            g = tg_call("getFile", {"file_id": file_id})
            if not g.get("ok"):
                edit_message(chat_id, status_msg_id, f"❌ خطا در دریافت اطلاعات فایل: {g.get('description')}")
                return
            file_path = g["result"]["file_path"]
            dl_url = f"{FILE_BASE_URL}/{file_path}"
            local_path = os.path.join(tmp_dir, filename)

            with requests.get(dl_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)

            filesize = os.path.getsize(local_path)
            filesize_mb = filesize / (1024 * 1024)
            edit_message(chat_id, status_msg_id, f"☁️ <b>در حال آپلود در پیکوفایل و بله...</b> ({filesize_mb:.1f} MB)")

            cloud_res = upload_dual_iranian_cloud(local_path, chat_id)
            markup = make_cloud_keyboard(cloud_res)

            links_text = ""
            if cloud_res.get("picofile"):
                links_text += f"\n📦 <b>پیکوفایل:</b> {cloud_res['picofile']}"
            if cloud_res.get("bale"):
                links_text += f"\n🇮🇷 <b>شبکه بله:</b> {cloud_res['bale']}"

            if not links_text:
                edit_message(chat_id, status_msg_id, f"❌ خطا در انتشار فایل روی سرورهای ابری.")
                return

            edit_message(
                chat_id,
                status_msg_id,
                f"✅ <b>فایل با موفقیت در فضای ابری ایرانی منتشر شد!</b>\n\n📁 <b>نام:</b> <code>{filename}</code>\n📦 <b>حجم:</b> {filesize_mb:.1f} MB\n{links_text}\n\n<i>بدون فیلترشکن، با ترافیک نیم‌بها و بدون تبلیغات</i>",
                reply_markup=markup
            )
        except Exception as e:
            logger.exception("Forward upload failed:")
            edit_message(chat_id, status_msg_id, f"❌ <b>خطا:</b> {str(e)[:150]}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    executor.submit(_task)

def handle_message(msg):
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = msg.get("text", "").strip()
    msg_id = msg.get("message_id")

    if not chat_id:
        return

    # Check for file attachments or forwarded documents
    if msg.get("document") or msg.get("audio") or msg.get("voice") or msg.get("photo"):
        handle_file_forward(msg)
        return

    if text == "/start" or text.startswith("/start "):
        welcome = (
            "👋 <b>سلام! به ربات دانلودر همه‌کاره با فضای ابری ملی خوش آمدید.</b>\n\n"
            "این ربات تمامی فایل‌ها و دانلودها را به‌صورت همزمان در دو فضای ابری پرسرعت ایرانی منتشر می‌کند:\n\n"
            "1️⃣ <b>پیکوفایل (PicoFile):</b> فضای ابری مطمئن با ۲۰ گیگابایت حافظه و بدون تبلیغ پاپ‌آپ\n"
            "2️⃣ <b>شبکه ابری بله (Bale Bot CDN):</b> دانلود مستقیم از CDN ملی با ترافیک نیم‌بها\n\n"
            "📌 <b>نحوه کار:</b>\n"
            "• برای دانلود رسانه: لینک SoundCloud، یوتیوب، اینستاگرام یا تیک‌تاک را بفرستید.\n"
            "• برای آپلود فایل: هر فایلی را به این چت فوروارد کنید.\n\n"
            "⚙️ <b>تنظیمات اتصال بله و پیکوفایل:</b>\n"
            "• ثبت ربات بله: <code>/setbale TOKEN CHAT_ID</code>\n"
            "• ثبت کوکی پیکوفایل: <code>/setpico COOKIE</code>"
        )
        send_message(chat_id, welcome, reply_to_message_id=msg_id)
        return

    if text.startswith("/setbale"):
        parts = text.split()
        if len(parts) < 3:
            send_message(
                chat_id,
                "⚠️ لطفاً توکن ربات و شناسه چت بله را به شکل زیر وارد کنید:\n<code>/setbale BALE_TOKEN CHAT_ID</code>\n\nمثال: <code>/setbale 123456:abcd... @my_bale_channel</code>",
                reply_to_message_id=msg_id
            )
            return
        b_token, b_chat = parts[1], parts[2]
        db.set_bale_config(chat_id, b_token, b_chat)
        send_message(chat_id, "✅ <b>تنظیمات شبکه بله با موفقیت ذخیره شد!</b> از این پس فایل‌ها در کانال/چت بله شما نیز منتشر می‌شوند.", reply_to_message_id=msg_id)
        return

    if text.startswith("/setpico"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "⚠️ لطفاً کوکی سشن لاگین پیکوفایل را بعد از دستور وارد کنید:\n<code>/setpico COOKIE</code>", reply_to_message_id=msg_id)
            return
        p_cookie = parts[1].strip()
        db.set_pico_cookie(chat_id, p_cookie)
        send_message(chat_id, "✅ <b>کوکی حساب پیکوفایل شما ذخیره شد!</b>", reply_to_message_id=msg_id)
        return

    if text == "/ping":
        send_message(chat_id, "🏓 <b>Pong!</b> سیستم دانلودر و انتشارات ابری ایرانی فعال است.", reply_to_message_id=msg_id)
        return

    if text == "/status":
        p = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        ytdlp_v = p.stdout.strip() if p.returncode == 0 else "unknown"
        b_token, b_chat = db.get_bale_config(chat_id)
        p_cookie = db.get_pico_cookie(chat_id)
        status_text = (
            f"⚙️ <b>وضعیت ربات و فضاهای ابری:</b>\n\n"
            f"• <b>موتور دانلود:</b> yt-dlp v{ytdlp_v} (کیفیت اصلی)\n"
            f"• <b>پیکوفایل (PicoFile):</b> {'متصل به اکانت شخصی' if p_cookie else 'فعال (آپلود عمومی TUS)'}\n"
            f"• <b>شبکه بله (Bale CDN):</b> {'متصل و فعال' if b_token else 'در انتظار توکن (/setbale)'}\n"
            f"• <b>سرور:</b> Alwaysdata Linux (24/7 Daemon)\n"
        )
        send_message(chat_id, status_text, reply_to_message_id=msg_id)
        return

    url = extract_url(text)
    if not url:
        if chat.get("type") == "private":
            send_message(
                chat_id,
                "⚠️ لینکی یافت نشد! لینک مدیا یا فایل مورد نظرتان را بفرستید.",
                reply_to_message_id=msg_id
            )
        return

    st = send_message(chat_id, "🔍 <b>در حال بررسی لینک...</b>", reply_to_message_id=msg_id)
    status_msg_id = st.get("result", {}).get("message_id") if st.get("ok") else None

    lower_url = url.lower()
    if any(k in lower_url for k in ["instagram.com", "tiktok.com", "twitter.com", "x.com", "pin.it", "pinterest.com"]):
        mode = "video"
    else:
        mode = "audio"

    executor.submit(download_and_send, chat_id, url, status_msg_id, mode, msg_id)

def main():
    logger.info("Starting scldl-bot (Dual Iranian Cloud: PicoFile + Bale)...")
    db.init_db()

    me = tg_call("getMe")
    if not me.get("ok"):
        logger.error(f"Invalid Telegram token: {me}")
        sys.exit(1)
    logger.info(f"Bot connected: @{me['result']['username']} ({me['result']['id']})")

    tg_call("deleteWebhook", {"drop_pending_updates": False})

    offset = 0
    while True:
        try:
            updates = tg_call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]}, timeout=40)
            if not updates.get("ok"):
                time.sleep(2)
                continue

            for u in updates.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    handle_message(u["message"])
                elif "callback_query" in u:
                    answer_callback(u["callback_query"]["id"])

        except Exception as e:
            logger.error(f"Polling loop error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
