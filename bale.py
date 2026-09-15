#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bale Bot Direct Cloud & CDN Module
Sends files directly to the user in the Bale messenger bot (@cloudmelodbot)
and generates high-speed direct CDN download links.
"""
import os
import logging
import requests

logger = logging.getLogger("bale")

BALE_API_BASE = "https://tapi.bale.ai"
DEFAULT_BALE_CHAT = 1446119540

def upload_to_bale(filepath, bale_token, target_chat_id=None):
    if not bale_token:
        return None, "توکن بله تنظیم نشده است."

    if not os.path.isfile(filepath):
        return None, "فایل یافت نشد."

    chat_id = target_chat_id or DEFAULT_BALE_CHAT
    filename = os.path.basename(filepath)
    bot_url = f"{BALE_API_BASE}/bot{bale_token}"

    is_audio = filename.lower().endswith((".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac"))
    is_video = filename.lower().endswith((".mp4", ".mkv", ".mov", ".webm", ".avi"))

    filesize = os.path.getsize(filepath)
    # 110MB+ lossless: split with ffmpeg -c copy into <47MB parts (Bale nginx 50MB hard limit)
    if filesize > 47 * 1024 * 1024:
        import subprocess, json as _json, tempfile, shutil as _sh
        try:
            pr = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","json",filepath], capture_output=True, text=True, timeout=15)
            dur = float(_json.loads(pr.stdout).get("format",{}).get("duration", 3600))
        except Exception:
            dur = 3600
        n = (filesize + 47*1024*1024 -1)//(47*1024*1024)
        seg = dur / n if n else dur
        parts=[]
        for i in range(int(n)):
            part = filepath + f".part{i+1}.mp3"
            subprocess.run(["ffmpeg","-y","-ss",str(i*seg),"-t",str(seg),"-i",filepath,"-c","copy",part], capture_output=True, timeout=120)
            if __import__("os").path.isfile(part): parts.append(part)
        if not parts:
            return None, "split failed"
        urls=[]
        for idx, part in enumerate(parts,1):
            cap = f"🎵 {filename} — قسمت {idx}/{len(parts)} (کیفیت اصلی، بدون افت)\n🤖 @cloudmelodbot"
            try:
                with open(part,"rb") as f:
                    rr = requests.post(f"{bot_url}/sendAudio", data={"chat_id": str(chat_id), "caption": cap}, files={"audio": (__import__("os").path.basename(part), f, "audio/mpeg")}, timeout=300)
                dd = rr.json()
                if dd.get("ok"):
                    msg=dd.get("result",{}); doc=msg.get("audio") or msg.get("document"); fid=doc.get("file_id") if doc else None
                    if fid:
                        gg=requests.get(f"{bot_url}/getFile", params={"file_id": fid}, timeout=30).json()
                        if gg.get("ok") and gg.get("result",{}).get("file_path"):
                            urls.append(f"{BALE_API_BASE}/file/bot{bale_token}/{gg['result']['file_path']}")
                        else: urls.append("https://ble.ir/cloudmelodbot")
                    else: urls.append("https://ble.ir/cloudmelodbot")
            except Exception as e:
                logger.error(f"Bale part {idx} failed: {e}")
        for pp in parts:
            try: __import__("os").remove(pp)
            except: pass
        return urls[0] if urls else "https://ble.ir/cloudmelodbot", None

    caption = f"🎵 {filename}\n🤖 ارسال شده از دانلودر ابری"

    try:
        with open(filepath, "rb") as f:
            if is_audio:
                res = requests.post(
                    f"{bot_url}/sendAudio",
                    data={"chat_id": str(chat_id), "caption": caption},
                    files={"audio": (filename, f, "audio/mpeg")},
                    timeout=300
                )
            elif is_video:
                res = requests.post(
                    f"{bot_url}/sendVideo",
                    data={"chat_id": str(chat_id), "caption": caption},
                    files={"video": (filename, f, "video/mp4")},
                    timeout=300
                )
            else:
                res = requests.post(
                    f"{bot_url}/sendDocument",
                    data={"chat_id": str(chat_id), "caption": caption},
                    files={"document": (filename, f)},
                    timeout=300
                )

        data = res.json()
        if not data.get("ok"):
            logger.error(f"Bale API error: {data}")
            return None, f"خطا در ارسال به بله: {data.get('description')}"

        msg = data.get("result", {})
        doc = msg.get("audio") or msg.get("video") or msg.get("document")
        file_id = doc.get("file_id") if doc else None

        # Resolve direct CDN URL
        if file_id:
            g = requests.get(f"{bot_url}/getFile", params={"file_id": file_id}, timeout=30).json()
            if g.get("ok") and g.get("result", {}).get("file_path"):
                file_path = g["result"]["file_path"]
                cdn_url = f"{BALE_API_BASE}/file/bot{bale_token}/{file_path}"
                return cdn_url, None

        return "https://ble.ir/cloudmelodbot", None
    except Exception as e:
        logger.error(f"Bale upload failed: {e}")
        return None, f"خطا در ارتباط با بله: {str(e)[:150]}"
