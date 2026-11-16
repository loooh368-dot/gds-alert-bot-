import os
import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ================= CONFIG =================

BASE_URL = "https://www.indiapost.gov.in/gdsonlineengagement"

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "seen_results.json"
STATUS_FILE = DATA_DIR / "last_status.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CIRCLES = {
    "West Bengal": "WestBengal",
    "Assam": "Assam",
    "North Eastern": "NorthEast",
}

MAX_LISTS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 IndiaPostGDSMonitor/2.0"
})

IST = ZoneInfo("Asia/Kolkata")


# ================= TELEGRAM =================

def send_telegram(message):

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = session.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=30
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram message failed")

    logging.info("Telegram message sent successfully")


# ================= STATE =================

def load_json(path, default):

    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))

    except Exception:
        return default


def save_json(path, data):

    DATA_DIR.mkdir(exist_ok=True)

    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ================= OFFICIAL RESULT CHECK =================

def check_results():

    results = {}

    for circle_name, circle_code in CIRCLES.items():

        for number in range(1, MAX_LISTS + 1):

            url = (
                f"{BASE_URL}/api/results/"
                f"list{number}?circle={circle_code}"
            )

            logging.info(
                "Checking %s List-%s",
                circle_name,
                number
            )

            response = session.get(
                url,
                timeout=40
            )

            if response.status_code == 404:
                continue

            response.raise_for_status()

            content = response.content

            # Only accept actual PDF files.
            if not content.startswith(b"%PDF"):
                continue

            fingerprint = hashlib.sha256(
                content
            ).hexdigest()

            key = f"{circle_code}_List-{number}"

            results[key] = {
                "circle": circle_name,
                "list": f"List-{number}",
                "url": url,
                "hash": fingerprint
            }

    return results


# ================= ALERT =================

def build_alert(item):

    return (
        "🚨 INDIA POST GDS NEW MERIT LIST\n\n"
        f"📍 Circle: {item['circle']}\n"
        f"📌 List: {item['list']}\n"
        "📅 Official publication: Newly detected\n\n"
        "🔗 Official PDF:\n"
        f"{item['url']}\n\n"
        "Source: India Post Official Website"
    )


# ================= MAIN =================

def main():

    logging.info("India Post GDS Monitor started")

    current_time = datetime.now(IST)

    today = current_time.strftime("%Y-%m-%d")

    old_results = load_json(STATE_FILE, None)

    last_status = load_json(
        STATUS_FILE,
        {}
    )

    current_results = check_results()

    # First run: create baseline.
    if old_results is None:

        save_json(
            STATE_FILE,
            current_results
        )

        logging.info("Initial baseline created")

        return

    # Detect new or changed PDFs.
    new_items = []

    for key, item in current_results.items():

        old_item = old_results.get(key)

        if old_item is None:

            new_items.append(item)

        elif old_item.get("hash") != item["hash"]:

            new_items.append(item)

    # New list found.
    if new_items:

        for item in new_items:

            send_telegram(
                build_alert(item)
            )

        save_json(
            STATE_FILE,
            current_results
        )

        save_json(
            STATUS_FILE,
            {"date": today}
        )

        logging.info(
            "%s new or updated list(s) detected",
            len(new_items)
        )

        return

    # No new list: send only once per day.
    if last_status.get("date") != today:

        message = (
            "ℹ️ INDIA POST GDS DAILY UPDATE\n\n"
            "Aaj porjonto notun kono Merit List "
            "detect hoyni.\n\n"
            "West Bengal: No New List\n"
            "Assam: No New List\n"
            "North Eastern: No New List\n\n"
            f"Last Checked: "
            f"{current_time.strftime('%d-%m-%Y %I:%M %p')}\n\n"
            "Official Website:\n"
            f"{BASE_URL}"
        )

        send_telegram(message)

        save_json(
            STATUS_FILE,
            {"date": today}
        )

        logging.info("Daily no-update message sent")

    else:

        logging.info(
            "No new list. Daily message already sent."
        )

    save_json(
        STATE_FILE,
        current_results
    )


if __name__ == "__main__":

    try:
        main()

    except Exception:

        logging.exception("Monitoring failed")

        raise
