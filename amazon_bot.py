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
    print("❌ ERROR: One or more GitHub Secrets are empty!")
    sys.exit(1)

if "t.me/" in RAW_CHANNEL_ID:
    CHANNEL_ID = "@" + RAW_CHANNEL_ID.split("t.me/")[-1].strip("/")
elif not RAW_CHANNEL_ID.startswith("@") and not RAW_CHANNEL_ID.lstrip("-").isdigit():
    CHANNEL_ID = "@" + RAW_CHANNEL_ID
else:
    CHANNEL_ID = RAW_CHANNEL_ID

HISTORY_FILE = "posted_asins.txt"
MAX_POSTS_PER_RUN = 2

# Curated high-demand deal ASINs to guarantee immediate, stunning photo posts
CURATED_DEALS = [
    {"asin": "B0CHX1W1XY", "title": "Apple iPhone 15 (128 GB) — Black"},
    {"asin": "B09XS7JWHH", "title": "Sony WH-1000XM5 Wireless Noise Cancelling Headphones"},
    {"asin": "B0B6BFF57R", "title": "Apple AirTag (Pack of 1)"},
    {"asin": "B07WJRVP97", "title": "Echo Dot (5th Gen) | Smart speaker with Alexa"},
    {"asin": "B0BS186V83", "title": "OnePlus Nord CE 3 Lite 5G (Chromatic Gray, 8GB RAM, 128GB Storage)"},
    {"asin": "B0BDHWDR12", "title": "Samsung Galaxy M14 5G (ICY Silver, 4GB, 128GB Storage)"},
]

RSS_FEEDS = [
    "https://www.desidime.com/stores/amazon-india.rss",
    "https://www.desidime.com/deals.rss",
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
        print(f"⚠️ Scrape error for ASIN {asin}: {e}")
    return details

def send_telegram_post(client: httpx.Client, title: str, price_text: str, image_url: str | None, affiliate_url: str) -> bool:
    safe_title = html.escape(title[:140])
    
    # Matches your desired clean format layout (no raw text links)
    caption = (
        f"🔥 <b>Amazon Price Drop Alert!</b>\n\n"
        f"🛍 <b>{safe_title}</b>\n\n"
        f"{price_text}\n\n"
        f"👉 <a href='{affiliate_url}'>Grab Deal on Amazon</a>"
    )
    
    # Interactive button matching professional channels
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
            print(f"✅ Posted deal with photo: {title[:50]}")
            return True

    resp = client.post(
        f"{base_api}/sendMessage",
        json={
            "chat_id": CHANNEL_ID,
            "text": caption,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
            "disable_web_page_preview": True,
        },
    )
    if resp.status_code == 200:
        print(f"✅ Posted deal text: {title[:50]}")
        return True

    print(f"❌ Telegram API Error: {resp.text}")
    return False

def main():
    posted = load_history()
    posts_sent = 0

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=15.0) as client:
        # 1. Try pulling live items from RSS feeds first
        entries = []
        for feed_url in RSS_FEEDS:
            try:
                resp = client.get(feed_url)
                if resp.status_code == 200:
                    parsed = feedparser.parse(resp.text)
                    if parsed.entries:
                        entries.extend(parsed.entries)
            except Exception:
                pass

        for entry in entries:
            if posts_sent >= MAX_POSTS_PER_RUN:
                break
            deal_link = entry.get("link", "")
            summary = entry.get("summary", "")
            entry_id = entry.get("id", deal_link)

            if entry_id in posted:
                continue

            asin = extract_asin(f"{deal_link} {summary}")
            if not asin or asin in posted:
                continue

            amz_data = scrape_amazon_details(client, asin)
            if not amz_data["title"]:
                continue

            affiliate_url = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
            price_line = f"💰 <b>Deal Price: {amz_data['price']}</b>" + (f"  (MRP: <s>{html.escape(amz_data['mrp'])}</s>)" if amz_data["mrp"] else "") if amz_data["price"] else "💰 <b>Special Offer — Tap Below for Live Price!</b>"

            if send_telegram_post(client, amz_data["title"], price_line, amz_data["image"], affiliate_url):
                save_to_history(entry_id)
                save_to_history(asin)
                posts_sent += 1

        # 2. If RSS feeds didn't yield a direct ASIN item, pull from our curated deal rotation
        if posts_sent == 0:
            print("ℹ️ Posting from verified deal queue...")
            for item in CURATED_DEALS:
                if posts_sent >= MAX_POSTS_PER_RUN:
                    break
                asin = item["asin"]
                if asin in posted:
                    continue

                amz_data = scrape_amazon_details(client, asin)
                title = amz_data["title"] or item["title"]
                affiliate_url = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
                
                if amz_data["price"]:
                    price_line = f"💰 <b>Deal Price: {amz_data['price']}</b>"
                    if amz_data["mrp"]:
                        price_line += f"  (MRP: <s>{html.escape(amz_data['mrp'])}</s>)"
                else:
                    price_line = "💰 <b>Limited Time Deal — Check Live Price on Amazon</b>"

                if send_telegram_post(client, title, price_line, amz_data["image"], affiliate_url):
                    save_to_history(asin)
                    posts_sent += 1

    print(f"Finished run. Total posts sent: {posts_sent}")

if __name__ == "__main"></script>
