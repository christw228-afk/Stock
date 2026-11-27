from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import requests


def send_telegram_message(
    text: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    timeout: int = 25,
    retries: int = 2,
) -> tuple[bool, str]:
    """
    Send a message via Telegram Bot API.
    Falls back to TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
    """
    token = (bot_token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    cid = (chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()

    if not token or not cid:
        return False, "未設定 Telegram bot_token 或 chat_id，略過推播"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req_timeout = (10, max(timeout, 45))
    last_err: str = ""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=req_timeout,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                return True, "Telegram 推播成功"
            last_err = f"Telegram 推播失敗 ({resp.status_code}): {resp.text[:200]}"
            if resp.status_code < 500 and resp.status_code != 429:
                break
        except requests.exceptions.ReadTimeout as exc:
            return False, f"Telegram 回應逾時（訊息可能已送出，不重試以避免重複）: {exc}"
        except Exception as exc:
            last_err = f"Telegram 連線錯誤: {exc}"
        if attempt < retries:
            time.sleep(2)
    return False, last_err


def send_telegram_document(
    file_path: Path,
    caption: str = "",
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    timeout: int = 30,
    retries: int = 2,
) -> tuple[bool, str]:
    """Upload an HTML/report file directly to Telegram so the user can open it with one tap."""
    token = (bot_token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    cid = (chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()

    if not token or not cid:
        return False, "未設定 Telegram bot_token 或 chat_id，略過檔案傳送"
    if not file_path.exists():
        return False, f"找不到檔案: {file_path}"

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req_timeout = (10, max(timeout, 60))
    last_err: str = ""
    for attempt in range(1, retries + 1):
        try:
            with file_path.open("rb") as f:
                resp = requests.post(
                    url,
                    data={"chat_id": cid, "caption": caption[:1000]},
                    files={"document": (file_path.name, f, "text/html")},
                    timeout=req_timeout,
                )
            if resp.status_code == 200 and resp.json().get("ok"):
                return True, f"Telegram 已傳送附件 ({file_path.name})"
            last_err = f"Telegram 附件傳送失敗 ({resp.status_code}): {resp.text[:200]}"
            if resp.status_code < 500 and resp.status_code != 429:
                break
        except requests.exceptions.ReadTimeout as exc:
            return False, f"Telegram 附件回應逾時（檔案可能已送出，不重試以避免重複）: {exc}"
        except Exception as exc:
            last_err = f"Telegram 附件連線錯誤: {exc}"
        if attempt < retries:
            time.sleep(2)
    return False, last_err
