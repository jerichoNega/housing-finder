"""Dutch housing finder: new self-contained studios in and around Eindhoven, straight to Telegram with
the landlord's own phone number and a ready-to-send reaction.

Run once (launchd every 3 min):   python main.py --once
Run forever (terminal):           python main.py
Test without sending anything:    python main.py --once --dry-run
One site, with reasons:           python main.py --site Funda
"""
import argparse
import html
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests

# ---------------------------------------------------------------- settings

MAX_PRICE = int(os.getenv("MAX_PRICE", "1100"))

# Below this a "monthly rent" is a parsing error, a parking spot or a per-week price.
MIN_PRICE = 300

# Eindhoven plus the towns around it within about 15 minutes by bike. A listing only counts if its
# place or link names one of these.
AREA = ["eindhoven", "veldhoven", "best", "geldrop", "son en breugel", "son-en-breugel", "nuenen", "waalre"]

# Towns for the sites that search per town instead of by radius (URL slugs).
TOWNS = ["eindhoven", "veldhoven", "best", "geldrop", "son-en-breugel", "nuenen", "waalre"]

# Self-contained only: a studio, or an apartment that is a single room (a studio under
# another name) or at most this many m².
SMALL_APARTMENT_M2 = 40

# A listing counts as "fresh" (🔥) when its site was checked fine at most this many minutes before
# it showed up, so you know it went online minutes ago.
FRESH_MINUTES = 12

ROOT = Path(__file__).resolve().parent

# Local secrets: .env next to this file with TELEGRAM_TOKEN=... and TELEGRAM_CHAT_ID=...
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() and not k.startswith("#") and v.strip():
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
MAX_PRICE = int(os.getenv("MAX_PRICE", MAX_PRICE))  # .env is loaded only now
STATE_FILE = Path(os.getenv("STATE_FILE", ROOT / "state.json"))
PROFILE_FILE = ROOT / "profile.json"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Fall back to a real (headless) Chrome when every fingerprint is blocked. Set BROWSER_FALLBACK=0 to turn off.
BROWSER_FALLBACK = os.getenv("BROWSER_FALLBACK", "1") != "0"

# A site counts as broken after this many runs in a row with an error or zero results.
BROKEN_AFTER = 8

# Minimum minutes between checks per site (default: every run). Funda's bot protection (Akamai) blocks
# the whole home IP when asked every 3 minutes, so it is asked less often.
SITE_EVERY = {"Funda": 10}

# How often a full check runs (launchd starts the script every minute; it skips when it is too soon).
# Most listings go online in the morning, at lunch and in the evening.
def run_every_minutes(hour):
    if 8 <= hour < 10 or 12 <= hour < 13 or 17 <= hour < 21:
        return 1
    if 0 <= hour < 7:
        return 6
    return 3


PAUSE_FILE = ROOT / "PAUSED"  # exists = no checks (set by /pause in Telegram or by --login)

# After a block, leave the site alone for 5, 10, 20 ... minutes (max 2 hours) so the block can lift.
BACKOFF_MAX_MIN = 120

# Phone numbers that belong to a portal, never to a landlord.
PORTAL_PHONES = {"+31202402010", "+31207944300"}
PORTAL_DOMAINS = ("pararius", "huurwoningen", "funda", "casco-media", "treehouse", "facebook", "immomiet",
                  "rentaroof", "toitpourtoi", "cbs.nl", "google", "cloudfront", "amazonaws",
                  # photo hosts of agent software, not the agent itself
                  "fastly", "realworks", "ogonline", "sharinpix", "windows.net", "kolibri", "tiara", "cdn", "imgix")

# Listings copied from these sites are not normal rentals (ruilwoning = swapping a social-housing
# home, you need one to swap).
NOT_RENTALS = ("ruilwoning", "gapph", "ad-hoc", "camelot", "villex", "zwerfkei")

# Words in a listing's own text that mean you cannot simply rent it.
NOT_RENTAL_TEXT = ("woningruil", "ruilwoning", "om te ruilen", "te kunnen ruilen", "ruilen met",
                   "alleen voor studenten", "uitsluitend voor studenten", "student housing only",
                   "students only", "only for students", "inschrijving bij woonbedrijf", "wooniezie",
                   "antikraak", "anti-kraak", "tijdelijk bewoning", "leegstandbeheer", "leegstandwet",
                   "tijdelijke huur op grond van")

# Real studios here cost roughly €25-45 per m². Far below that is a swap, social housing
# or a scam, so such a listing is only sent after its page has been read and found clean.
SUSPICIOUS_PER_M2 = 18


# ---------------------------------------------------------------- fetching
# Cloudflare on Pararius/Huurwoningen/Funda blocks some browser fingerprints and not others, and that
# changes over time. So: try several, remember per site which one last worked, keep cookies, and as a
# last resort open the page in a real Chrome.

FINGERPRINTS = ["safari184", "safari180", "firefox135", "safari260", "chrome136", "safari18_0_ios", "edge101"]
_sessions = {}
_prefer = {}  # host -> fingerprint that worked last; filled from state at start


class Blocked(RuntimeError):
    pass


def _host(url):
    return urllib.parse.urlparse(url).netloc.lower()


def _session(fp):
    if fp not in _sessions:
        _sessions[fp] = requests.Session(impersonate=fp)
    return _sessions[fp]


BLOCK_MARKERS = ("Just a moment", "Even geduld", "challenges.cloudflare.com", "<TITLE>Access Denied",
                 "<title>Access Denied", "Je bent bijna op de pagina", "captcha-delivery")


def _is_block_page(page):
    head = page[:4000]
    return any(m in head for m in BLOCK_MARKERS)


def _is_challenge(r):
    return _is_block_page(r.text)


