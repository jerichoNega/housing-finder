import asyncio
import json
import os
import random
import time
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SEEN_FILE = "seen_listings.json"

# Target URLs for Dutch Tech Hubs (Under €1100)
TARGET_URLS = [
    "https://www.pararius.com/apartments/amsterdam/0-1100",
    "https://www.pararius.com/apartments/utrecht/0-1100",
    "https://www.pararius.com/apartments/eindhoven/0-1100",
    "https://www.pararius.com/apartments/rotterdam/0-1100",
    "https://www.pararius.com/apartments/den-haag/0-1100"
]

def load_seen_listings():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
        except:
            return set()
    return set()

def save_seen_listings(seen_set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen_set), f)

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            print(f"Telegram error: {response.text}")
    except Exception as e:
        print(f"Error sending telegram message: {e}")

async def scrape_pararius(page, url):
    city = url.split("/")[-2].capitalize()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking {city}...")
    try:
        # Navigate and wait for content
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        # Cloudflare/Loading wait - increased to be safe
        await asyncio.sleep(25)
        
        # Human-like interaction
        await page.mouse.wheel(0, random.randint(500, 1500))
        await asyncio.sleep(random.uniform(2, 4))
        
        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")
        
        listings = []
        # Pararius uses <section class="listing-search-item">
        items = soup.find_all("section", class_="listing-search-item")
        
        if not items:
            title = await page.title()
            if "Just a moment" in title:
                print(f"  Blocked by Cloudflare for {city}")
            else:
                print(f"  No listings found in {city}. Page title: {title}")
        
        for item in items:
            try:
                # Find all links in the section
                links = item.find_all("a", href=True)
                
                # The property link usually starts with /apartment-for-rent/ or /studio-for-rent/
                prop_link = None
                title = "Unknown Apartment"
                
                for l in links:
                    href = l["href"]
                    text = l.get_text(strip=True)
                    if ("/apartment-for-rent/" in href or "/studio-for-rent/" in href):
                        prop_link = "https://www.pararius.com" + href
                        if text and len(text) > 5:
                            title = text
                
                if not prop_link:
                    continue
                    
                price_elem = item.find("div", class_="listing-search-item__price")
                price = price_elem.get_text(strip=True) if price_elem else "Price not shown"
                
                location_elem = item.find("div", class_="listing-search-item__location")
                location = location_elem.get_text(strip=True) if location_elem else city
                
                listings.append({
                    "id": prop_link,
                    "title": title,
                    "url": prop_link,
                    "price": price,
                    "location": location
                })
            except Exception as e:
                print(f"Error parsing listing: {e}")
                
        return listings
    except Exception as e:
        print(f"Scrape error: {e}")
        return []

async def main():
    seen_listings = load_seen_listings()
    
    async with async_playwright() as p:
        # Use a more realistic browser launch
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        
        # We will rotate through cities one by one to stay stealthy
        city_index = 0
        
        while True:
            try:
                url = TARGET_URLS[city_index]
                listings = await scrape_pararius(page, url)
                
                new_found = False
                for listing in listings:
                    if listing["id"] not in seen_listings:
                        msg = (
                            f"🏠 <b>New Listing Found!</b>\n\n"
                            f"📍 <b>{listing['title']}</b>\n"
                            f"💶 {listing['price']}\n"
                            f"📍 {listing['location']}\n\n"
                            f"🔗 <a href='{listing['url']}'>View Property</a>"
                        )
                        send_telegram_message(msg)
                        seen_listings.add(listing["id"])
                        new_found = True
                        print(f"Notified: {listing['title']}")
                
                if new_found:
                    save_seen_listings(seen_listings)
                
                # Move to next city for the next check
                city_index = (city_index + 1) % len(TARGET_URLS)
                
            except Exception as e:
                print(f"Loop error: {e}")
            
            # Wait 4-7 minutes between single city checks
            wait_time = random.randint(240, 420)
            print(f"Waiting {wait_time // 60} minutes until checking the next city...")
            await asyncio.sleep(wait_time)

if __name__ == "__main__":
    asyncio.run(main())
