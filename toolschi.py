#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Toolschi Uploader Module — جایگزین PicoFile برای فایل‌های <= 100MB
- آپلود به toolschi.com (ایرانی، لینک مستقیم، بدون نیاز VPN)
- لینک مستقیم: https://dl.toolschi.com/up/<filename>
- محدودیت: حداکثر 100MB (فایل‌های بزرگتر → PicoFile fallback)
- از آپلود سوم به بعد شماره موبایل (۱۱ رقمی) الزامی است
"""
import os
import re
import logging
import requests

logger = logging.getLogger("toolschi")

UPLOAD_PAGE = "https://toolschi.com/tools/upload-center"
UPLOAD_ENDPOINT = "https://toolschi.com/tools/upload-center"
DIRECT_BASE = "https://dl.toolschi.com/up/"
MAX_SIZE = 104857600  # 100 MB

# شماره موبایل برای آپلودهای سوم به بعد (۱۱ رقمی)
MOBILE = os.environ.get("TOOLSCHI_MOBILE", "09120000000")


def _fetch_token():
    """گرفتن توکن تازه از HTML صفحه آپلود."""
    try:
        html = requests.get(UPLOAD_PAGE, timeout=25,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}).text
        m = re.search(r'token\s*[:=]\s*["\']([a-f0-9]{64})', html)
        if not m:
            m = re.search(r'"token"\s*:\s*"([a-f0-9]{64})"', html)
        return m.group(1) if m else None
    except Exception as e:
        logger.warning("token fetch failed: %s", e)
        return None


def upload_to_toolschi(filepath, token=None):
    """Upload a file to toolschi. Returns (direct_url, error)."""
    if not os.path.isfile(filepath):
        return None, "فایل یافت نشد."

    size = os.path.getsize(filepath)
    if size > MAX_SIZE:
        return None, f"حجم بیش از حد مجاز toolschi (حداکثر 100MB) — از PicoFile استفاده کنید ({size/1048576:.1f}MB)."

    filename = os.path.basename(filepath)
    token = token or _fetch_token()
    if not token:
        return None, "دریافت توکن آپلود toolschi ناموفق بود."

    ctype = "audio/mpeg" if filename.lower().endswith((".mp3", ".m4a", ".aac")) else \
            "video/mp4" if filename.lower().endswith((".mp4", ".mov", ".mkv")) else \
            "application/octet-stream"

    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                UPLOAD_ENDPOINT,
                headers={"X-Upload-Token": token,
                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                         "Referer": UPLOAD_PAGE,
                         "Origin": "https://toolschi.com"},
                files={"file": (filename, f, ctype)},
                data={"upload_context": "upload_center"},
                timeout=900,
            )
    except Exception as e:
        return None, f"خطای آپلود toolschi: {e}"

    try:
        d = resp.json()
    except Exception:
        return None, f"پاسخ نامعتبر toolschi: {resp.status_code} {resp.text[:200]}"

    if not d.get("ok"):
        msg = d.get("message", "ناموفق")
        if "موبایل" in msg or "MOBILE_REQUIRED" in str(d.get("code", "")):
            # retry with mobile number
            try:
                with open(filepath, "rb") as f:
                    resp2 = requests.post(
                        UPLOAD_ENDPOINT,
                        headers={"X-Upload-Token": token,
                                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                                 "Referer": UPLOAD_PAGE,
                                 "Origin": "https://toolschi.com"},
                        files={"file": (filename, f, ctype)},
                        data={"upload_context": "upload_center", "mobile": MOBILE},
                        timeout=900,
                    )
                d2 = resp2.json()
                if d2.get("ok"):
                    fname = d2.get("filename")
                    return DIRECT_BASE + fname, None
                return None, d2.get("message", "ناموفق")
            except Exception as e:
                return None, f"خطای آپلود با شماره: {e}"
        return None, msg

    fname = d.get("filename")
    if not fname:
        return None, "پاسخ toolschi بدون filename"
    return DIRECT_BASE + fname, None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python3 toolschi.py <filepath>")
        sys.exit(1)
    url, err = upload_to_toolschi(sys.argv[1])
    print(url or f"ERROR: {err}")