def get(url, method="GET", json_body=None, browser=True):
    host = _host(url)
    order = [_prefer[host]] if host in _prefer else []
    order += [f for f in FINGERPRINTS if f not in order]
    last = ""
    for i, fp in enumerate(order[:4]):
        try:
            r = _session(fp).request(method, url, json=json_body, timeout=40,
                                     headers={"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"})
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            continue
        if r.status_code == 200 and not _is_challenge(r):
            _prefer[host] = fp
            return r.text
        last = f"HTTP {r.status_code}{' (bot check)' if _is_challenge(r) else ''}"
        if r.status_code in (404, 410):
            raise RuntimeError(last)
        time.sleep(0.5 + random.random())
    if browser and BROWSER_FALLBACK and method == "GET":
        try:
            return browser_get(url)
        except Exception as e:
            last += f"; browser: {str(e)[:80]}"
    raise Blocked(last)


_browser = None


def browser_get(url):
    """Open the page in your real Chrome with its own profile, so Cloudflare cookies stick. Headless Chrome
    is caught by Cloudflare, so it runs as a normal window placed off-screen (a Chrome icon may show in
    the Dock for a few seconds)."""
    page = browser_context().new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        for _ in range(10):
            if not _is_block_page(page.content()):
                break
            page.wait_for_timeout(1500)
        content = page.content()
        if _is_block_page(content):
            raise Blocked(f"bot check in browser too ({page.title()[:40]})")
        return content
    finally:
        page.close()


