import os
import re
import sys
import html
import urllib.parse
import httpx
import feedparser
from bs4 import BeautifulSoup

# 1. Load and sanitize GitHub Secrets
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
RAW_CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
AMAZON_TAG = os.environ.get("AMAZON_TAG", "").strip()

if not BOT_TOKEN or not RAW_CHANNEL_ID or not AMAZON_TAG:
    print("❌ ERROR: One or more GitHub Secrets (BOT_TOKEN, CHANNEL_ID, AMAZON_TAG) are empty!")
    sys.exit(1)

# Auto-fix common CHANNEL_ID mistakes (e.g., "https://t.me/mychannel" or missing "@")
if "t.me/" in RAW_CHANNEL_ID:
    CHANNEL_ID = "@" + RAW_CHANNEL_ID.split("t.me/")[-1].strip("/")
elif not RAW_CHANNEL_ID.startswith("@") and not RAW_CHANNEL_ID.lstrip("-").isdigit():
    CHANNEL_ID = "@" + RAW_CHANNEL_ID
else:
    CHANNEL_ID = RAW_CHANNEL_ID

print(f"✅ Loaded config for channel: {CHANNEL_ID} | Amazon Tag: {AMAZON_TAG}")

HISTORY_FILE = "posted_asins.txt"
MAX_POSTS_PER_RUN = 2

# Multiple RSS sources in case Cloudflare blocks one on GitHub's IP
RSS_FEEDS = [
    "https://www.desidime.com/stores/amazon-india.rss",
    "https://www.desidime.com/deals.rss",
    "https://news.google.com/rss/search?q=site:amazon.in+deal+OR+discount+OR+price+drop&hl=en-IN&gl=IN&ceid=IN:en",
]

# Guaranteed starter ASINs if all RSS feeds are temporarily blocked by cloud IP
FALLBACK_DEALS = [
    {"asin": "B09XS7JWHH", "title": "Sony WH-1000XM5 Wireless Active Noise Cancelling Headphones"},
    {"asin": "B0B6BFF57R", "title": "Apple AirTag (Pack of 1) — Smart Bluetooth Tracker"},
    {"asin": "B0CHX1W1XY", "title": "Apple iPhone 15 (128 GB) — Limited Time Deal"},
    {"asin": "B0B3CPQ5PF", "title": "boAt Airdopes 141 Bluetooth Truly Wireless in Ear Earbuds"},
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

def load_history() -> set:
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip() and not line.startswith("#")}

def save_to_history(deal_id: str):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{deal_id}\n")

def extract_asin(text: str) -> str | None:
    match = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", text)
    return match.group(1) if match else None

