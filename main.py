import asyncio
import json
import os
import random
import time
import urllib.parse
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SEEN_FILE = os.path.expanduser("~/housing-finder/seen_listings.json")

# Target URLs
# Holland2Stay, Vesteda, and Funda are dropped: Holland2Stay and Funda serve a
# bot-check interstitial to headless Chromium even with stealth applied, and
# Vesteda's /en/search results load via a JS/API call that returns nothing
# without a real browser session, so all three would only ever scrape empty.
TARGETS = [
    {"name": "Ad Hoc", "url": "https://www.adhocbeheer.nl/aanbod/", "type": "Antikraak"},
    {"name": "Alvast", "url": "https://alvast.nl/aanbod/", "type": "Antikraak"},
    {"name": "Pararius", "url": "https://www.pararius.com/apartments/eindhoven/radius-15km/0-1100", "type": "Portal"},
]

# Ad Hoc's grid mixes residential units in with office/storage/retail space;
# skip anything whose title flags it as non-residential.
NON_RESIDENTIAL_KEYWORDS = [
    "werkruimte", "kantoorruimte", "atelierruimte", "opslagruimte",
    "bedrijfsruimte", "winkelruimte", "praktijkruimte", "garage", "parkeerplaats",
]

def is_residential(title):
    lowered = title.lower()
    return not any(kw in lowered for kw in NON_RESIDENTIAL_KEYWORDS)

# Ad Hoc and Alvast are nationwide grids with no city-scoped URL, so listings
# are kept only if their title/location mentions a place within roughly 15-20km
# of Eindhoven.
EINDHOVEN_AREA_KEYWORDS = [
    "eindhoven", "veldhoven", "best", "son en breugel", "nuenen", "geldrop",
    "mierlo", "waalre", "valkenswaard", "heeze", "helmond", "oirschot",
    "sint-oedenrode", "boxtel", "deurne", "asten", "someren",
]

def is_in_eindhoven_area(text):
    lowered = text.lower()
    return any(kw in lowered for kw in EINDHOVEN_AREA_KEYWORDS)

def load_seen_listings():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
        except: return set()
    return set()

def save_seen_listings(seen_set):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen_set), f)
    except Exception as e:
        print(f"Error saving seen listings: {e}")

def send_telegram_message(message, listing_url=None, agent_name=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    buttons = []
    if listing_url: buttons.append({"text": "🔗 View Property", "url": listing_url})
    if agent_name and agent_name not in ["Unknown Agent", "Ad Hoc", "Alvast", "Unknown", "Check Funda"]:
        buttons.append({"text": "🔍 Search Agent", "url": f"https://www.google.com/search?q={urllib.parse.quote(agent_name + ' contact')}"})
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": [buttons]}) if buttons else None
    }
    try: 
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            print(f"Telegram API Error: {resp.text}")
    except Exception as e: print(f"Telegram error: {e}")

async def scrape_site(browser, target):
    site_name = target["name"]
    print(f"[{time.strftime('%H:%M:%S')}] Checking {site_name}...")
    
    context = await browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)
    
    try:
        await page.goto(target["url"], wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(random.uniform(5, 10))
        
        # Check for Cloudflare or Blocks
        title = await page.title()
        if "Just a moment" in title:
            print(f"  Blocked by Cloudflare on {site_name}")
            await context.close()
            return []

        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")
        listings = []

        if site_name == "Pararius":
            items = soup.find_all("section", class_="listing-search-item")
            print(f"  Found {len(items)} items on Pararius")
            for item in items:
                link = item.find("a", class_="listing-search-item__link--title")
                if not link: continue
                url = "https://www.pararius.com" + link["href"]
                price_elem = item.find("span", class_="listing-search-item__price-main")
                price = price_elem.get_text(strip=True) if price_elem else "N/A"
                agent = item.find("div", class_="listing-search-item__info").get_text(strip=True) if item.find("div", class_="listing-search-item__info") else "Unknown"
                listings.append({"id": url, "title": link.get_text(strip=True), "url": url, "price": price, "agent": agent, "site": site_name})
        
        elif site_name == "Ad Hoc":
            items = soup.find_all("article", class_="wpgb-card")
            print(f"  Found {len(items)} items on Ad Hoc")
            for item in items:
                link = item.find("a", class_="wpgb-card-thumb-link", href=True)
                if not link: continue
                url = link["href"]
                title = link.get("aria-label", "Ad Hoc listing")
                if not is_residential(title): continue
                if not is_in_eindhoven_area(title): continue
                listings.append({"id": url, "title": title, "url": url, "price": "See listing", "agent": "Ad Hoc", "site": site_name})

        elif site_name == "Alvast":
            items = soup.find_all("div", class_="object")
            print(f"  Found {len(items)} items on Alvast")
            for item in items:
                link = item.find_parent("a", href=True)
                if not link: continue
                url = "https://alvast.nl" + link["href"]
                title_elem = item.find("div", class_="title")
                title = title_elem.get_text(strip=True) if title_elem else "Alvast"
                if not is_in_eindhoven_area(title): continue
                price_elem = item.find(string=lambda s: s and "€" in s)
                price = price_elem.strip() if price_elem else "Cheap"
                listings.append({"id": url, "title": title, "url": url, "price": price, "agent": "Alvast", "site": site_name})

        await context.close()
        return listings
    except Exception as e:
        print(f"Error checking {site_name}: {e}")
        try: await context.close()
        except: pass
        return []

async def main():
    seen_listings = load_seen_listings()
    target_index = 0
    while True:
        try:
            async with async_playwright() as p:
                # Use a larger browser launch
                browser = await p.chromium.launch(headless=True)
                target = TARGETS[target_index]
                listings = await scrape_site(browser, target)
                
                new_found = False
                for l in listings:
                    if l["id"] not in seen_listings:
                        msg = f"🚨 <b>{l['site']} ALERT</b>\n\n📍 <b>{l['title']}</b>\n💶 <b>Price:</b> {l['price']}\n👤 <b>Agent:</b> {l['agent']}\n\n⚡ <i>Check the property now!</i>"
                        send_telegram_message(msg, l["url"], l["agent"])
                        seen_listings.add(l["id"])
                        new_found = True
                        print(f"  NEW: {l['title']}")
                
                if new_found: save_seen_listings(seen_listings)
                await browser.close()
            
            target_index = (target_index + 1) % len(TARGETS)
            wait = random.randint(180, 420)
            print(f"Waiting {wait//60}m until next check...")
            await asyncio.sleep(wait)
        except Exception as e:
            print(f"Global Loop Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
