"""Dutch housing finder: new self-contained studios around Eindhoven, straight to Telegram with the
landlord's own phone number.

Run once (launchd every 5 min):   python main.py --once
Run forever (terminal):           python main.py
Test without sending anything:    python main.py --once --dry-run
"""
import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests

# ---------------------------------------------------------------- settings

MAX_PRICE = int(os.getenv("MAX_PRICE", "1100"))

# A listing only counts if its title/address mentions one of these places
# (roughly 15-20 km around Eindhoven). Lower case.
AREA = [
    "eindhoven", "veldhoven", "best", "son en breugel", "son", "breugel", "nuenen",
    "geldrop", "mierlo", "waalre", "aalst", "valkenswaard", "heeze", "leende",
    "helmond", "oirschot", "sint-oedenrode", "boxtel", "deurne", "asten",
    "someren", "meerhout", "meerveldhoven", "wintelre", "knegsel", "steensel",
    "eersel", "vessem", "oerle", "zeelst", "gerwen", "nederwetten", "lieshout",
    "stiphout", "brandevoort", "dierdonk",
]

# Self-contained only: a studio, or an apartment that is a single room (a studio under
# another name) or at most this many m².
SMALL_APARTMENT_M2 = 40

ROOT = Path(__file__).resolve().parent

