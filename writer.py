"""Writes the reaction to a listing the way Jericho would write it himself, and learns which wording works.

How it writes (rules in ~/.claude-personal/skills/rental-reaction/SKILL.md):
  - one specific line about THIS home (street, size, interior, place), never a generic "this fits me"
  - who he is, income and guarantor, documents ready: what agents select on
  - a clear ask: a viewing, two concrete days, his phone number
  - simple Dutch (he is B1): short sentences, no polished stock phrases
  - only approved facts from profile.json; nothing invented

How it learns: each message is built from blocks (greeting, hook, about, money, cta, close) and every block
has a few wordings. Which wording was used is saved with the application. When you tap 📅 Viewing or ❌ No
in Telegram, that counts as a result. Next time, each block picks a wording by Thompson sampling: wordings
that got more viewings are picked more, new ones still get tried.

  python writer.py --sample            print a few sample messages
  python writer.py --stats             what has worked so far
"""
import datetime as dt
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPS_FILE = ROOT / "applications.json"

DAYS_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Phrases that make a message read as AI or template. compose() refuses to return a text containing one.
BANNED = [
    r"\bik schrijf u\b", r"met veel enthousiasme", r"\benthousiast\b", r"\bgepassioneerd\b", r"\bgedreven\b",
    r"\bideale huurder\b", r"perfecte match", r"\bperfect(e)? (kandidaat|huurder)\b", r"ik kijk (ernaar )?uit naar uw reactie",
    r"bij voorbaat dank", r"hopelijk tot snel", r"\bdroomwoning\b", r"\bunieke kans\b",
    r"i am writing", r"\bexcited\b", r"\bthrilled\b", r"\bpassionate\b", r"look forward to hearing",
    r"perfect fit", r"ideal tenant", r"dream home", r"\bseamless", r"\bdelve", r"—",
]


# ---------------------------------------------------------------- facts from the listing

def _street(l):
    return re.sub(r"^(studio|appartement|woning|flat|apartment|huis)\s+", "", l["title"], flags=re.I).strip()


def _at(street):
    """Dutch article for the street: 'aan het ...plein', otherwise 'aan de ...'."""
    return "het" if re.search(r"(plein|plantsoen|erf|veld|park)$", street.lower()) else "de"


def _eur(n, en=False):
    s = f"€{n:,}"
    return s if en else s.replace(",", ".")


def _kind(l, en=False):
    k = (l.get("kind") or "").strip("?")
    if en:
        return {"studio": "studio", "appartement": "apartment"}.get(k, "home")
    return {"studio": "studio", "appartement": "appartement"}.get(k, "woning")


def _town(l):
    hay = f"{l.get('place', '')} {l['url']}".lower()
    for t in ("veldhoven", "best", "geldrop", "nuenen", "waalre", "son en breugel", "son-en-breugel", "eindhoven"):
        if t in hay:
            return t.replace("-", " ").title().replace("En", "en")
    return ""


def _viewing_days(now):
    """The next two working days, starting tomorrow (today is often already planned)."""
    out, d = [], now.date()
    while len(out) < 2:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def _next_call_time(now):
    d = now + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.replace(hour=9, minute=45, second=0, microsecond=0)


# ---------------------------------------------------------------- blocks, each with a few wordings

def _greeting(l, now, en):
    h = now.hour
    if en:
        hi = "Good morning" if h < 12 else ("Good afternoon" if h < 18 else "Good evening")
        return {"time": f"{hi},", "hello": "Hello,"}
    hi = "Goedemorgen" if h < 12 else ("Goedemiddag" if h < 18 else "Goedenavond")
    return {"time": f"{hi},", "beste": "Beste heer/mevrouw,"}


def _hook(l, p, en):
    """The first line: about this home. Plus at most two true, specific reasons it suits him."""
    street, kind, town, size = _street(l), _kind(l, en), _town(l), l.get("size")
    place = f" in {town}" if town else ""
    reasons = []
    if en:
        if l.get("interior") == "bare":
            reasons.append("That it comes unfurnished is fine for me, I have my own furniture.")
        elif l.get("interior") == "furnished":
            reasons.append("That it comes furnished suits me well.")
        elif l.get("interior") == "upholstered":
            reasons.append("With floors and curtains already in, I could move in quickly.")
        if size:
            reasons.append(f"{size} m² for just me is plenty." if size >= 30 else f"{size} m² is fine for me, I live alone.")
        if town:
            reasons.append("It is close to my work at the High Tech Campus.")
        r = " ".join(reasons[:2])
        return {
            "saw": f"I just saw your {kind} at {street}{place} on {l['site']}, and I would like to come and see it. {r}".strip(),
            "direct": f"I am reacting to the {kind} at {street}{place} ({_eur(l['price'], True)} a month). {r}".strip(),
        }
    if l.get("interior") == "bare":
        reasons.append("Dat hij kaal wordt opgeleverd is voor mij geen probleem, ik heb mijn eigen meubels.")
    elif l.get("interior") == "furnished":
        reasons.append("Dat hij gemeubileerd is komt mij goed uit.")
    elif l.get("interior") == "upholstered":
        reasons.append("Met vloer en gordijnen erin kan ik er snel in.")
    if size:
        reasons.append(f"{size} m² voor mij alleen is ruim genoeg." if size >= 30 else f"{size} m² is prima, ik woon alleen.")
    if town:
        reasons.append("En het is dichtbij mijn werk op de High Tech Campus.")
    r = " ".join(reasons[:2])
    het = kind == "appartement"
    if het:
        r = r.replace("Dat hij", "Dat het")
    return {
        "saw": f"Ik zag net uw {kind} aan {_at(street)} {street}{place} op {l['site']} en ik wil {'het' if het else 'hem'} graag komen bekijken. {r}".strip(),
        "direct": f"Ik reageer op {'het' if het else 'de'} {kind} aan {_at(street)} {street}{place} ({_eur(l['price'])} per maand). {r}".strip(),
    }


