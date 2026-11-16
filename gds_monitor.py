import os
import json
import time
import hashlib
import logging
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# SETTINGS

BASE_URL = "https://www.indiapost.gov.in/gdsonlineengagement"

CIRCLES = {
    "West Bengal": "WestBengal",
    "Assam": "Assam",
    "North East": "NorthEast",
}

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DATA_DIR = Path("data")
SEEN_FILE = DATA_DIR / "seen_urls.json"

MAX_LIST = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 GDSMonitor/2.0"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update(HEADERS)


def load_seen():
    DATA_DIR.mkdir(exist_ok=True)

    if not SEEN_FILE.exists():
        return {}

    try:
        data = json.loads(SEEN_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_seen(data):
    DATA_DIR.mkdir(exist_ok=True)

    temp = SEEN_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False)
    )
    temp.replace(SEEN_FILE)


def get_result(circle, number):

    url = (
        f"{BASE_URL}/api/results/"
        f"list{number}?circle={circle}"
    )

    try:
        response = session.get(url, timeout=30)

        if response.status_code != 200:
            return None

        soup = BeautifulSoup(
            response.text, "html.parser"
        )

        table = soup.find("table")

        if not table:
            return None

        rows = table.find_all("tr")

        if len(rows) < 2:
            return None

        headers = " ".join(
            th.get_text(" ", strip=True).lower()
            for th in table.find_all("th")
        )

        if "reg number" not in headers:
            return None

        content = " ".join(
            table.get_text(" ", strip=True).split()
        )

        if len(content) < 50:
            return None

        fingerprint = hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()

        return {
            "url": url,
            "hash": fingerprint,
            "list": number
        }

    except requests.RequestException as error:
        log.error("%s List-%s: %s", circle, number, error)
        return None


def send_telegram(message):

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": False
        },
        timeout=30
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(str(result))


def main():

    log.info("India Post GDS Monitor started")

    seen = load_seen()

    # First run: save existing lists as baseline.
    if not seen:

        log.info("Creating initial baseline")

        for circle_name, circle_code in CIRCLES.items():

            for number in range(1, MAX_LIST + 1):

                item = get_result(circle_code, number)

                if item:
                    key = f"{circle_code}_List-{number}"
                    seen[key] = item["hash"]

                    log.info(
                        "Baseline: %s List-%s",
                        circle_name, number
                    )

        save_seen(seen)

        log.info("Initial baseline saved. No alerts sent.")
        return

    changes = {}

    # Check all three circles.

    for circle_name, circle_code in CIRCLES.items():

        for number in range(1, MAX_LIST + 1):

            item = get_result(circle_code, number)

            if not item:
                continue

            key = f"{circle_code}_List-{number}"

            old_hash = seen.get(key)

            if old_hash != item["hash"]:

                changes[key] = {
                    "circle": circle_name,
                    "list": number,
                    "url": item["url"],
                    "hash": item["hash"]
                }

    if not changes:
        log.info("No new or changed GDS list detected.")
        return

    # Send notifications.

    for key, item in changes.items():

        message = (
            "🚨 INDIA POST GDS ALERT\n\n"
            "🆕 New/Updated Merit List Detected!\n\n"
            f"📍 Circle: {item['circle']}\n"
            f"📌 List: List-{item['list']}\n\n"
            f"🔗 Official Result Link:\n{item['url']}"
        )

        try:
            send_telegram(message)

            seen[key] = item["hash"]
            save_seen(seen)

            log.info("Telegram notification sent: %s", key)

        except Exception as error:
            log.error("Notification failed: %s", error)

    log.info("Monitoring cycle completed.")


if __name__ == "__main__":
    main()
