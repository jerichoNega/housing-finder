"""Telegram side of the housing finder: answers buttons and commands, reminds you to follow up.

Runs all the time (launchd KeepAlive). Commands in the chat:
  /status   sites, last check, today's numbers, auto-apply mode
  /apps     your open reactions and viewings
  /pause    stop checking (e.g. while you are away)   /resume   start again
Buttons under each alert (Reacted / Viewing / No) update applications.json.
"""
import json
import time

from curl_cffi import requests

import main  # first: it loads .env, which apply reads AUTO_APPLY from
import apply

API = f"https://api.telegram.org/bot{main.TELEGRAM_TOKEN}"
REMIND_AFTER_H = 48
LABEL = {"reacted": "✅ reacted", "applied": "🤖 auto-applied", "viewing": "📅 viewing", "no": "❌ no",
         "rehearsed": "🧪 rehearsed", "failed": "⚠️ failed", "needs_login": "🔑 needs login"}


def call(method, **payload):
    try:
        r = requests.post(f"{API}/{method}", json=payload, timeout=70)
        return r.json()
    except Exception as e:
        print(f"Telegram {method}: {e}")
        return {}


def say(text):
    call("sendMessage", chat_id=main.TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)


def status_text():
    st = main.load_state()
    now = time.time()
    lines = ["⏸ <b>Paused</b> (/resume to start)" if main.PAUSE_FILE.exists() else "▶️ <b>Running</b>"]
    last = st.get("last_run")
    lines.append(f"Last check: {time.strftime('%H:%M', time.localtime(last)) if last else 'never'}, "
                 f"now every {main.run_every_minutes(time.localtime().tm_hour)} min")
    for name in main.SITES:
        ok = st["last_ok"].get(name)
        cool = st["cooldown"].get(name, 0)
        if cool > now:
            s = f"resting until {time.strftime('%H:%M', time.localtime(cool))}"
        elif st["fails"].get(name):
            s = f"⚠️ {st['fails'][name]} failed checks"
        else:
            s = f"ok, {int((now - ok) / 60)} min ago" if ok else "not read yet"
        lines.append(f"• {name}: {s}")
    with apply.apps_locked() as apps:
        day = time.strftime("%Y-%m-%d")
        today = [a for a in apps.values() if any(h[0].startswith(day) for h in a["history"])]
        counts = {}
        for a in apps.values():
            counts[a["status"]] = counts.get(a["status"], 0) + 1
    lines.append(f"\nAuto-apply: <b>{apply.MODE}</b>, {apply.sent_today()}/{apply.DAILY_CAP} sent today")
    for site, w in st.get("login", {}).items():
        when = time.strftime("%H:%M", time.localtime(w["checked"])) if w.get("checked") else "never"
        state = {True: "🔓 logged in", False: "🔑 LOGGED OUT, run main.py --login"}.get(w.get("ok"), "unknown")
        lines.append(f"• {site}: {state} (checked {when})")
    lines.append("Tracked: " + (", ".join(f"{LABEL.get(k, k)} {v}" for k, v in counts.items()) or "nothing yet")
                 + f" ({len(today)} touched today)")
    return "\n".join(lines)


def apps_text():
    with apply.apps_locked() as apps:
        open_ = sorted((a for a in apps.values() if a["status"] in ("reacted", "applied", "viewing")),
                       key=lambda a: -a["updated"])
    if not open_:
        return "No open reactions."
    out = []
    for a in open_[:25]:
        when = time.strftime("%d-%m %H:%M", time.localtime(a["updated"]))
        phone = f" · 📞 {main.pretty_phone(a['phone'])}" if a.get("phone") else ""
        out.append(f"{LABEL.get(a['status'], a['status'])} {when} · <a href=\"{a['url']}\">{main.esc(a['title'])}</a> €{a['price']}{phone}")
    return "\n".join(out)