# Local secrets: .env next to this file with TELEGRAM_TOKEN=... and TELEGRAM_CHAT_ID=...
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() and not k.startswith("#") and v.strip():
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
STATE_FILE = Path(os.getenv("STATE_FILE", ROOT / "state.json"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# A site counts as broken after this many runs in a row with an error or zero results.
BROKEN_AFTER = 6

# Phone numbers that belong to a portal, never to a landlord.
PORTAL_PHONES = {"+31202402010"}
PORTAL_DOMAINS = ("pararius", "huurwoningen", "casco-media", "treehouse", "facebook", "immomiet",
                  "rentaroof", "toitpourtoi", "cbs.nl", "google", "cloudfront", "amazonaws",
                  # photo hosts of agent software, not the agent itself
                  "fastly", "realworks", "ogonline", "sharinpix", "windows.net", "kolibri", "tiara", "cdn", "imgix")

# Listings copied from these sites are not normal rentals (ruilwoning = swapping a social-housing
# home, you need one to swap).
NOT_RENTALS = ("ruilwoning",)


# ---------------------------------------------------------------- helpers

def get(url):
    """Fetch a page looking like real Chrome (TLS fingerprint included), which gets past Cloudflare on Pararius."""
    r = requests.get(url, impersonate="chrome", timeout=40,
                     headers={"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"})
    if r.status_code != 200 or "<title>Just a moment" in r.text[:3000]:
        raise RuntimeError(f"HTTP {r.status_code}{' (Cloudflare)' if 'Just a moment' in r.text[:3000] else ''}")
    return r.text


def soup(url):
    return BeautifulSoup(get(url), "html.parser")


def text(el):
    return el.get_text(" ", strip=True) if el else ""


def to_int(s):
    """'€ 1.235,- /mnd' -> 1235, '22 m²' -> 22. None when there is no number."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = re.sub(r"\.(?=\d{3}(?!\d))", "", str(s).replace("\xa0", " "))  # 1.235 -> 1235, keeps 498.2
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
        if n not in PORTAL_PHONES and n not in out:
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
        out.append(listing(
            site, urllib.parse.urljoin(base, a["href"]), text(a),
            price=to_int(text(it.select_one(".listing-search-item__price-main, .listing-search-item__price"))),
            place=text(it.select_one(".listing-search-item__sub-title")),
            size=to_int(text(it.select_one(".illustrated-features__item--surface-area"))),
            rooms=to_int(text(it.select_one(".illustrated-features__item--number-of-rooms"))),
            image=image, agent=text(agent), agent_site=origin,
        ))
    return out


def pararius():
    return pararius_style("Pararius", "https://www.pararius.nl",
                          f"https://www.pararius.nl/huurwoningen/eindhoven/0-{MAX_PRICE}/straal-15")


def huurwoningen():
    return pararius_style("Huurwoningen", "https://www.huurwoningen.nl",
                          f"https://www.huurwoningen.nl/in/eindhoven/?price=0-{MAX_PRICE}&radius=15&sort=published_at&direction=desc")


def wonen123():
    out = []
    for it in soup("https://www.123wonen.nl/huurwoningen/in/eindhoven/sort/newest").select(".pandlist-container"):
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
            image=img.get("data-src") if img else None, agent="123Wonen Eindhoven",
            agent_site="123wonen.nl",
        ))
    return out


def ikwilhuren():
    out = []
    for it in soup("https://ikwilhuren.nu/aanbod/eindhoven/").select(".card-woning"):
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
            place=text(it.select_one(".card-body > span:not(.card-title):not(.small)")),
            size=to_int(next((b for b in bits if re.search(r"\d\s*m\s*(2|²)", b)), None)),
            image=(img.get("src") or img.get("data-src")) if img else None,
            agent="MVGM (register on ikwilhuren.nu)", agent_site="ikwilhuren.nu",
        ))
    return out


SITES = {
    "Pararius": pararius,
    "Huurwoningen": huurwoningen,
    "123Wonen": wonen123,
    "ikwilhuren": ikwilhuren,
}


# ---------------------------------------------------------------- landlord contact

def enrich(l, cache):
    """Fill in the landlord/agent's own phone number and website. Only runs for new listings that fit."""
    try:
        if l["site"] == "Pararius":
            s = soup(l["url"])
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
        if not l["phone"] and l["agent_site"]:
            key = "site:" + l["agent_site"]
            if key not in cache:
                nums = []
                for path in ("", "/contact", "/contact/"):
                    try:
                        nums = phones_in(get(f"https://{l['agent_site']}{path}"))
                    except Exception:
                        continue
                    if nums:
                        break
                cache[key] = nums[0] if nums else ""
            l["phone"] = cache[key]
    except Exception as e:
        print(f"  could not get contact for {l['url']}: {e}")
    if not l["agent"] and l["agent_site"]:
        l["agent"] = l["agent_site"]
    return l


# ---------------------------------------------------------------- filtering

def in_area(l):
    hay = f" {l['title']} {l['place']} {l['url']} ".lower()
    return any(re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", hay) for p in AREA)


def self_contained(l):
    kind = l["kind"]
    if kind == "studio":
        return True
    if kind in ("appartement", "flat", "apartment"):
        return l["rooms"] == 1 or (l["size"] is not None and l["size"] <= SMALL_APARTMENT_M2)
    return False  # kamer, huis, woonruimte, ...


def why_not(l):
    if not l["url"]:
        return "no url"
    if any(n in (l["agent_site"] + l["agent"]).lower() for n in NOT_RENTALS):
        return "swap, not a rental"
    if not in_area(l):
        return "outside area"
    if l["price"] is None:
        return "no price"
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


def notify(l):
    facts = [f"💶 €{l['price']:,}".replace(",", ".") + " p/m"]
    if l["size"]:
        facts.append(f"📐 {l['size']} m²")
    if l["phone"]:
        contact = f"📞 <b>{esc(pretty_phone(l['phone']))}</b>  {esc(l['agent'])}"
    elif l["agent"]:
        contact = f"✉️ {esc(l['agent'])} (no phone found, react via the listing)"
    else:
        contact = "✉️ react via the listing"
    caption = (
        f"🏠 <b>{esc(l['title'])}</b>\n"
        + (f"📍 {esc(l['place'])}\n" if l["place"] and l["place"] != l["title"] else "")
        + "  ·  ".join(facts) + "\n"
        + contact + "\n"
        + f"🔎 {esc(l['site'])}"
    )
    row = [{"text": "Open listing", "url": l["url"]}]
    if l["agent_site"] and l["agent_site"] not in l["url"]:
        row.append({"text": "Agent website", "url": f"https://{l['agent_site']}"})
    buttons = {"inline_keyboard": [row]}
    if l["image"] and telegram("sendPhoto", {"photo": l["image"], "caption": caption, "reply_markup": buttons}):
        return
    telegram("sendMessage", {"text": caption, "reply_markup": buttons})


# ---------------------------------------------------------------- state

def load_state():
    try:
        s = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        s = {}
    for k, v in (("seen", []), ("keys", []), ("fails", {}), ("broken", []), ("contacts", {})):
        s.setdefault(k, v)
    return s


def save_state(s):
    # Keep the file small: newest 5000 is far more than all sites ever show at once.
    s["seen"] = s["seen"][-5000:]
    s["keys"] = s["keys"][-5000:]
    STATE_FILE.write_text(json.dumps(s, indent=0))


# ---------------------------------------------------------------- run

def check_all(state, dry_run=False):
    seen, keys = set(state["seen"]), set(state["keys"])
    first_run = not seen
    new = []

    for name, fn in SITES.items():
        try:
            found = fn()
            error = None if found else "0 results"
        except Exception as e:
            found, error = [], f"{type(e).__name__}: {e}"
        ok = [l for l in found if fits(l)]
        print(f"[{time.strftime('%H:%M:%S')}] {name}: {len(found)} on page, {len(ok)} studios fit" + (f"  !! {error}" if error else ""))

        # Health: tell me once when a site keeps failing, and once when it recovers.
        if error:
            state["fails"][name] = state["fails"].get(name, 0) + 1
            if state["fails"][name] >= BROKEN_AFTER and name not in state["broken"]:
                state["broken"].append(name)
                if not dry_run:
                    telegram("sendMessage", {"text": f"⚠️ <b>{esc(name)}</b> has failed {BROKEN_AFTER} checks in a row ({esc(error)}). The site probably changed; the finder needs a fix."})
        else:
            state["fails"][name] = 0
            if name in state["broken"]:
                state["broken"].remove(name)
                if not dry_run:
                    telegram("sendMessage", {"text": f"✅ <b>{esc(name)}</b> works again."})

        for l in ok:
            if l["url"] in seen:
                continue
            seen.add(l["url"])
            state["seen"].append(l["url"])
            k = street_key(l)
            if k and k in keys:
                print(f"  dup  {l['title']} (already sent from another site)")
                continue
            if k:
                keys.add(k)
                state["keys"].append(k)
            new.append(l)

    if first_run:
        # Nothing remembered yet (new setup or lost state): learn the current listings without
        # flooding the chat with everything that is already online.
        print(f"First run: remembered {len(new)} current studios, no alerts sent.")
        if new and not dry_run:
            telegram("sendMessage", {"text": f"🏠 Housing finder is live: self-contained studios around Eindhoven up to €{MAX_PRICE}, from {', '.join(SITES)}. {len(new)} already online were skipped; you get every new one from now on, with the landlord's phone number."})
        return state

    for l in new:
        enrich(l, state["contacts"])
        print(f"  NEW  {l['site']}: {l['title']} €{l['price']} | {l['agent']} {l['phone']}")
        if not dry_run:
            notify(l)
            time.sleep(1)
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check every site once and exit (for cron)")
    ap.add_argument("--dry-run", action="store_true", help="print, do not send or save")
    ap.add_argument("--site", help="only run this site and print what it finds (with contact for fits)")
    args = ap.parse_args()

    if args.site:
        cache = {}
        for l in SITES[args.site]():
            reason = why_not(l)
            if not reason:
                enrich(l, cache)
            print(("FIT  " if not reason else f"skip [{reason}] ") + f"{l['title']} €{l['price']} {l['size']}m² | {l['agent']} {l['phone']} {l['agent_site']}")
        return

    while True:
        state = check_all(load_state(), dry_run=args.dry_run)
        if not args.dry_run:
            save_state(state)
        if args.once:
            return
        wait = random.randint(300, 600)
        print(f"Next check in {wait // 60} min")
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
