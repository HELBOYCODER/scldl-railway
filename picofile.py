#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PicoFile TUS Chunked Uploader Module
Streams any file in 1MB chunks conforming to PicoFile's TUS 1.0.0 Nginx proxy rules.
Works for files up to 2 GB with zero payload size limits.
"""
import os
import base64
import mimetypes
import logging
import requests

logger = logging.getLogger("picofile")

PICO_UPLOAD_ENDPOINT = "https://www.picofile.com/uploads"
CHUNK_SIZE = 1048576  # 1 MB chunks (matches PicoFile chunkSizeBytes)

def upload_to_picofile(filepath, cookie=None):
    if not os.path.isfile(filepath):
        return None, "فایل یافت نشد."

    filesize = os.path.getsize(filepath)
    filename = os.path.basename(filepath)
    ctype, _ = mimetypes.guess_type(filepath)
    ctype = ctype or "application/octet-stream"

    b64_filename = base64.b64encode(filename.encode("utf-8")).decode("utf-8")
    b64_filetype = base64.b64encode(ctype.encode("utf-8")).decode("utf-8")

    headers = {
        "Tus-Resumable": "1.0.0",
        "Upload-Length": str(filesize),
        "Upload-Metadata": f"filename {b64_filename},filetype {b64_filetype}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.picofile.com/",
        "Origin": "https://www.picofile.com"
    }
    if cookie:
        headers["Cookie"] = cookie

    # Step 1: Initiate upload
    try:
        r = requests.post(PICO_UPLOAD_ENDPOINT, headers=headers, timeout=30)
        if r.status_code != 201:
            return None, f"خطا در ایجاد آپلود در پیکوفایل (کد {r.status_code}): {r.text[:200]}"
        location = r.headers.get("Location")
        if not location:
            return None, "آدرس آپلود از پیکوفایل دریافت نشد."
        if location.startswith("/"):
            location = "https://www.picofile.com" + location
    except Exception as e:
        logger.error(f"PicoFile init error: {e}")
        return None, f"خطای اتصال به پیکوفایل: {str(e)[:150]}"

    # Step 2: Stream in chunks via PATCH
    try:
        offset = 0
        final_slug = None

        with open(filepath, "rb") as f:
            while offset < filesize:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break

                patch_headers = {
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": str(offset),
                    "Content-Type": "application/offset+octet-stream",
                    "Content-Length": str(len(chunk)),
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.picofile.com/",
                    "Origin": "https://www.picofile.com"
                }
                if cookie:
                    patch_headers["Cookie"] = cookie

                p = requests.patch(location, headers=patch_headers, data=chunk, timeout=60)
                if p.status_code not in [200, 204]:
                    return None, f"خطا در ارسال قطعه داده به پیکوفایل (کد {p.status_code})"

                new_offset = p.headers.get("Upload-Offset")
                offset = int(new_offset) if new_offset else (offset + len(chunk))

                slug = p.headers.get("Storage-Slug")
                if slug:
                    final_slug = slug

        if final_slug:
            download_url = f"https://www.picofile.com/f/{final_slug}/{filename}"
            return download_url, None

        return location, None
    except Exception as e:
        logger.error(f"PicoFile patch error: {e}")
        return None, f"خطای انتقال داده به پیکوفایل: {str(e)[:150]}"
