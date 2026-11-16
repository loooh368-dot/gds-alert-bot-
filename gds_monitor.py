import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ============================================================
# Configuration
# ============================================================

OFFICIAL_URL = "https://www.indiapost.gov.in/gdsonlineengagement"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SEEN_FILE = DATA_DIR / "seen_urls.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; IndiaPostGDSMonitor/1.0; "
        "+https://github.com/loooh368-dot/gds-alert-bot)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# HTTP
# ============================================================

def fetch_page(url: str) -> str:
    """Fetch a webpage with retry handling."""

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("Fetching: %s (attempt %s/%s)",
                        url, attempt, MAX_RETRIES)

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            logger.info(
                "Website fetched successfully: HTTP %s",
                response.status_code,
            )

            return response.text

        except requests.RequestException as exc:
            last_error = exc

            logger.warning(
                "Request failed: %s",
                exc,
            )

            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        f"Unable to fetch official India Post website: {last_error}"
    )


# ============================================================
# Seen URLs
# ============================================================

def load_seen_urls() -> set:
    """Load previously processed URLs."""

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not SEEN_FILE.exists():
        return set()

    try:
        with SEEN_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            raise ValueError("seen_urls.json must contain a JSON list")

        return set(str(item) for item in data)

    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.error("Could not read seen URLs: %s", exc)
        return set()


def save_seen_urls(urls: set) -> None:
    """Save processed URLs."""

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with SEEN_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            sorted(urls),
            file,
            indent=2,
            ensure_ascii=False,
        )


# ============================================================
# Text helpers
# ============================================================

def clean_text(text: str) -> str:
    """Normalize whitespace."""

    return re.sub(r"\s+", " ", text).strip()


def is_gds_list_text(text: str) -> bool:
    """
    Decide whether text looks like a GDS shortlist/merit/selection list.

    We intentionally require GDS-related wording plus list-related wording
    to reduce false positives.
    """

    text_lower = text.lower()

    gds_keywords = (
        "gds",
        "gramin dak sevak",
        "shortlisted candidates",
        "merit list",
        "selection list",
    )

    list_keywords = (
        "list",
        "shortlisted",
        "merit",
        "selection",
        "candidates",
    )

    has_gds = any(keyword in text_lower for keyword in gds_keywords)
    has_list = any(keyword in text_lower for keyword in list_keywords)

    return has_gds and has_list


def extract_list_number(text: str) -> str:
    """Try to extract a list number such as List-I, List-II, etc."""

    patterns = [
        r"\bList[-\s]*([IVX]+)\b",
        r"\bList[-\s]*(\d+)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            return f"List-{match.group(1)}"

    return "Not specified"


def extract_date(text: str) -> str:
    """Try to find a date in common Indian date formats."""

    patterns = [
        r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b",
        r"\b\d{1,2}[-/][A-Za-z]{3,9}[-/]\d{4}\b",
        r"\b[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            return match.group(0)

    return "Not specified"


# ============================================================
# India Post list detection
# ============================================================

def detect_lists(html: str) -> list:
    """
    Find links and page sections related to GDS merit/shortlist/selection
    information.

    Returns a list of dictionaries.
    """

    soup = BeautifulSoup(html, "html.parser")

    detected = []

    # --------------------------------------------------------
    # 1. Inspect hyperlinks
    # --------------------------------------------------------

    for anchor in soup.find_all("a", href=True):

        href = clean_text(anchor.get("href", ""))
        link_text = clean_text(anchor.get_text(" ", strip=True))

        if not href:
            continue

        absolute_url = urljoin(OFFICIAL_URL, href)

        combined = clean_text(
            f"{link_text} {absolute_url}"
        )

        if not is_gds_list_text(combined):
            continue

        # Ignore non-http links.
        if not absolute_url.startswith(("http://", "https://")):
            continue

        detected.append(
            {
                "url": absolute_url,
                "title": link_text or "India Post GDS List",
                "list_number": extract_list_number(combined),
                "date": extract_date(combined),
            }
        )

    # --------------------------------------------------------
    # 2. Inspect visible page text
    # --------------------------------------------------------

    page_text = clean_text(soup.get_text(" ", strip=True))

    if is_gds_list_text(page_text):

        # Use official page URL as a fallback item.
        detected.append(
            {
                "url": OFFICIAL_URL,
                "title": "India Post GDS Online Engagement",
                "list_number": extract_list_number(page_text),
                "date": extract_date(page_text),
            }
        )

    # --------------------------------------------------------
    # Remove duplicates
    # --------------------------------------------------------

    unique = {}

    for item in detected:
        key = item["url"]

        if key not in unique:
            unique[key] = item

    return list(unique.values())


# ============================================================
# Telegram
# ============================================================

def telegram_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def send_telegram_message(message: str) -> None:
    """Send a Telegram message."""

    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN environment variable is missing."
        )

    if not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID environment variable is missing."
        )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": False,
    }

    response = requests.post(
        telegram_api_url("sendMessage"),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API returned an error: {result}"
        )

    logger.info("Telegram notification sent successfully.")


def build_alert(item: dict) -> str:
    """Build the Telegram alert message."""

    return (
        "🚨 INDIA POST GDS ALERT\n\n"
        "New Merit/Shortlist Information Detected!\n\n"
        f"📌 List: {item['list_number']}\n"
        f"📅 Date: {item['date']}\n"
        f"🔗 Official Link: {item['url']}"
    )


# ============================================================
# Main monitoring logic
# ============================================================

def main() -> int:

    logger.info("==========================================")
    logger.info("India Post GDS Monitor started")
    logger.info("Official source: %s", OFFICIAL_URL)
    logger.info("==========================================")

    html = fetch_page(OFFICIAL_URL)

    detected_items = detect_lists(html)

    if not detected_items:
        logger.warning(
            "No GDS list/shortlist information was detected."
        )

        logger.warning(
            "The India Post website structure may have changed."
        )

        return 0

    logger.info(
        "Detected %s GDS-related item(s).",
        len(detected_items),
    )

    seen_urls = load_seen_urls()

    # --------------------------------------------------------
    # First run:
    # Save currently existing items as baseline.
    #
    # This prevents already-published lists from generating
    # false notifications when the system starts for the first time.
    # --------------------------------------------------------

    if not SEEN_FILE.exists():

        logger.info(
            "First run detected. Creating initial baseline."
        )

        for item in detected_items:
            seen_urls.add(item["url"])

        save_seen_urls(seen_urls)

        logger.info(
            "Initial baseline saved. No Telegram alert sent."
        )

        return 0

    # --------------------------------------------------------
    # Detect genuinely new URLs
    # --------------------------------------------------------

    new_items = [
        item
        for item in detected_items
        if item["url"] not in seen_urls
    ]

    if not new_items:

        logger.info(
            "No new GDS list detected. No notification sent."
        )

        return 0

    # --------------------------------------------------------
    # Send notifications for new items
    # --------------------------------------------------------

    for item in new_items:

        logger.info(
            "NEW GDS LIST DETECTED: %s",
            item["url"],
        )

        message = build_alert(item)

        try:
            send_telegram_message(message)

        except Exception as exc:
            logger.error(
                "Telegram notification failed: %s",
                exc,
            )

            # Do NOT mark it as seen if Telegram failed.
            # This allows the next scheduled run to retry.
            continue

        seen_urls.add(item["url"])

    save_seen_urls(seen_urls)

    logger.info("Monitoring cycle completed.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())

    except Exception as exc:
        logger.exception(
            "Monitoring failed: %s",
            exc,
        )

        raise