def _real_chrome_ua():
    try:
        out = subprocess.run(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],
                             capture_output=True, text=True, timeout=10).stdout
        major = re.search(r"(\d+)\.", out).group(1)
        return (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
    except Exception:
        return None


def browser_context():
    global _browser
    from playwright.sync_api import sync_playwright
    if _browser is None:
        pw = sync_playwright().start()
        # Headless by default so Chrome never pops up or steals focus while you work. HEADLESS=0 shows it again.
        headless = os.environ.get("HEADLESS", "1") != "0"
        args = ["--disable-blink-features=AutomationControlled", "--window-size=1280,900"]
        ua = _real_chrome_ua()
        if headless and ua:
            args.append(f"--user-agent={ua}")  # headless says "HeadlessChrome" in its UA; sites block that
        if not headless:
            args.append("--window-position=-3000,-3000")
        ctx = pw.chromium.launch_persistent_context(
            str(ROOT / ".browser-profile"), channel="chrome", headless=headless, locale="nl-NL",
            viewport={"width": 1280, "height": 900}, args=args,
            ignore_default_args=["--enable-automation"])
        restore_session_cookies(ctx)
        _browser = (pw, ctx)
    return _browser[1]


SESSION_COOKIES = ROOT / ".browser-profile" / "session-cookies.json"


def save_session_cookies(ctx):
    """Chrome forgets 'session only' cookies when it closes, and that is where Pararius keeps your login
    (also a Google login). Keep a copy and put them back on the next start."""
    try:
        keep = [c for c in ctx.cookies() if c.get("expires", -1) in (-1, None) or c["expires"] < 0]
        if keep:
            SESSION_COOKIES.write_text(json.dumps(keep))
            SESSION_COOKIES.chmod(0o600)
    except Exception:
        pass


def restore_session_cookies(ctx):
    try:
        ctx.add_cookies([{k: v for k, v in c.items() if k != "expires"} for c in json.loads(SESSION_COOKIES.read_text())])
    except Exception:
        pass


def close_browser():
    global _browser
    if _browser:
        save_session_cookies(_browser[1])
        try:
            _browser[1].close()
            _browser[0].stop()
        except Exception:
            pass
        _browser = None


def soup(url):
    return BeautifulSoup(get(url), "html.parser")


# ---------------------------------------------------------------- helpers

def text(el):
    return el.get_text(" ", strip=True) if el else ""


def to_int(s):
    """'€ 1.235,- /mnd' -> 1235, '22 m²' -> 22. None when there is no number."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = re.sub(r"(?<=\d)[.\s](?=\d{3}(?!\d))", "", str(s).replace("\xa0", " "))  # 1.235 / 1 235 -> 1235
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def listing(site, url, title, price=None, place="", size=None, rooms=None, kind="", image=None,
            agent="", phone="", agent_site=""):
    return {
        "site": site, "url": url, "title": title.strip(), "price": price, "place": place.strip(),
        "size": size, "rooms": rooms, "kind": (kind or title.split(" ")[0]).strip().lower(), "image": image,
        "agent": agent, "phone": phone, "agent_site": agent_site,
    }


def phones_in(page):
    """All tel: numbers on a page, normalised to +31..., portal numbers removed."""
    out = []
    for raw in re.findall(r'tel:\s*([+\d][\d ()+-]{7,})', page):
        n = re.sub(r"[^\d+]", "", raw)
        if n.startswith("00"):
            n = "+" + n[2:]
        elif n.startswith("+0"):
            n = "+31" + n[2:]
        elif n.startswith("0"):
            n = "+31" + n[1:]
        n = n.replace("+310", "+31")
        if n not in PORTAL_PHONES and n not in out and len(n) >= 11:
            out.append(n)
    return out


def pretty_phone(n):
    """+31402440244 -> 040 244 0244, +31622259359 -> 06 2225 9359."""
    if not n.startswith("+31"):
        return n
    d = "0" + n[3:]
    if d.startswith("06"):
        return f"{d[:2]} {d[2:6]} {d[6:]}"
    if d.startswith(("010", "020", "030", "040", "050", "070", "085", "088", "013")):
        return f"{d[:3]} {d[3:6]} {d[6:]}"
    return f"{d[:4]} {d[4:7]} {d[7:]}"


def site_of(url):
    """https://crm.goethvastgoed.nl/x -> goethvastgoed.nl"""
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    parts = host.split(".")
    return ".".join(parts[-3:]) if parts[-2] in ("co", "com") and len(parts) > 2 else ".".join(parts[-2:])


def per_town(fn):
    """Run fn(town) for every town; one town failing is fine, all failing is an error."""
    out, errors = [], []
    for town in TOWNS:
        try:
            out += fn(town)
        except RuntimeError as e:
            if "404" not in str(e):  # town has no page on this site
                errors.append(f"{town}: {e}")
    if errors and not out:
        raise RuntimeError("; ".join(errors[:2]))
    return out


# ---------------------------------------------------------------- sites
# Each returns every listing on its first results page (newest first); filtering happens later.

def pararius_style(site, base, url):
    """Pararius and Huurwoningen run on the same platform with the same markup."""
    out = []
    for it in soup(url).select("section.listing-search-item"):
        a = it.select_one("a.listing-search-item__link--title")
        if not a:
            continue
        src = it.select_one("source[srcset], img[src]")
        image, origin = None, ""
        if src is not None:
            image = (src.get("srcset") or src.get("src") or "").split(",")[-1].strip().split(" ")[0] or None
            # Photos are proxied with the agent's own URL inside: original_uri=https://vbtverhuurmakelaars.nl/...
            m = re.search(r"original_uri=([^&]+)", image or "")
            if m:
                origin = site_of(urllib.parse.unquote(m.group(1)))
                if any(p in origin for p in PORTAL_DOMAINS):
                    origin = ""
        agent = it.select_one(".listing-search-item__info a")
        price_text = text(it.select_one(".listing-search-item__price-main, .listing-search-item__price"))
        out.append(listing(
            site, urllib.parse.urljoin(base, a["href"]), text(a),
            price=None if re.search(r"week|dag|day", price_text, re.I) else to_int(price_text),
            place=text(it.select_one(".listing-search-item__sub-title")),
            size=to_int(text(it.select_one(".illustrated-features__item--surface-area"))),
            rooms=to_int(text(it.select_one(".illustrated-features__item--number-of-rooms"))),
            image=image, agent=text(agent), agent_site=origin,
        ))
    return out


def pararius():
    return pararius_style("Pararius", "https://www.pararius.nl",
                          f"https://www.pararius.nl/huurwoningen/eindhoven/0-{MAX_PRICE}/straal-10")


def huurwoningen():
    return pararius_style("Huurwoningen", "https://www.huurwoningen.nl",
                          f"https://www.huurwoningen.nl/in/eindhoven/?price=0-{MAX_PRICE}&radius=10"
                          "&sort=published_at&direction=desc")


def funda():
    area = urllib.parse.quote('["eindhoven,10km"]')
    price = urllib.parse.quote(f'"0-{MAX_PRICE}"')
    s = soup(f"https://www.funda.nl/zoeken/huur?selected_area={area}&price={price}&sort=%22date_down%22")
    out = []
    for card in s.select("div.border.border-neutral-20"):
        a = card.select_one('a[href*="/detail/huur/"]')
        if not a:
            continue
        parts = [p for p in card.get_text("|", strip=True).split("|") if p.strip()]
        parts = [p for p in parts if p not in ("Nieuw", "Uitgelicht", "Topper", "Blikvanger")]
        href = a["href"]
        kind = re.search(r"/detail/huur/[^/]+/([a-z]+)-", href)
        price_i = next((i for i, p in enumerate(parts) if "€" in p), None)
        if price_i is None:
            continue
        address = parts[price_i + 1] if len(parts) > price_i + 1 else ""
        place = parts[price_i + 2] if len(parts) > price_i + 2 else ""
        size_i = next((i for i, p in enumerate(parts) if re.fullmatch(r"\d+\s*m²", p.strip())), None)
        rooms = to_int(parts[size_i + 1]) if size_i is not None and len(parts) > size_i + 1 and parts[size_i + 1].strip().isdigit() else None
        img = card.select_one("img")
        image = None
        if img:
            image = (img.get("srcset") or img.get("src") or "").split(",")[-1].strip().split(" ")[0] or None
        k = kind.group(1) if kind else "woning"
        out.append(listing(
            "Funda", urllib.parse.urljoin("https://www.funda.nl", href), f"{k.capitalize()} {address}",
            price=None if "week" in parts[price_i] else to_int(parts[price_i]), place=place,
            size=to_int(parts[size_i]) if size_i is not None else None, rooms=rooms, kind=k,
            image=image, agent=parts[-1] if parts[-1] not in (address, place) and not re.search(r"\d", parts[-1]) else "",
        ))
    return out


def wonen123():
    def town(t):
        out = []
        for it in soup(f"https://www.123wonen.nl/huurwoningen/in/{t}/sort/newest").select(".pandlist-container"):
            if it.select_one(".pand-status"):  # verhuurd / in optie
                continue
            a = it.select_one("a[href*='/huur/']")
            if not a:
                continue
            specs = {text(li.select_one("span")): text(li.select("span")[-1]) for li in it.select(".pand-specs li")}
            img = it.select_one("[data-src]")
            out.append(listing(
                "123Wonen", a["href"], f"{specs.get('Type', 'Woning')} {text(it.select_one('.pand-address'))}",
                price=to_int(text(it.select_one(".pand-price"))), place=text(it.select_one(".pand-title")),
                size=to_int(specs.get("Woonoppervlakte")), kind=specs.get("Type", "").split(" ")[0],
                image=img.get("data-src") if img else None, agent="123Wonen",
                agent_site="123wonen.nl",
            ))
        return out
    return per_town(town)


def ikwilhuren():
    def town(t):
        out = []
        for it in soup(f"https://ikwilhuren.nu/aanbod/{t}/").select(".card-woning"):
            a = it.select_one("a.stretched-link")
            if not a:
                continue
            status = text(it.select_one(".badge"))
            if status and "te huur" not in status.lower():
                continue
            bits = [text(s) for s in it.select(".dotted-spans > span")]
            img = it.select_one("img")
            out.append(listing(
                "ikwilhuren (MVGM)", urllib.parse.urljoin("https://ikwilhuren.nu", a["href"]), text(a),
                price=to_int(bits[0]) if bits else None,
                place=text(it.select_one(".card-body > span:not(.card-title):not(.small)")) or t,
                size=to_int(next((b for b in bits if re.search(r"\d\s*m\s*(2|²)", b)), None)),
                image=(img.get("src") or img.get("data-src")) if img else None,
                agent="MVGM (register on ikwilhuren.nu)", agent_site="ikwilhuren.nu",
            ))
        return out
    return per_town(town)


def vesteda():
    body = {"filters": [], "latitude": 51.44, "longitude": 5.47, "place": "Eindhoven",
            "placeObject": {"placeType": 1}, "radius": 12, "rootId": 1303, "culture": "nl",
            "sortType": 0, "page": 1, "pageSize": 200}
    data = json.loads(get("https://www.vesteda.com/api/units/search/facet", method="POST", json_body=body))
    out = []
    for o in data["results"]["objects"]:
        if o.get("onlySixtyFivePlus") or (o.get("ageFrom") or 0) > 30:
            continue
        street = f"{o['street']} {o.get('houseNumber') or ''}{o.get('houseNumberAddition') or ''}".strip()
        bedrooms = o.get("numberOfBedRooms") or 0
        out.append(listing(
            "Vesteda", "https://www.vesteda.com" + o["url"], f"Appartement {street}",
            price=int(o["priceUnformatted"] or 0) or None, place=f"{o.get('postalCode') or ''} {o['city']}",
            size=o.get("size"), rooms=bedrooms + 1, kind="studio" if bedrooms == 0 else "appartement",
            image=o.get("imageBig"), agent="Vesteda (react with a Vesteda account)", agent_site="vesteda.com",
        ))
    return out


def househunting():
    out = []
    for it in soup("https://househunting.nl/woningaanbod/?type=huur").select("li.location"):
        a = it.select_one("a[href*='/woningaanbod/']")
        if not a:
            continue
        img = it.select_one("img")
        out.append(listing(
            "HouseHunting", a["href"], f"Woning {text(it.select_one('.location_street'))}",
            price=to_int(text(it.select_one(".location_price"))), place=text(it.select_one(".location_city")),
            kind="?",  # type and size are only on the listing page; read there
            image=img.get("src") if img else None, agent="HouseHunting (viewing via the form only)",
            agent_site="househunting.nl",
        ))
    return out


KINDS = ("studio", "appartement", "apartment", "kamer", "room", "woonhuis", "huis", "house", "bovenwoning",
         "benedenwoning", "maisonnette", "penthouse", "eengezinswoning", "tussenwoning", "hoekwoning")


def agent_cards(site, url, link_pattern, agent, base=None):
    """Generic parser for an agent's own listing page: every link to a listing whose card shows a price.
    Reads type, price, m² and place from the card text."""
    out, seen = [], set()
    for a in soup(url).select("a[href]"):
        href = urllib.parse.urljoin(base or url, a["href"])
        if not re.search(link_pattern, href) or href in seen:
            continue
        card = a
        for _ in range(5):
            if "€" in card.get_text():
                break
            card = card.parent
        t = card.get_text(" | ", strip=True)
        if "€" not in t or len(t) > 600:
            continue
        seen.add(href)
        low = t.lower()
        if re.search(r"\bverhuurd\b|\brented\b|onder optie|under option|\bkoop\b|k\.k\.", low):
            continue
        kind = next((k for k in KINDS if re.search(rf"\b{k}\b", low)), "?")
        price = re.search(r"€\s*([\d.]+)", t)
        size = re.search(r"(\d{2,3})\s*m(?:2|²)", t)
        rooms = re.search(r"(\d)\s*(?:kmr|kamer|kamers|rooms?)\b", low)
        slug = urllib.parse.unquote(href.rstrip("/").split("/")[-1])
        place = next((p for p in AREA if p in low), "")
        parts = [x.strip() for x in t.split("|")]
        street = next((x.split(",")[0] for x in parts if re.match(r"^[A-Za-z][^,]+,\s*\d{4}\s?[A-Z]{2}", x)), None) \
            or next((x for x in parts if x.lower() not in KINDS and not re.search(r"[€\d()]", x)
                     and x.lower() not in ("nieuw", "new", place) and len(x) > 3), slug)
        out.append(listing(site, href, f"{kind.capitalize() if kind != '?' else 'Woning'} {street.split(',')[0]}",
                           price=to_int(price.group(1)) if price else None, place=f"{place} {href}" if place else href,
                           size=int(size.group(1)) if size else None, rooms=int(rooms.group(1)) if rooms else None,
                           kind={"apartment": "appartement", "room": "kamer"}.get(kind, kind), agent=agent,
                           agent_site=site_of(url)))
    return out


def friendlyhousing():
    return agent_cards("Friendly Housing", "https://www.friendlyhousing.nl/nl/aanbod", r"/woningaanbod/[0-9a-f-]{20,}",
                       "Friendly Housing")


def lightcity():
    return agent_cards("Lightcity Housing", "https://lightcityhousing.nl/aanbod/", r"/aanbod/[a-z-]+/[a-z0-9-]+/[0-9a-f]{12,}",
                       "Lightcity Housing", base="https://lightcityhousing.nl/")


SITES = {
    "Pararius": pararius,
    "Funda": funda,
    "Huurwoningen": huurwoningen,
    "123Wonen": wonen123,
    "ikwilhuren": ikwilhuren,
    "Vesteda": vesteda,
    "HouseHunting": househunting,
    "Friendly Housing": friendlyhousing,
    "Lightcity": lightcity,
}


# ---------------------------------------------------------------- landlord contact

def detail_facts(l, s):
    """For sites whose results page has no type/size: read them from the listing page."""
    page = text(s.select_one("main") or s.body or s).lower()
    if l["size"] is None:
        m = re.search(r"(?:woonoppervlakte|oppervlakte|living area|surface)\D{0,20}(\d{2,3})\s*m", page) \
            or re.search(r"(\d{2,3})\s*m²", page)
        l["size"] = int(m.group(1)) if m else None
    if l["kind"] == "?":
        if re.search(r"\bstudio\b", page):
            l["kind"] = "studio"
        elif re.search(r"\b(kamer|room) (in|te huur in)\b|\bgedeelde\b|\bshared\b", page):
            l["kind"] = "kamer"
        elif re.search(r"\bappartement|apartment\b", page):
            l["kind"] = "appartement"
        else:
            l["kind"] = "woning"
        rooms = re.search(r"(?:aantal kamers|kamers|rooms)\D{0,10}(\d)", page)
        l["rooms"] = int(rooms.group(1)) if rooms else l["rooms"]
        if not self_contained(l):
            l["blocked"] = f"not a studio after reading the page ({l['kind']}, {l['size']} m²)"


SCAM_TEXT = (("western union", "asks for Western Union"), ("moneygram", "asks for MoneyGram"),
             ("sleutel per post", "keys by post"), ("keys by mail", "keys by post"), ("keys by post", "keys by post"),
             ("in het buitenland", "landlord abroad"), ("currently abroad", "landlord abroad"),
             ("living abroad", "landlord abroad"), ("airbnb", "via Airbnb"),
             ("whatsapp only", "WhatsApp only"), ("alleen via whatsapp", "WhatsApp only"),
             ("before the viewing", "pay before viewing?"), ("voor de bezichtiging betalen", "pay before viewing"))


def listing_facts(l, body):
    """Facts that decide whether it is worth reacting: income rule, service costs, furnishing, scam signs."""
    m = re.search(r"(\d(?:[.,]\d)?)\s*(?:x|keer|maal|times)\s*(?:de\s+|the\s+)?(?:kale\s+)?(?:bruto\s+|gross\s+)?(?:maand)?(?:huur|rent)", body) \
        or re.search(r"(?:inkomen|income)[^.]{0,60}?(\d(?:[.,]\d)?)\s*(?:x|keer|maal|times)", body)
    if m:
        mult = float(m.group(1).replace(",", "."))
        if 2 <= mult <= 5:
            l["income_mult"] = mult
    m = re.search(r"servicekosten[^0-9€]{0,30}€?\s*(\d{2,3})", body) or re.search(r"service (?:costs|charges)[^0-9€]{0,30}€?\s*(\d{2,3})", body)
    if m:
        l["service_costs"] = int(m.group(1))
    if re.search(r"\bgemeubileerd|\bfurnished\b", body) and not re.search(r"ongemeubileerd|unfurnished", body):
        l["interior"] = "furnished"
    elif re.search(r"gestoffeerd|upholstered|semi-furnished", body):
        l["interior"] = "upholstered"
    elif re.search(r"\bkaal\b|\bshell\b|unfurnished|ongemeubileerd", body):
        l["interior"] = "bare"
    # Hard flags (scam signs in the text) stop auto-apply; a low price alone is only a warning, because
    # established agents (Rotsvast, Huurinc) also list cheap older studios.
    l["flags"] = sorted({why for word, why in SCAM_TEXT if word in body})
    if l["price"] and l["size"] and l["price"] / l["size"] < SUSPICIOUS_PER_M2:
        l["warning"] = f"very cheap (€{l['price'] / l['size']:.0f}/m²): check it is a normal lease"


def income_check(l, profile):
    """True/False when both the rule and your income are known, else None."""
    p = profile or {}
    income = p.get("income_year", (p.get("income_gross_month") or 0) * 12) / 12  # incl. holiday allowance
    if not income:
        return None
    need = (l.get("income_mult") or 3) * l["price"]
    l["income_need"] = int(need)
    return income >= need


def enrich(l, cache):
    """Fill in the landlord/agent's own phone number and website. Only runs for new listings that fit."""
    try:
        s = soup(l["url"])
        if l["kind"] == "?" or l["size"] is None:
            detail_facts(l, s)
        # Only the listing's own description: the sites' menus mention "woningruil" on every page.
        desc = s.select("[class*=listing-detail-description], [class*=object-description], "
                        "[data-testid*=description], .description, #description")
        body = " ".join(text(d) for d in desc).lower() or text(s.select_one("main") or s.body or s).lower()[:20000]
        listing_facts(l, body)
        hit = next((w for w in NOT_RENTAL_TEXT if w in body), None)
        if hit:
            l["blocked"] = f"listing text says '{hit}'"
        if l["site"] == "Pararius":
            box = s.select_one(".contact-agent-block") or s
            name = box.select_one("a[href*='/makelaars/']")
            if name:
                l["agent"] = l["agent"] or text(name)
                agent_page = urllib.parse.urljoin("https://www.pararius.nl", name["href"])
                if agent_page not in cache:
                    links = [x["href"] for x in soup(agent_page).select("a[href^=http]")
                             if not any(p in x["href"] for p in PORTAL_DOMAINS)]
                    cache[agent_page] = site_of(links[0]) if links else ""
                l["agent_site"] = cache[agent_page] or l["agent_site"]
            nums = phones_in(str(box))
            if nums:
                l["phone"] = nums[0]
            if l["agent"]:  # remember per agent name, Funda shows the name but not the number
                cache["agent:" + l["agent"].lower()] = {"phone": l["phone"], "site": l["agent_site"]}
        elif l["site"] == "Funda" and l["agent"]:
            known = cache.get("agent:" + l["agent"].lower()) or {}
            l["phone"] = l["phone"] or known.get("phone", "")
            l["agent_site"] = l["agent_site"] or known.get("site", "")
        if not l["phone"] and l["agent_site"] and l["site"] not in ("Vesteda", "HouseHunting", "ikwilhuren (MVGM)"):
            key = "site:" + l["agent_site"]
            if key not in cache:
                nums = []
                for path in ("", "/contact", "/contact/"):
                    try:
                        nums = phones_in(get(f"https://{l['agent_site']}{path}", browser=False))
                    except Exception:
                        continue
                    if nums:
                        break
                cache[key] = nums[0] if nums else ""
            l["phone"] = cache[key]
    except Exception as e:
        print(f"  could not read {l['url']}: {e}")
        if l["price"] and l["size"] and l["price"] / l["size"] < SUSPICIOUS_PER_M2:
            l["blocked"] = "suspiciously cheap and the page could not be checked"
        if l["kind"] == "?":
            l["kind"] = "woning?"  # still send: better a maybe than a miss
    if not l["agent"] and l["agent_site"]:
        l["agent"] = l["agent_site"]
    return l


# ---------------------------------------------------------------- filtering

def town_of(l):
    hay = f" {l['place']} {urllib.parse.unquote(l['url'])} ".lower()
    if l["site"] in ("123Wonen", "ikwilhuren (MVGM)", "HouseHunting", "Friendly Housing", "Lightcity Housing"):
        hay += f" {l['title']} ".lower()
    return next((p for p in AREA if re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", hay)), "")


def self_contained(l):
    kind = l["kind"]
    if kind in ("studio", "?", "woning?"):
        return True
    if kind in ("appartement", "flat", "apartment", "bovenwoning", "benedenwoning"):
        return l["rooms"] == 1 or (l["size"] is not None and l["size"] <= SMALL_APARTMENT_M2)
    return False  # kamer, huis, woonruimte, parkeergelegenheid, ...


def why_not(l):
    if not l["url"]:
        return "no url"
    if any(n in (l["agent_site"] + l["agent"]).lower() for n in NOT_RENTALS):
        return "swap, not a rental"
    if not town_of(l):
        return "outside area"
    if l["price"] is None:
        return "no price"
    if l["price"] < MIN_PRICE:
        return f"price €{l['price']} looks wrong (parking, per week or misread)"
    if l["price"] > MAX_PRICE:
        return "too expensive"
    if not self_contained(l):
        return f"not a studio ({l['kind']}, {l['rooms']} rooms, {l['size']} m²)"
    return ""


def fits(l):
    return not why_not(l)


def street_key(l):
    """Same home on two sites -> same key: first street-like word + price."""
    words = re.findall(r"[a-z][a-z'-]{3,}", l["title"].lower())
    skip = {"kamer", "appartement", "studio", "woning", "huis", "woonruimte", "eengezinswoning",
            "bovenwoning", "benedenwoning", "flat", "maisonnette", "huren", "eindhoven", "galerij", "portiek"}
    street = next((w for w in words if w not in skip), "")
    return f"{street}|{l['price']}" if street and l["price"] else None


# ---------------------------------------------------------------- reaction text

def load_profile():
    try:
        p = json.loads(PROFILE_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not p.get("first_name") or "TODO" in json.dumps(p):
        return None
    return p


def street_of(l):
    return re.sub(r"^(studio|appartement|woning|flat|apartment)\s+", "", l["title"], flags=re.I)


def reactions(l, p):
    """Short reactions (Telegram copy buttons allow 256 characters) in Dutch and English."""
    import writer
    return writer.short(l, p), writer.short(l, p, en=True)


# ---------------------------------------------------------------- telegram

def esc(s):
    return html.escape(str(s), quote=False)


def telegram(method, payload):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"  (no Telegram config) {method}:\n{payload.get('text') or payload.get('caption')}\n")
        return True
    payload = {"chat_id": TELEGRAM_CHAT_ID, "parse_mode": "HTML", **payload}
    for attempt in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", json=payload, timeout=20)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
                continue
            print(f"  Telegram {method} error {r.status_code}: {r.text[:200]}")
            return False
        except Exception as e:
            print(f"  Telegram error: {e}")
            time.sleep(3)
    return False


def telegram_photo(path, caption, markup=None):
    """Send a local screenshot."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"  (no Telegram config) photo {path}: {caption}")
        return
    try:
        from curl_cffi import CurlMime
        mp = CurlMime()
        mp.addpart(name="chat_id", data=TELEGRAM_CHAT_ID)
        mp.addpart(name="caption", data=caption[:1000])
        mp.addpart(name="parse_mode", data="HTML")
        mp.addpart(name="photo", local_path=str(path), filename=Path(path).name, content_type="image/png")
        if markup:
            mp.addpart(name="reply_markup", data=json.dumps(markup))
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", multipart=mp, timeout=40)
        mp.close()
        if r.status_code != 200:
            raise RuntimeError(r.text[:200])
    except Exception as e:
        print(f"  Telegram photo error: {e}")
        telegram("sendMessage", {"text": caption, **({"reply_markup": markup} if markup else {})})


def telegram_text(t):
    telegram("sendMessage", {"text": t, "disable_web_page_preview": True})


def notify(l, profile):
    facts = [f"💶 €{l['price']:,}".replace(",", ".") + " p/m"]
    if l["size"]:
        facts.append(f"📐 {l['size']} m²")
        facts.append(f"€{l['price'] / l['size']:.0f}/m²")
    if l["phone"]:
        contact = f"📞 <b>{esc(pretty_phone(l['phone']))}</b>  {esc(l['agent'])}"
    elif l["agent"]:
        contact = f"✉️ {esc(l['agent'])} (no phone found, react via the listing)"
    else:
        contact = "✉️ react via the listing"
    if l.get("fresh") and l["phone"]:
        head = "🔥 <b>NEW, CALL NOW</b>\n"
    elif l.get("fresh"):
        head = "🔥 <b>NEW, react now</b>\n"
    else:
        head = f"🕒 <i>found late ({esc(l.get('age_note', 'site was blocked for a while'))}), may already be busy</i>\n"
    town = town_of(l).replace("-", " ").title()
    extra = []
    if l.get("interior"):
        extra.append({"furnished": "🛋 furnished", "upholstered": "🪟 upholstered (floors/curtains)", "bare": "🧱 bare (no floors)"}[l["interior"]])
    if l.get("service_costs"):
        extra.append(f"+ €{l['service_costs']} service costs")
    ok = income_check(l, profile)
    l["income_ok"] = ok
    if ok is not None:
        mark = "✅" if ok else ("🟡" if profile.get("guarantor") else "❌")
        tail = "" if ok else (" (above yours, mention your guarantor)" if profile.get("guarantor") else " (above yours)")
        extra.append(f"{mark} income {l.get('income_mult', 3):g}× rent = €{l['income_need']:,}".replace(",", ".") + tail)
    if l.get("flags"):
        extra.append("🚩 " + ", ".join(l["flags"]))
    if l.get("warning"):
        extra.append("⚠️ " + l["warning"])
    caption = (
        head
        + f"🏠 <b>{esc(l['title'])}</b>\n"
        + (f"📍 {esc(l['place'])}\n" if l["place"] and l["place"] != l["title"] else (f"📍 {town}\n" if town else ""))
        + "  ·  ".join(facts) + "\n"
        + ("  ·  ".join(esc(x) for x in extra) + "\n" if extra else "")
        + contact + "\n"
        + f"🔎 {esc(l['site'])}"
    )
    rows = [[{"text": "Open listing", "url": l["url"]}]]
    if l["agent_site"] and l["agent_site"] not in l["url"]:
        rows[0].append({"text": "Agent website", "url": f"https://{l['agent_site']}"})
    if profile:
        nl, en = reactions(l, profile)
        rows.append([{"text": "📋 Copy reaction NL", "copy_text": {"text": nl}},
                     {"text": "📋 Copy EN", "copy_text": {"text": en}}])
    from apply import app_id
    aid = app_id(l["url"])
    rows.append([{"text": "✅ Reacted", "callback_data": f"st:{aid}:reacted"},
                 {"text": "📅 Viewing", "callback_data": f"st:{aid}:viewing"},
                 {"text": "❌ No", "callback_data": f"st:{aid}:no"}])
    buttons = {"inline_keyboard": rows}
    if l["image"] and telegram("sendPhoto", {"photo": l["image"], "caption": caption, "reply_markup": buttons}):
        return
    telegram("sendMessage", {"text": caption, "reply_markup": buttons})


# ---------------------------------------------------------------- state

def load_state():
    try:
        s = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        s = {}
    for k, v in (("seen", []), ("keys", []), ("fails", {}), ("broken", []), ("contacts", {}),
                 ("last_ok", {}), ("prefer", {}), ("sites_known", []), ("keysite", {}),
                 ("cooldown", {}), ("last_try", {})):
        s.setdefault(k, v)
    return s


def save_state(s):
    # Keep the file small: newest 8000 is far more than all sites ever show at once.
    s["seen"] = s["seen"][-8000:]
    s.pop("keys", None)
    s["keysite"] = dict(list(s["keysite"].items())[-8000:])
    s["prefer"] = dict(_prefer)
    STATE_FILE.write_text(json.dumps(s, indent=0))


# ---------------------------------------------------------------- run

def check_all(state, dry_run=False):
    seen = set(state["seen"])
    first_run = not seen
    _prefer.update(state["prefer"])
    profile = load_profile()
    new = []
    now = time.time()

    for name, fn in SITES.items():
        wait_until = max(state["cooldown"].get(name, 0),
                         state["last_try"].get(name, 0) + SITE_EVERY.get(name, 0) * 60 - 20)
        if now < wait_until:
            if name in state["cooldown"] and now < state["cooldown"][name]:
                print(f"[{time.strftime('%H:%M:%S')}] {name}: resting after a block, next try {time.strftime('%H:%M', time.localtime(wait_until))}")
            continue
        state["last_try"][name] = now
        # A site added since the last run: learn what is online without alerting on all of it.
        learning = not first_run and name not in state["sites_known"]
        try:
            found = fn()
            error = None if found else "0 results"
        except Exception as e:
            found, error = [], f"{type(e).__name__}: {e}"
        ok = [l for l in found if fits(l)]
        print(f"[{time.strftime('%H:%M:%S')}] {name}: {len(found)} on page, {len(ok)} studios fit"
              + (f"  !! {error}" if error else "") + ("  (learning, no alerts)" if learning and not error else ""))

        # Health: tell me once when a site keeps failing, and once when it recovers.
        if error:
            state["fails"][name] = state["fails"].get(name, 0) + 1
            if "Blocked" in error or "bot check" in error or "403" in error:
                rest = min(5 * 2 ** (state["fails"][name] - 1), BACKOFF_MAX_MIN)
                state["cooldown"][name] = now + rest * 60
                print(f"  {name} blocked us; resting {rest} min")
            if state["fails"][name] >= BROKEN_AFTER and name not in state["broken"]:
                state["broken"].append(name)
                if not dry_run:
                    telegram("sendMessage", {"text": f"⚠️ <b>{esc(name)}</b> has failed {BROKEN_AFTER} checks in a row ({esc(error[:200])}). Blocked or the site changed; the finder needs a look."})
            continue
        state["fails"][name] = 0
        state["cooldown"].pop(name, None)
        if name in state["broken"]:
            state["broken"].remove(name)
            if not dry_run:
                telegram("sendMessage", {"text": f"✅ <b>{esc(name)}</b> works again."})
        if name not in state["sites_known"]:
            state["sites_known"].append(name)
        prev_ok = state["last_ok"].get(name)
        state["last_ok"][name] = now
        gap = (now - prev_ok) / 60 if prev_ok else None

        for l in ok:
            if l["url"] in seen:
                continue
            seen.add(l["url"])
            state["seen"].append(l["url"])
            # Same street + price on another site = same home (Pararius goes first, it has the number).
            # On the same site it is a neighbouring unit in the same building, so not a duplicate.
            k = street_key(l)
            if k and state["keysite"].get(k, l["site"]) != l["site"]:
                print(f"  dup  {l['title']} (already sent from {state['keysite'][k]})")
                continue
            if k:
                state["keysite"][k] = l["site"]
            if learning:
                continue
            l["fresh"] = gap is not None and gap <= FRESH_MINUTES
            if not l["fresh"]:
                l["age_note"] = f"site unreadable for {gap:.0f} min" if gap else "first check of this site"
            new.append(l)

    if first_run:
        # Nothing remembered yet (new setup or lost state): learn the current listings without
        # flooding the chat with everything that is already online.
        print(f"First run: remembered {len(new)} current studios, no alerts sent.")
        if not dry_run:
            telegram("sendMessage", {"text": f"🏠 Housing finder is live: self-contained studios in Eindhoven and around up to €{MAX_PRICE}, from {', '.join(SITES)}. {len(new)} already online were skipped; you get every new one from now on."})
        return state

    for l in new:
        enrich(l, state["contacts"])
        if l.get("blocked"):
            print(f"  skip {l['site']}: {l['title']} €{l['price']} ({l['blocked']})")
            continue
        print(f"  NEW  {'🔥' if l['fresh'] else '🕒'} {l['site']}: {l['title']} €{l['price']} {l['size']}m² | {l['agent']} {l['phone']}")
        if not dry_run:
            notify(l, profile)
            if l["fresh"]:  # only fresh ones: an hours-old listing is not worth an automatic reaction
                import apply
                result = apply.apply(l, profile, telegram_photo, telegram_text)
                print(f"       auto-apply: {result}")
            time.sleep(1)
    if not dry_run and not first_run:
        import apply
        apply.watch_logins(state, telegram_text, esc)
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check every site once and exit (for launchd)")
    ap.add_argument("--dry-run", action="store_true", help="print, do not send or save")
    ap.add_argument("--site", help="only run this site and print what it finds (with contact for fits)")
    ap.add_argument("--test-alert", action="store_true", help="send one sample alert to Telegram")
    ap.add_argument("--login", action="store_true", help="open the bot's Chrome to log in on Pararius/Funda once")
    ap.add_argument("--rehearse", metavar="URL", help="fill in the reaction form for this listing, screenshot, do not send")
    args = ap.parse_args()

    try:
        if args.login:
            PAUSE_FILE.write_text("login")
            try:
                import apply
                apply.login()
            finally:
                PAUSE_FILE.unlink(missing_ok=True)
            return

        if args.rehearse:
            import apply
            os.environ["AUTO_APPLY"] = "rehearse"
            apply.MODE = "rehearse"
            site = next((n for n in ("Pararius", "Funda", "123Wonen") if n.lower() in args.rehearse.lower()), "")
            l = listing(site, args.rehearse, "Woning " + args.rehearse.rstrip("/").split("/")[-1].replace("-", " ").title(), price=1000)
            prof = load_profile()
            if not prof:
                print("Fill in profile.json first (no TODO left).")
                return
            print(apply.apply(l, prof, telegram_photo, telegram_text))
            with apply.apps_locked() as apps:  # a rehearsal must not block a real reaction later
                apps.pop(apply.app_id(l["url"]), None)
            return

        if args.test_alert:
            l = listing("Test", "https://www.pararius.nl", "Studio Voorbeeldstraat 1", price=950, place="5611 AA Eindhoven",
                        size=30, rooms=1, agent="Example Makelaars", phone="+31401234567", agent_site="example.com")
            l["fresh"] = True
            notify(l, load_profile())
            return

        if args.site:
            _prefer.update(load_state()["prefer"])
            cache = {}
            for l in SITES[args.site]():
                reason = why_not(l)
                if not reason:
                    enrich(l, cache)
                    reason = l.get("blocked", "")
                print(("FIT  " if not reason else f"skip [{reason}] ")
                      + f"{l['title']} €{l['price']} {l['size']}m² {l['kind']} [{l['place']}] | {l['agent']} {l['phone']} {l['agent_site']}"
                      + (f" | income {l.get('income_mult')}x svc {l.get('service_costs')} {l.get('interior','')} flags {l.get('flags')}" if not reason else ""))
            return

        while True:
            if PAUSE_FILE.exists():
                if not args.once:
                    time.sleep(60)
                    continue
                return
            if args.once:
                st = load_state()
                every = run_every_minutes(time.localtime().tm_hour)
                if time.time() - st.get("last_run", 0) < every * 60 - 20:
                    return  # launchd starts us every minute; this hour we check less often
                time.sleep(random.uniform(0, 8))  # do not hit the sites at the exact same second every run
            state = check_all(load_state(), dry_run=args.dry_run)
            state["last_run"] = time.time()
            if not args.dry_run:
                save_state(state)
            if args.once:
                return
            wait = random.randint(150, 240)
            print(f"Next check in {wait // 60} min")
            close_browser()
            time.sleep(wait)
    finally:
        close_browser()


if __name__ == "__main__":
    sys.exit(main())