def _about(l, p, en):
    name = f"{p['first_name']} {p['last_name']}"
    if en:
        return {
            "plain": f"A bit about me: I am {name}, 27. I work full-time as a developer at the High Tech Campus and I "
                     f"already live in Eindhoven. I am looking for a place for the long term, just for myself.",
            "short": f"I am {name}, 27, a developer at the High Tech Campus (full-time). I already live in Eindhoven "
                     f"and I want to stay here for a long time. It would be just me.",
        }
    return {
        "plain": f"Even over mij: ik ben {name}, 27 jaar. Ik werk fulltime als developer op de High Tech Campus en ik "
                 f"woon al in Eindhoven. Ik zoek een woning voor de lange termijn, alleen voor mezelf.",
        "short": f"Ik ben {name}, 27, developer op de High Tech Campus (fulltime). Ik woon al in Eindhoven en wil hier "
                 f"lang blijven wonen. Het is alleen voor mij.",
        "b1": f"Ik ben {name}, 27 jaar, en ik werk fulltime als developer op de High Tech Campus. Ik woon al in "
              f"Eindhoven en zoek iets voor lange tijd, alleen voor mij. Mijn Nederlands is B1, dus excuses voor een "
              f"kleine fout. Engels mag ook.",
    }


def _money(l, p, en):
    inc = f"€{p['income_gross_month']:,}".replace(",", ".")
    short = l.get("income_ok") is False
    mult = l.get("income_mult")
    if en:
        inc = inc.replace(".", ",")
        g = (f"I know you ask for {mult:g}x the rent as income. That is why my Dutch foster father will be my guarantor."
             if short and mult else "My Dutch foster father will be my guarantor, he also lives in Eindhoven."
             if short else "If you need one, my Dutch foster father can be my guarantor.")
        return {"std": f"I earn {inc} a month. {g} The employer statement and payslips are ready, I can send them right away."}
    g = (f"Ik weet dat u {mult:g}x de huur als inkomen vraagt. Daarom staat mijn Nederlandse pleegvader garant, hij woont ook in Eindhoven."
         if short and mult else "Mijn Nederlandse pleegvader staat garant, hij woont ook in Eindhoven."
         if short else "Als het nodig is, staat mijn Nederlandse pleegvader garant.")
    return {
        "std": f"Ik verdien {inc} per maand. {g} Werkgeversverklaring en loonstroken liggen klaar, die kan ik meteen sturen.",
        "docs_first": f"Werkgeversverklaring en loonstroken heb ik al klaar. Ik verdien {inc} per maand. {g}",
    }


def _cta(l, p, now, en):
    d1, d2 = _viewing_days(now)
    phone = p["phone"]
    if en:
        days = f"{DAYS_EN[d1.weekday()]} or {DAYS_EN[d2.weekday()]}"
        return {
            "days": f"Could I come for a viewing? {days} works for me, but I can adjust to your planning. "
                    f"You can call or WhatsApp me on {phone}.",
            "days_call": f"Could I come for a viewing? {days} works for me, but I can adjust to your planning. "
                         f"I will give you a short call tomorrow morning as well. My number is {phone}.",
        }
    days = f"{DAYS_NL[d1.weekday()]} of {DAYS_NL[d2.weekday()]}"
    return {
        "days": f"Kan ik komen kijken? {days.capitalize()} kan ik bijvoorbeeld, maar ik pas me graag aan uw planning aan. "
                f"U kunt mij bellen of appen op {phone}.",
        "days_call": f"Kan ik komen kijken? {days.capitalize()} kan ik bijvoorbeeld, maar ik pas me graag aan uw planning aan. "
                     f"Ik bel morgenochtend ook even. Mijn nummer is {phone}.",
    }


def _close(l, p, en):
    name = f"{p['first_name']} {p['last_name']}"
    if en:
        return {"kind": f"Kind regards,\n{name}", "best": f"Best,\n{name}"}
    return {"groet": f"Groeten,\n{name}", "mvg": f"Met vriendelijke groet,\n{name}"}