def on_callback(cb):
    data = cb.get("data", "")
    if data.startswith("st:"):
        _, aid, status = data.split(":", 2)
        msg = cb.get("message", {})
        title = (msg.get("caption") or msg.get("text") or "").split("\n")
        title = next((t for t in title if t.startswith("🏠")), "listing").replace("🏠", "").strip()
        url = next((b["url"] for row in msg.get("reply_markup", {}).get("inline_keyboard", [])
                    for b in row if b.get("url") and "Open listing" in b.get("text", "")), "")
        with apply.apps_locked() as apps:
            a = apps.setdefault(aid, {"url": url, "title": title, "price": None, "site": "", "phone": "",
                                      "agent": "", "created": time.time(), "history": []})
            a["status"] = status
            a["updated"] = time.time()
            a.pop("reminded", None)
            a["history"].append([time.strftime("%Y-%m-%d %H:%M"), status, "button"])
        call("answerCallbackQuery", callback_query_id=cb["id"], text=f"Saved: {LABEL.get(status, status)}")


def on_message(m):
    if str(m.get("chat", {}).get("id")) != str(main.TELEGRAM_CHAT_ID):
        return  # only you
    t = (m.get("text") or "").strip().lower()
    if t.startswith("/status"):
        say(status_text())
    elif t.startswith("/apps"):
        say(apps_text())
    elif t.startswith("/pause"):
        main.PAUSE_FILE.write_text("telegram")
        say("⏸ Paused. /resume to start again.")
    elif t.startswith("/resume"):
        main.PAUSE_FILE.unlink(missing_ok=True)
        say("▶️ Running again.")
    elif t.startswith("/learn"):
        import writer
        say(writer.stats_text(html=True))
    elif t.startswith("/start") or t.startswith("/help"):
        say("/status · /apps · /learn · /pause · /resume")


def reminders():
    """One nudge per reaction that got no answer within 48 hours: call them."""
    now = time.time()
    due = []
    with apply.apps_locked() as apps:
        for a in apps.values():
            if a["status"] in ("reacted", "applied") and not a.get("reminded") \
                    and now - a["updated"] > REMIND_AFTER_H * 3600:
                a["reminded"] = now
                due.append(a)
    calls = []
    with apply.apps_locked() as apps:
        for a in apps.values():
            if a.get("call_due") and now >= a["call_due"] and not a.get("call_reminded") and a["status"] == "applied":
                a["call_reminded"] = now
                calls.append(a)
    for a in calls:
        who = f"{main.esc(a.get('agent') or 'the agent')} on <b>{main.pretty_phone(a['phone'])}</b>" if a.get("phone") else "the agent (no number found, send a short follow-up via the listing)"
        say(f"📞 Your message to <a href=\"{a['url']}\">{main.esc(a['title'])}</a> said you would call this morning. "
            f"Call {who} now. Say: <i>\"Goedemorgen, u spreekt met Jericho Degu. Ik heb gisteren gereageerd op "
            f"de woning aan de {main.esc(main.street_of(a))}. Ik wilde even horen of ik kan komen kijken.\"</i>")
    for a in due:
        phone = f"Call {main.pretty_phone(a['phone'])} ({main.esc(a.get('agent', ''))})" if a.get("phone") else "Send a short follow-up via the listing"
        say(f"⏰ No answer in 48 h on <a href=\"{a['url']}\">{main.esc(a['title'])}</a>. {phone}: ask if it is still available and if you can view it.")


def run():
    call("setMyCommands", commands=[{"command": "status", "description": "sites and today's numbers"},
                                    {"command": "apps", "description": "open reactions and viewings"},
                                    {"command": "learn", "description": "which message wording gets viewings"},
                                    {"command": "pause", "description": "stop checking"},
                                    {"command": "resume", "description": "start checking again"}])
    offset = None
    last_remind = 0
    while True:
        res = call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    on_callback(u["callback_query"])
                elif "message" in u:
                    on_message(u["message"])
            except Exception as e:
                print(f"update {u.get('update_id')}: {e}")
        if not res.get("ok"):
            time.sleep(10)
        if time.time() - last_remind > 900:
            last_remind = time.time()
            try:
                reminders()
            except Exception as e:
                print(f"reminders: {e}")


if __name__ == "__main__":
    run()
