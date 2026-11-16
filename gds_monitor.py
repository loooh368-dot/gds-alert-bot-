import os
import re
import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ================ CONFIG ================

BASE_URL = "https://www.indiapost.gov.in/gdsonlineengagement"

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "seen_results.json"
STATUS_FILE = DATA_DIR / "last_status.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CIRCLES = {
    "West Bengal": ["west bengal"],
    "Assam": ["assam"],
    "North East": ["north east", "northeast"],
}

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()

session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; GDSMonitor/3.0)"
})


# ================ TELEGRAM ================

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
        raise RuntimeError(str(result))

    logging.info("Telegram message sent")


# ================ JSON ================

def load_json(path, default):

    if not path.exists():
        return default

    try:
        return json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception:
        return default


def save_json(path, data):

    DATA_DIR.mkdir(exist_ok=True)

    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ================ OFFICIAL DOMAIN ================

def official_url(url):

    host = (urlparse(url).hostname or "").lower()

    allowed = (
        host == "indiapost.gov.in"
        or host.endswith(".indiapost.gov.in")
        or host == "indiapostgdsonline.gov.in"
        or host.endswith(".indiapostgdsonline.gov.in")
        or host == "indiapostgdsonline.cept.gov.in"
    )

    return allowed


# ================ CIRCLE DETECTION ================

def detect_circle(text):

    text = text.lower()

    found = []

    for circle, aliases in CIRCLES.items():

        if any(alias in text for alias in aliases):
            found.append(circle)

    if len(found) == 1:
        return found[0]

    return None


# ================ LIST DETECTION ================

def detect_list(text):

    text = re.sub(r"\s+", " ", text.lower())

    if re.search(r"suppl?imentary|supplementary", text):

        match = re.search(
            r"list\s*[-– ]*\s*(ii|iii|iv|v|vi|vii|viii|ix|x|\d+)",
            text
        )

        if match:
            return "Supplementary List-" + match.group(1).upper()

        return None

    if (
        "shortlisted candidates" in text
        or "list-i" in text
        or "list 1" in text
        or "list-i" in text.replace(" ", "")
    ):
        return "List-I"

    return None


# ================ FIND OFFICIAL PDF LINKS ================

def find_pdf_links():

    logging.info("Opening official India Post website")

    response = session.get(
        BASE_URL,
        timeout=45
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    page_text = soup.get_text(" ", strip=True).lower()

    # Fail closed if the expected circles are missing.
    for circle in CIRCLES:

        if circle.lower() not in page_text:

            raise RuntimeError(
                f"Official page verification failed: {circle}"
            )

    results = {}

    for anchor in soup.find_all("a", href=True):

        href = anchor.get("href", "").strip()

        full_url = urljoin(BASE_URL, href)

        if not official_url(full_url):
            continue

        if ".pdf" not in full_url.lower():
            continue

        # Collect nearby HTML text to identify circle and list.
        node = anchor
        context = ""

        for _ in range(5):

            if node is None:
                break

            context = node.get_text(
                " ",
                strip=True
            )

            circle = detect_circle(context)

            if circle:
                break

            node = node.parent

        if not circle:
            continue

        list_name = detect_list(
            anchor.get_text(" ", strip=True)
            + " "
            + context
        )

        if not list_name:
            continue

        key = f"{circle}_{list_name}"

        results[key] = {
            "circle": circle,
            "list": list_name,
            "url": full_url
        }

    if not results:

        raise RuntimeError(
            "No verified Circle-wise Merit List PDF links found. "
            "No no-update message will be sent."
        )

    logging.info(
        "Found %s official PDF links",
        len(results)
    )

    return results


# ================ PDF VERIFICATION ================

def download_and_hash(results):

    verified = {}

    for key, item in results.items():

        logging.info(
            "Checking %s %s",
            item["circle"],
            item["list"]
        )

        response = session.get(
            item["url"],
            timeout=60
        )

        response.raise_for_status()

        content = response.content

        # Accept only real PDF files.
        if not content.startswith(b"%PDF"):
            raise RuntimeError(
                f"Invalid PDF: {item['url']}"
            )

        fingerprint = hashlib.sha256(
            content
        ).hexdigest()

        verified[key] = {
            **item,
            "hash": fingerprint
        }

    return verified


# ================ ALERT MESSAGE ================

def build_alert(item):

    return (
        "🚨 INDIA POST GDS MERIT LIST UPDATE\n\n"
        f"📍 Circle: {item['circle']}\n"
        f"📌 Merit List: {item['list']}\n\n"
        "✅ Official PDF detected.\n\n"
        "🔗 Download Official Merit List:\n"
        f"{item['url']}\n\n"
        "Source: India Post Official Website"
    )


# ================ MAIN ================

def main():

    logging.info("GDS Monitor Started")

    now = datetime.now(IST)

    today = now.strftime("%Y-%m-%d")

    old_state = load_json(
        STATE_FILE,
        {}
    )

    last_status = load_json(
        STATUS_FILE,
        {}
    )

    # Step 1: Find official links.
    links = find_pdf_links()

    # Step 2: Verify and download actual PDFs.
    current = download_and_hash(links)

    # Step 3: Load previous verified data.
    if (
        old_state.get("version") != 3
        or not isinstance(old_state.get("items"), dict)
    ):

        save_json(
            STATE_FILE,
            {
                "version": 3,
                "items": current
            }
        )

        save_json(
            STATUS_FILE,
            {"date": today}
        )

        send_telegram(
            "✅ INDIA POST GDS MONITOR ACTIVATED\n\n"
            "Official website successfully checked.\n"
            f"Existing verified PDF lists: {len(current)}\n\n"
            "West Bengal, Assam and North East monitoring "
            "is now active.\n\n"
            "I will automatically check for new or changed "
            "official Merit List PDFs."
        )

        logging.info("Initial baseline saved")

        return

    old_items = old_state["items"]

    new_items = []

    for key, item in current.items():

        previous = old_items.get(key)

        if previous is None:

            new_items.append(item)

        elif previous.get("hash") != item["hash"]:

            new_items.append(item)

    # Step 4: Send genuine new/changed PDF alerts.
    if new_items:

        for item in new_items:

            send_telegram(
                build_alert(item)
            )

        # Preserve previously detected lists.
        old_items.update(current)

        save_json(
            STATE_FILE,
            {
                "version": 3,
                "items": old_items
            }
        )

        save_json(
            STATUS_FILE,
            {"date": today}
        )

        logging.info(
            "%s new or changed PDF(s)",
            len(new_items)
        )

        return

    # Step 5: Daily no-update message.
    if last_status.get("date") != today:

        send_telegram(
            "ℹ️ INDIA POST GDS DAILY UPDATE\n\n"
            "Official website successfully checked.\n\n"
            "West Bengal: No new/changed PDF detected.\n"
            "Assam: No new/changed PDF detected.\n"
            "North East: No new/changed PDF detected.\n\n"
            "📅 Date: "
            + now.strftime("%d-%m-%Y")
            + "\n🕒 Time: "
            + now.strftime("%I:%M %p")
            + "\n\nNo new official Merit List PDF "
            "was detected in this check.\n\n"
            + BASE_URL
        )

        save_json(
            STATUS_FILE,
            {"date": today}
        )

        logging.info("Daily status sent")

    else:

        logging.info(
            "No new PDF. Daily status already sent."
        )

    old_items.update(current)

    save_json(
        STATE_FILE,
        {
            "version": 3,
            "items": old_items
        }
    )


if __name__ == "__main__":

    try:
        main()

    except Exception:

        logging.exception(
            "Monitoring failed. No no-update message sent."
        )

        raise