BLOCKS = ("greeting", "hook", "about", "money", "cta", "close")


# ---------------------------------------------------------------- learning

def _results():
    """{block: {wording: [viewings, sent]}} from applications.json."""
    try:
        apps = json.loads(APPS_FILE.read_text() or "{}")
    except (OSError, ValueError):
        apps = {}
    out = {}
    for a in apps.values():
        v = a.get("variant")
        if not v:
            continue
        won = any(h[1] == "viewing" for h in a.get("history", []))
        for block, wording in v.items():
            if block in BLOCKS:
                s = out.setdefault(block, {}).setdefault(wording, [0, 0])
                s[0] += won
                s[1] += 1
    return out


def _pick(block, options, results, rng):
    """Thompson sampling: draw a likely viewing rate per wording, take the highest draw."""
    seen = results.get(block, {})
    best, best_draw = None, -1
    for key in options:
        wins, sent = seen.get(key, [0, 0])
        draw = rng.betavariate(wins + 1, sent - wins + 1)
        if draw > best_draw:
            best, best_draw = key, draw
    return best


def compose(l, p, now=None, rng=None):
    """Returns (message, variant, extra). extra['call_due'] is set when the message promised a call."""
    now = now or dt.datetime.now()
    rng = rng or random.Random()
    en = p.get("lang", "nl") == "en" or "pararius.com" in l["url"]
    hook = _hook(l, p, en)
    if l.get("fresh") is False:
        hook.pop("saw")  # "I just saw" is not true for a listing that is hours or days old
    parts = {"greeting": _greeting(l, now, en), "hook": hook, "about": _about(l, p, en),
             "money": _money(l, p, en), "cta": _cta(l, p, now, en), "close": _close(l, p, en)}
    results = _results()
    variant = {b: _pick(b, parts[b], results, rng) for b in BLOCKS}
    variant["lang"] = "en" if en else "nl"
    # "docs_first" and "std" both carry the money facts; the paragraph order is fixed on purpose:
    # agents scan for this home -> who -> income -> when can I view.
    text = "\n\n".join(parts[b][variant[b]] for b in BLOCKS)
    text = re.sub(r"[ \t]+", " ", text).strip()
    hit = next((b for b in BANNED if re.search(b, text, re.I)), None)
    if hit:
        raise ValueError(f"writer produced a banned phrase ({hit}); fix writer.py")
    extra = {}
    if variant["cta"] == "days_call":
        extra["call_due"] = _next_call_time(now).timestamp()
    return text, variant, extra


def short(l, p, en=False):
    """≤256 characters for the Telegram copy buttons (you paste it yourself)."""
    street, kind = _street(l), _kind(l, en)
    name = f"{p['first_name']} {p['last_name']}"
    if en:
        t = (f"Hello, I would like to view the {kind} at {street}. I am {name}, 27, full-time developer at the "
             f"High Tech Campus, guarantor available, documents ready. When could I come by? {p['phone']}")
    else:
        t = (f"Goedendag, ik wil graag {'het' if kind == 'appartement' else 'de'} {kind} aan {_at(street)} {street} komen bekijken. Ik ben {name}, 27, fulltime developer "
             f"op de High Tech Campus, garantsteller aanwezig, documenten klaar. Wanneer kan ik langskomen? {p['phone']}")
    return t if len(t) <= 256 else t.replace(", documenten klaar", "").replace(", documents ready", "")[:256]


def stats_text(html=False):
    res = _results()
    if not res:
        return "No results yet: tap 📅 Viewing or ❌ No under each auto-applied listing so it can learn."
    lines = []
    for b in BLOCKS:
        if b not in res:
            continue
        cells = ", ".join(f"{k} {w}/{s}" for k, (w, s) in sorted(res[b].items(), key=lambda kv: -kv[1][1]))
        lines.append(f"{b}: {cells}")
    head = "Viewings per wording (viewings/sent):"
    return "\n".join([f"<b>{head}</b>" if html else head] + lines)


if __name__ == "__main__":
    if "--stats" in sys.argv:
        print(stats_text())
    else:
        prof = json.loads((ROOT / "profile.json").read_text())
        demo = [
            {"site": "Pararius", "url": "https://www.pararius.nl/studio-te-huur/eindhoven/x/hermelijnstraat",
             "title": "Studio Hermelijnstraat", "price": 925, "size": 28, "kind": "studio", "place": "5612 Eindhoven",
             "interior": "upholstered"},
            {"site": "Funda", "url": "https://www.funda.nl/huur/veldhoven/x", "title": "Appartement Kempenbaan",
             "price": 1240, "size": 38, "kind": "appartement", "place": "Veldhoven", "interior": "bare",
             "income_ok": False, "income_mult": 4},
        ]
        for i, l in enumerate(demo):
            msg, v, extra = compose(l, prof, rng=random.Random(i))
            print(f"--- {v} {extra}\n{msg}\n")
