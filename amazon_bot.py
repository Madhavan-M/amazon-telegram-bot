import os
import re
import html
import urllib.parse
import httpx
import feedparser
from bs4 import BeautifulSoup

# Loaded automatically from your GitHub Secrets
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
CHANNEL_ID = os.environ["CHANNEL_ID"].strip()
AMAZON_TAG = os.environ["AMAZON_TAG"].strip()

# Dedicated Amazon India Deals RSS Feed
DEAL_RSS_FEED = "https://www.desidime.com/stores/amazon-india.rss"
HISTORY_FILE = "posted_asins.txt"
MAX_POSTS_PER_RUN = 2  # Posts up to 2 fresh deals every 15 minutes to avoid spamming

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
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
    """Removes price tags like 'at Rs. 499' from RSS title to search Amazon cleanly."""
    cleaned = re.sub(r"(?i)\s*(?:at|@|for)\s*(?:rs\.?|₹|inr)\s*[\d,]+.*", "", raw_title)
    cleaned = re.sub(r"\(\d+%\s*off\)", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def find_asin_on_amazon(client: httpx.Client, search_query: str) -> str | None:
    """Searches Amazon.in for the deal title and grabs the top matching product ASIN."""
    encoded = urllib.parse.quote_plus(search_query)
    search_url = f"https://www.amazon.in/s?k={encoded}"
    try:
        resp = client.get(search_url)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for div in soup.select("div[data-asin]"):
                asin = div.get("data-asin", "").strip()
                if len(asin) == 10:
                    return asin
    except Exception:
        pass
    return None

def scrape_amazon_details(client: httpx.Client, asin: str) -> dict:
    """Attempts to scrape live price, MRP, and high-res image from Amazon.in."""
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
    except Exception:
        pass
    return details

def extract_rss_image(summary_html: str) -> str | None:
    soup = BeautifulSoup(summary_html, "html.parser")
    img = soup.find("img")
    return img.get("src") if img else None

def send_telegram_post(client: httpx.Client, title: str, price_text: str, image_url: str | None, affiliate_url: str):
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
            return

    # Fallback to text message if image fails to load
    client.post(
        f"{base_api}/sendMessage",
        json={
            "chat_id": CHANNEL_ID,
            "text": caption,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
            "disable_web_page_preview": False,
        },
    )

def main():
    posted = load_history()
    feed = feedparser.parse(DEAL_RSS_FEED)
    posts_sent = 0

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=15.0) as client:
        for entry in feed.entries[:15]:
            if posts_sent >= MAX_POSTS_PER_RUN:
                break

            raw_title = entry.get("title", "Amazon Deal")
            summary = entry.get("summary", "")
            deal_link = entry.get("link", "")

            # Unique key per RSS entry
            entry_id = entry.get("id", deal_link)
            if entry_id in posted:
                continue

            # 1. Try finding direct ASIN in RSS content, or search Amazon by product name
            asin = extract_asin(f"{deal_link} {summary}")
            search_name = clean_search_title(raw_title)
            if not asin and search_name:
                asin = find_asin_on_amazon(client, search_name)

            if asin and asin in posted:
                continue

            # 2. Build the Amazon Affiliate URL (Direct ASIN link or Affiliate Search link)
            if asin:
                affiliate_url = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
                amz_data = scrape_amazon_details(client, asin)
            else:
                encoded_q = urllib.parse.quote_plus(search_name or raw_title)
                affiliate_url = f"https://www.amazon.in/s?k={encoded_q}&tag={AMAZON_TAG}"
                amz_data = {"title": None, "price": None, "mrp": None, "image": None}

            # 3. Combine scraped details with RSS fallback details
            final_title = amz_data["title"] or raw_title
            final_image = amz_data["image"] or extract_rss_image(summary)

            if amz_data["price"]:
                price_line = f"💰 <b>Deal Price: {amz_data['price']}</b>"
                if amz_data["mrp"]:
                    price_line += f"  (MRP: <s>{html.escape(amz_data['mrp'])}</s>)"
            else:
                price_line = "💰 <b>Special Offer — Check Deal Price Below!</b>"

            # 4. Publish to Telegram & save history
            send_telegram_post(client, final_title, price_line, final_image, affiliate_url)
            save_to_history(entry_id)
            if asin:
                save_to_history(asin)

            posts_sent += 1
            print(f"Posted deal: {final_title[:60]}")

if __name__ == "__main__":
    main()
