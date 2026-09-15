import os
import mimetypes
import requests

ZDRIVE_BASE_URL = "https://zincdrive.com"

def upload_to_zincdrive(filepath, api_key):
    if not api_key:
        return None, "کلید ZincDrive تنظیم نشده است."

    if not os.path.isfile(filepath):
        return None, "فایل یافت نشد."

    filesize = os.path.getsize(filepath)
    filename = os.path.basename(filepath)
    ctype, _ = mimetypes.guess_type(filepath)
    ctype = ctype or "application/octet-stream"

    # Reject video
    if ctype.startswith("video/"):
        return None, "آپلود ویدیو در ZincDrive مجاز نیست."

    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Step 1: Sign
    try:
        sign_res = requests.post(
            f"{ZDRIVE_BASE_URL}/api/v1/s3/sign",
            headers=headers,
            json={"filename": filename, "type": ctype},
            timeout=20
        )
        sign_data = sign_res.json()
        presigned_url = sign_data.get("url")
        s3_key = sign_data.get("key")
        if not presigned_url or not s3_key:
            return None, f"خطا در دریافت لینک امضاشده: {sign_res.text[:200]}"
    except Exception as e:
        return None, f"خطا در ارتباط با سرور ZincDrive: {str(e)[:150]}"

    # Step 2: S3 PUT
    try:
        with open(filepath, "rb") as f:
            put_res = requests.put(
                presigned_url,
                headers={"Content-Type": ctype},
                data=f,
                timeout=300
            )
        if put_res.status_code not in [200, 204]:
            return None, f"خطا در ارسال فایل به فضای ابری (کد {put_res.status_code})"
    except Exception as e:
        return None, f"خطا در آپلود ابری: {str(e)[:150]}"

    # Step 3: Confirm
    try:
        confirm_res = requests.post(
            f"{ZDRIVE_BASE_URL}/api/v1/s3/confirm",
            headers=headers,
            json={
                "key": s3_key,
                "filename": filename,
                "size": filesize,
                "type": ctype
            },
            timeout=20
        )
        conf_data = confirm_res.json()
        file_url = conf_data.get("download_link") or conf_data.get("url") or conf_data.get("file_url") or conf_data.get("link")
        if not file_url and "shared_id" in conf_data:
            file_url = f"https://zdrive.to/{conf_data['shared_id']}"
        if not file_url and isinstance(conf_data.get("data"), dict):
            file_url = conf_data["data"].get("download_link") or conf_data["data"].get("url")
        return file_url, None
    except Exception as e:
        return None, f"خطا در تایید آپلود: {str(e)[:150]}"