def clean_search_title(raw_title: str) -> str:
    cleaned = re.sub(r"(?i)\s*(?:at|@|for)\s*(?:rs\.?|₹|inr)\s*[\d,]+.*", "", raw_title)
    cleaned = re.sub(r"\(\d+%\s*off\)", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def scrape_amazon_details(client: httpx.Client, asin: str) -> dict:
    details = {"title": None, "price": None, "mrp": None, "image": None}
    try:
        resp = client.get(f"https://www.amazon.in/dp/{asin}")
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            title_el = soup.select_one("#productTitle")
            price_el = soup.select_one(".a-price-whole")
            mrp_el = soup.select_one(".a-text-price .a-offscreen")
            img_el = soup.select_one("#landingImage") or soup.select_one("#imgBlkFront")

            if title_el:
                details["title"] = title_el.get_text(strip=True)
            if price_el:
                details["price"] = f"₹{price_el.get_text(strip=True).rstrip('.')}"
            if mrp_el:
                details["mrp"] = mrp_el.get_text(strip=True)
            if img_el:
                details["image"] = img_el.get("data-old-hires") or img_el.get("src")
    except Exception as e:
        print(f"⚠️ Amazon scrape warning for {asin}: {e}")
    return details

def extract_rss_image(summary_html: str) -> str | None:
    soup = BeautifulSoup(summary_html, "html.parser")
    img = soup.find("img")
    return img.get("src") if img else None

def send_telegram_post(client: httpx.Client, title: str, price_text: str, image_url: str | None, affiliate_url: str) -> bool:
    safe_title = html.escape(title[:140])
    caption = (
        f"🔥 <b>Amazon Deal Alert!</b>\n\n"
        f"🛍 <b>{safe_title}</b>\n\n"
        f"{price_text}\n\n"
        f"👉 <a href='{affiliate_url}'>Grab Deal on Amazon</a>"
    )
    reply_markup = {
        "inline_keyboard": [[{"text": "🛒 Buy Now on Amazon", "url": affiliate_url}]]
    }

    base_api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    if image_url:
        resp = client.post(
            f"{base_api}/sendPhoto",
            json={
                "chat_id": CHANNEL_ID,
                "photo": image_url,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
        )
        if resp.status_code == 200:
            print(f"✅ Posted photo deal to Telegram: {title[:50]}")
            return True
        else:
            print(f"⚠️ sendPhoto failed ({resp.status_code}): {resp.text} — trying text post...")

    resp = client.post(
        f"{base_api}/sendMessage",
        json={
            "chat_id": CHANNEL_ID,
            "text": caption,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
            "disable_web_page_preview": False,
        },
    )
    if resp.status_code == 200:
        print(f"✅ Posted text deal to Telegram: {title[:50]}")
        return True
    else:
        print(f"❌ TELEGRAM API ERROR ({resp.status_code}): {resp.text}")
        return False

def fetch_rss_entries(client: httpx.Client) -> list:
    for feed_url in RSS_FEEDS:
        try:
            resp = client.get(feed_url)
            if resp.status_code == 200:
                parsed = feedparser.parse(resp.text)
                if parsed.entries:
                    print(f"✅ Loaded {len(parsed.entries)} deals from {feed_url}")
                    return parsed.entries
                else:
                    print(f"⚠️ Feed returned 0 entries: {feed_url}")
            else:
                print(f"⚠️ Feed HTTP {resp.status_code}: {feed_url}")
        except Exception as e:
            print(f"⚠️ Error fetching {feed_url}: {e}")
    return []

def main():
    posted = load_history()
    posts_sent = 0

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=15.0) as client:
        entries = fetch_rss_entries(client)

        for entry in entries[:15]:
            if posts_sent >= MAX_POSTS_PER_RUN:
                break

            raw_title = entry.get("title", "Amazon Deal")
            summary = entry.get("summary", "")
            deal_link = entry.get("link", "")
            entry_id = entry.get("id", deal_link)

            if entry_id in posted:
                continue

            asin = extract_asin(f"{deal_link} {summary}")
            search_name = clean_search_title(raw_title)

            if asin:
                affiliate_url = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
                amz_data = scrape_amazon_details(client, asin)
            else:
                encoded_q = urllib.parse.quote_plus(search_name or raw_title)
                affiliate_url = f"https://www.amazon.in/s?k={encoded_q}&tag={AMAZON_TAG}"
                amz_data = {"title": None, "price": None, "mrp": None, "image": None}

            final_title = amz_data["title"] or raw_title
            final_image = amz_data["image"] or extract_rss_image(summary)

            if amz_data["price"]:
                price_line = f"💰 <b>Deal Price: {amz_data['price']}</b>"
                if amz_data["mrp"]:
                    price_line += f"  (MRP: <s>{html.escape(amz_data['mrp'])}</s>)"
            else:
                price_line = "💰 <b>Special Offer — Tap Below to Check Live Price!</b>"

            if send_telegram_post(client, final_title, price_line, final_image, affiliate_url):
                save_to_history(entry_id)
                if asin:
                    save_to_history(asin)
                posts_sent += 1

        # Fallback if RSS feeds were blocked by Cloudflare on GitHub IP
        if posts_sent == 0:
            print("ℹ️ Using fallback Amazon deals for this run...")
            for item in FALLBACK_DEALS:
                if posts_sent >= MAX_POSTS_PER_RUN:
                    break
                asin = item["asin"]
                if asin in posted:
                    continue
                amz_data = scrape_amazon_details(client, asin)
                title = amz_data["title"] or item["title"]
                affiliate_url = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
                price_line = (
                    f"💰 <b>Deal Price: {amz_data['price']}</b>"
                    if amz_data["price"]
                    else "💰 <b>Deal Alert — Check Latest Price on Amazon!</b>"
                )
                if send_telegram_post(client, title, price_line, amz_data["image"], affiliate_url):
                    save_to_history(asin)
                    posts_sent += 1

    if posts_sent == 0:
        print("❌ No posts were sent. Check the TELEGRAM API ERROR message above!")
        sys.exit(1)

if __name__ == "__main__":
    main()
