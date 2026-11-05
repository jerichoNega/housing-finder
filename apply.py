"""Auto-apply: react on a listing in your name, in your own logged-in Chrome profile.

Modes (AUTO_APPLY in .env):
  off       never react automatically
  rehearse  fill in the form, screenshot it to Telegram, do NOT send (default, to check it once)
  live      fill in and send, then screenshot the confirmation to Telegram

Rules that always hold, in every mode:
  - never twice on the same listing (applications.json)
  - at most DAILY_CAP reactions per day
  - never when the listing has a scam flag or your income is below the stated requirement
  - never solves a CAPTCHA or pays anything: if a site asks for that, it stops and tells you
  - a page that wants a login stops it and tells you to run `python main.py --login`

Sites: Pararius and Funda (need a free account, log in once with --login).
Others are alert-only: 123Wonen and HouseHunting (visible CAPTCHA), Huurwoningen (paid account),
Vesteda/ikwilhuren (own application flow per account).
"""
import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPS_FILE = ROOT / "applications.json"
SHOTS = ROOT / "logs" / "screens"
DAILY_CAP = int(os.getenv("DAILY_CAP", "20"))
MODE = os.getenv("AUTO_APPLY", "rehearse").strip().lower()
APPLY_SITES = ("Pararius", "Funda")

LOGIN_URLS = {
    "Pararius": "https://www.pararius.nl/inloggen",
    "Funda": "https://login.funda.nl/account/login",
}


# ---------------------------------------------------------------- shared application log

@contextmanager
def apps_locked():
    """applications.json is shared with the Telegram bot process, so lock it while changing it."""
    APPS_FILE.touch(exist_ok=True)
    with open(APPS_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = json.loads(f.read() or "{}")
        except ValueError:
            data = {}
        yield data
        f.seek(0)
        f.truncate()
        f.write(json.dumps(data, indent=1, ensure_ascii=False))
        fcntl.flock(f, fcntl.LOCK_UN)


def app_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def record(l, status, note=""):
    with apps_locked() as apps:
        a = apps.setdefault(app_id(l["url"]), {
            "url": l["url"], "title": l["title"], "price": l["price"], "site": l["site"],
            "phone": l.get("phone", ""), "agent": l.get("agent", ""), "created": time.time(), "history": []})
        a["status"] = status
        a["updated"] = time.time()
        a["history"].append([time.strftime("%Y-%m-%d %H:%M"), status, note])


def already(url):
    with apps_locked() as apps:
        a = apps.get(app_id(url))
        return bool(a and a.get("status") not in ("failed", "rehearsed", "needs_login"))


def sent_today():
    day = time.strftime("%Y-%m-%d")
    with apps_locked() as apps:
        return sum(1 for a in apps.values()
                   if any(h[0].startswith(day) and h[1] == "applied" for h in a.get("history", [])))


# ---------------------------------------------------------------- the message

def message(l, p):
    """The longer reaction for a form (no 256-character limit there)."""
    street = re.sub(r"^(studio|appartement|woning|flat|apartment)\s+", "", l["title"], flags=re.I)
    if p.get("lang", "nl") == "en":
        return (f"Dear Sir/Madam,\n\nI am very interested in the home at {street} (€{l['price']} per month) "
                f"and would love to come for a viewing.\n\n{p['pitch_en']}\n\n"
                f"I can move in {p['move_in_en']}. My documents (ID, employer statement, payslips) are "
                f"ready and I can send them right away.\n\nYou can reach me on {p['phone']}.\n\n"
                f"Kind regards,\n{p['first_name']} {p['last_name']}")
    return (f"Beste heer/mevrouw,\n\nIk heb grote interesse in de woning aan de {street} (€{l['price']} per maand) "
            f"en kom graag bezichtigen.\n\n{p['pitch_nl']}\n\n"
            f"Ik kan {p['move_in']} huren. Mijn documenten (ID, werkgeversverklaring, loonstroken) liggen "
            f"klaar en kan ik direct sturen.\n\nU kunt mij bereiken op {p['phone']}.\n\n"
            f"Met vriendelijke groet,\n{p['first_name']} {p['last_name']}")


# Form field label/name/placeholder -> profile value
FIELD_MAP = [
    (r"voorletter|initial", "initials"),
    (r"tussenvoegsel|infix|prefix", "_empty"),
    (r"achternaam|last.?name|surname|lastname", "last_name"),
    (r"voornaam|first.?name|firstname|given", "first_name"),
    (r"^(naam|name|full.?name|uw naam|your name)$|c_name|\bnaam\b", "full_name"),
    (r"e-?mail", "email"),
    (r"telefoon|phone|mobiel|mobile|tel\b", "phone_digits"),
    (r"huisnummer|house.?number|address_number|nummer", "house_number"),
    (r"postcode|zip|postal", "postcode"),
    (r"woonplaats|plaats|city|town|stad", "city"),
    (r"straat|street|adres|address", "street"),
    (r"geboorte|birth", "birth_date"),
    (r"inkomen|income|salaris|salary", "income"),
    (r"beroep|occupation|functie|job|werkzaam", "job"),
    (r"werkgever|employer", "employer"),
    (r"personen|persons|household|aantal", "household"),
    (r"ingangsdatum|move.?in|start.?date|per wanneer|beschikbaar", "move_in"),
]


def value_for(key, l, p):
    full = f"{p['first_name']} {p['last_name']}"
    return {
        "_empty": "", "full_name": full, "phone_digits": re.sub(r"[^\d+]", "", p["phone"]),
        "income": str(p.get("income_gross_month", "")), "household": str(p.get("household", 1)),
    }.get(key, str(p.get(key, "")))


# ---------------------------------------------------------------- browser

def _ctx():
    """The finder's own Chrome (one profile can only be open once), whether main.py runs as script or module."""
    import sys
    m = sys.modules.get("__main__")
    if not hasattr(m, "browser_context"):
        import main as m
    return m.browser_context()


def _label(page, el):
    return page.evaluate("""el => {
        const bits = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.getAttribute('autocomplete')];
        if (el.id) { const lab = document.querySelector('label[for="' + el.id + '"]'); if (lab) bits.push(lab.innerText); }
        const wrap = el.closest('label'); if (wrap) bits.push(wrap.innerText);
        return bits.filter(Boolean).join(' ').toLowerCase();
    }""", el)


def _fill_form(page, l, p, msg):
    """Fill every visible field we understand. Returns (filled, unknown_required)."""
    filled, unknown = [], []
    for el in page.query_selector_all("form input, form textarea, form select"):
        if not el.is_visible():
            continue
        typ = (el.get_attribute("type") or el.evaluate("e => e.tagName").lower()).lower()
        if typ in ("hidden", "submit", "button", "file", "password", "search", "radio"):
            continue
        lab = _label(page, el)
        if typ == "checkbox":
            if re.search(r"privacy|voorwaarden|akkoord|agree|terms|toestemming|consent|gelezen", lab) \
                    and not re.search(r"nieuwsbrief|newsletter|marketing|aanbiedingen", lab):
                el.check()
                filled.append("consent")
            continue
        if typ == "textarea":
            el.fill(msg)
            filled.append("message")
            continue
        if el.input_value():
            continue  # prefilled by the account
        key = next((k for rx, k in FIELD_MAP if re.search(rx, lab)), None)
        if key and typ != "select":
            v = value_for(key, l, p)
            if v:
                el.fill(v)
                filled.append(key)
        elif el.get_attribute("required") is not None:
            unknown.append(lab[:40] or typ)
    # A radio choice with nothing picked (e.g. Funda "Welke makelaar wil je spreken?"): take the first option.
    # Sites often draw these as role=radio buttons with hidden inputs behind them, so click what a human sees.
    for grp in page.query_selector_all("form [role=radiogroup]"):
        opts = [o for o in grp.query_selector_all("[role=radio]") if o.is_visible()]
        if opts and not any(o.get_attribute("aria-checked") == "true" for o in opts):
            opts[0].click()
            filled.append("choice")
    groups = {}
    for el in page.query_selector_all("form input[type=radio]:not([aria-hidden=true])"):
        groups.setdefault(el.get_attribute("name") or "", []).append(el)
    for name, els in groups.items():
        if not any(e.is_checked() for e in els):
            try:
                els[0].check(force=True)
                filled.append(f"choice:{name or 'radio'}")
            except Exception:
                unknown.append(f"choice {name}")
    return filled, unknown


def _income_band(page, sel, income):
    """Pick the '€3000 - €3500 per maand' style option that contains your monthly income."""
    for v in page.eval_on_selector(sel, "e => [...e.options].map(o => o.value)"):
        m = re.match(r"\[(\d+),(\d*)\]", v)
        if m and int(m.group(1)) <= income < int(m.group(2) or 10**9):
            return v


def _site_fields(page, l, p):
    """Fields the site prefills from your account (old name, old phone) or keeps in its own lists:
    set them to your profile every time."""
    if l["site"] != "Pararius":
        return []
    f = "contact_agent_huurprofiel_form"
    done = []
    def put(name, value, select=False):
        el = page.query_selector(f"[name='{f}[{name}]']")
        if el and value not in (None, ""):
            el.select_option(value) if select else el.fill(value)
            done.append(name)
    put("first_name", p["first_name"])
    put("last_name", p["last_name"])
    put("phone_number", "+31" + re.sub(r"\D", "", p["phone"]).lstrip("0"))
    put("date_of_birth", "-".join(reversed(p["birth_date"].split("-"))))
    put("work_situation", "1", True)
    band = page.query_selector(f"[name='{f}[gross_annual_household_income]']") and \
        _income_band(page, f"[name='{f}[gross_annual_household_income]']", p.get("income_gross_month", 0))
    put("gross_annual_household_income", band, True)
    put("guarantor", "2" if p.get("guarantor") else "1", True)
    put("preferred_living_situation", "1" if p.get("household", 1) == 1 else "2", True)
    if "pets" in p:
        put("pets", "1" if p["pets"] else "0", True)
    put("rent_start_date", time.strftime("%Y-%m-%d"))
    put("preferred_contract_period", "1", True)  # onbepaalde tijd: long term
    return done


def _captcha_challenge(page):
    """A visible CAPTCHA ("I'm not a robot" box or puzzle). Invisible scoring is fine; anything a human must
    click or solve is left to the human."""
    for fr in page.frames:
        if re.search(r"recaptcha/(api2|enterprise)/(bframe|anchor)|hcaptcha.com/captcha|challenges.cloudflare", fr.url):
            try:
                if fr.frame_element().is_visible():
                    return True
            except Exception:
                pass
    return False


def _shot(page, l, tag):
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{app_id(l['url'])}-{tag}.png"
    form = page.query_selector("form:has(textarea)") if tag != "sent" else None
    try:
        if form and form.is_visible():
            form.scroll_into_view_if_needed()
            form.screenshot(path=str(path))
            return path
    except Exception:
        pass
    page.screenshot(path=str(path), full_page=False)
    return path


def _needs_login(page):
    return bool(re.search(r"inloggen|/login|account/login", page.url)) or page.query_selector("input[type=password]:visible")


def _auto_login(page, site):
    """Log back in with the e-mail/password you put in .env (SITE_EMAIL / SITE_PASSWORD). Pararius keeps its
    login only for one browser session, so this is what keeps auto-apply working there."""
    email, pw = os.getenv(f"{site.upper()}_EMAIL"), os.getenv(f"{site.upper()}_PASSWORD")
    if not (email and pw):
        return False
    if not page.query_selector("input[type=password]:visible"):
        page.goto(LOGIN_URLS[site], wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    page.fill("input[type=email]:visible, input[name*=mail i]:visible, input[name=username]:visible", email)
    page.fill("input[type=password]:visible", pw)
    page.click("form:has(input[type=password]) button[type=submit], button:has-text('Inloggen'), button:has-text('Log in')")
    page.wait_for_timeout(5000)
    return not _needs_login(page)


def _open_form(page, l):
    page.goto(l["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    for txt in ("Alles accepteren", "Accepteren", "Accept all", "Akkoord", "Ik begrijp het"):
        b = page.query_selector(f"button:has-text('{txt}')")
        if b and b.is_visible():
            try:
                b.click(timeout=2000)
            except Exception:
                pass
    if l["site"] == "Pararius":
        a = page.query_selector("a[href^='/contact/']:not([href*='meld-een-probleem'])")
        if not a:
            raise RuntimeError("no contact link on the listing (rented already?)")
        page.goto("https://www.pararius.nl" + a.get_attribute("href"), wait_until="domcontentloaded")
    elif l["site"] == "Funda":
        b = page.query_selector("a:has-text('Neem contact op'), button:has-text('Neem contact op'), "
                                "a:has-text('Reageer'), button:has-text('Reageer'), a:has-text('Contact makelaar'), "
                                "button:has-text('Contact makelaar'), a:has-text('Stel een vraag')")
        if not b:
            raise RuntimeError("no contact button on the Funda listing")
        b.click()
    elif l["site"] == "123Wonen":
        pass  # the form is on the listing page itself
    page.wait_for_timeout(2500)


def _submit(page):
    b = page.query_selector("form button[type=submit]:visible, form input[type=submit]:visible") or \
        page.query_selector("button:has-text('Verstuur'), button:has-text('Verzenden'), button:has-text('Versturen'), "
                            "button:has-text('Reageer'), button:has-text('Send'), button:has-text('Verstuur bericht')")
    if not b:
        raise RuntimeError("no send button found")
    b.click()
    page.wait_for_timeout(5000)
    body = page.inner_text("body").lower()
    if re.search(r"bedankt|verzonden|verstuurd|ontvangen|thank you|has been sent|is verstuurd|succes", body):
        return True
    if re.search(r"verplicht|required|ongeldig|invalid|fout|error", body):
        raise RuntimeError("the site reports a form error after sending")
    return None  # sent, but no clear confirmation text


def apply(l, profile, telegram_photo, telegram_text):
    """React on one listing. Returns a short status for the log."""
    if MODE == "off" or l["site"] not in APPLY_SITES:
        return "not auto-applied (site or mode)"
    if not profile:
        return "not auto-applied (profile.json not filled in)"
    if l.get("flags"):
        return f"not auto-applied (flags: {', '.join(l['flags'])})"
    if l.get("income_ok") is False and not profile.get("guarantor"):
        return "not auto-applied (income below the stated requirement)"
    if already(l["url"]):
        return "already applied"
    if MODE == "live" and sent_today() >= DAILY_CAP:
        return f"not auto-applied (daily cap {DAILY_CAP} reached)"

    msg = message(l, profile)
    page = _ctx().new_page()
    try:
        _open_form(page, l)
        if _needs_login(page) and _auto_login(page, l["site"]):
            _open_form(page, l)
        if _needs_login(page):
            record(l, "needs_login", l["site"])
            telegram_text(f"🔑 Auto-apply needs you logged in on <b>{l['site']}</b>. Run "
                          f"<code>.venv/bin/python main.py --login</code> on the Mac, or put {l['site'].upper()}_EMAIL and "
                          f"{l['site'].upper()}_PASSWORD in .env so it logs in by itself. React by hand for now: {l['url']}")
            return "needs login"
        filled, unknown = _fill_form(page, l, profile, msg)
        filled += _site_fields(page, l, profile)
        if "message" not in filled and not filled:
            raise RuntimeError("no form fields found")
        if unknown:
            raise RuntimeError(f"required fields it does not know: {', '.join(unknown)}")
        if _captcha_challenge(page):
            raise RuntimeError("the site shows a CAPTCHA")
        if MODE != "live":
            path = _shot(page, l, "rehearsal")
            record(l, "rehearsed", ", ".join(filled))
            telegram_photo(path, f"🧪 <b>Rehearsal, nothing sent</b>: {l['site']} form filled ({', '.join(filled)}). "
                                 f"If this looks right, set <code>AUTO_APPLY=live</code> in .env.")
            return "rehearsed"
        ok = _submit(page)
        if _captcha_challenge(page):
            raise RuntimeError("the site showed a CAPTCHA after sending")
        path = _shot(page, l, "sent")
        record(l, "applied", "confirmed" if ok else "sent, no confirmation text")
        telegram_photo(path, f"✅ <b>Applied</b> on {l['site']}: {l['title']} €{l['price']}"
                             + ("" if ok else " (sent, but the site showed no clear confirmation; check your mail)"))
        return "applied"
    except Exception as e:
        try:
            path = _shot(page, l, "failed")
        except Exception:
            path = None
        record(l, "failed", str(e)[:200])
        text = f"⚠️ Auto-apply failed on {l['site']} ({str(e)[:150]}). React by hand: {l['url']}"
        telegram_photo(path, text) if path else telegram_text(text)
        return f"failed: {e}"
    finally:
        page.close()


def login():
    """Open the bot's Chrome visibly so you can log in once on each site. Cookies stay in .browser-profile."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(ROOT / ".browser-profile"), channel="chrome", headless=False, locale="nl-NL",
            args=["--disable-blink-features=AutomationControlled"], ignore_default_args=["--enable-automation"])
        for i, (site, url) in enumerate(LOGIN_URLS.items()):  # reuse the blank start tab, so closing the tabs ends it
            (ctx.pages[0] if i == 0 and ctx.pages else ctx.new_page()).goto(url)
        print("Log in on every tab (Google login is fine).")
        print("Close the Chrome window when done.")
        import main as m
        m.restore_session_cookies(ctx)
        while ctx.pages:
            m.save_session_cookies(ctx)  # every second, so a Cmd+Q still keeps your login
            time.sleep(1)
        ctx.close()
