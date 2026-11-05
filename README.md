# Housing finder

Watches rental sites in Eindhoven and the towns around it for **self-contained studios up to €1100** and sends each new one to Telegram with photo, price, size, **the landlord's or agent's own phone number** and two buttons that copy a ready reaction (Dutch and English).

Runs on your Mac in the background (launchd): every minute at peak times (8-10, 12-13, 17-21), every 3 minutes otherwise, every 6 minutes at night. It can also react for you (auto-apply, below). Not in the cloud: Pararius and Huurwoningen block GitHub's servers (Cloudflare 403), but not a home connection.

Area: Eindhoven, Veldhoven, Best, Geldrop, Son en Breugel, Nuenen, Waalre (`AREA` and `TOWNS` in `main.py`).

## What counts

- Studios, plus apartments that are one room or at most 40 m² (studios under another name).
- No rooms in shared houses, no houses, no antikraak, no house swaps (ruilwoning.nl).

## Sites

| Site | Why |
|---|---|
| Pararius | Professional agents only, free to contact, biggest source (10 km radius) |
| Funda | Second biggest; agents often post here first (10 km radius) |
| Huurwoningen | Same platform as Pararius, extra listings copied from agent websites |
| 123Wonen | Agent network, per town |
| ikwilhuren (MVGM) | Large landlord, per town, register on their site |
| Vesteda | Large landlord, read from their own search API |
| HouseHunting | Agent's own site (lists some homes before Pararius) |
| Friendly Housing, Lightcity | Eindhoven agents' own sites, read with one generic parser (`agent_cards`); add more agents with a few lines |

Left out on purpose: Kamernet and Huurstunt (pay to react, private landlords, most scams), Rentola and Directwonen (paywalled copies), temporary-lease/antikraak landlords (Gapph, Ad-Hoc, Camelot and others). Not possible: Holland2Stay (Cloudflare stops even a real Chrome driven by a script; check it by hand), Interhouse (no listings in the page).

## Getting past blocks

Cloudflare on Pararius, Huurwoningen and Funda blocks some browser fingerprints and not others, and it changes. The finder tries several (Safari, Firefox, Chrome, Edge), remembers per site which one worked (`prefer` in `state.json`) and keeps cookies. If all are blocked it opens the page in your real Chrome, as a window placed off-screen (`.browser-profile/`), which passes. Turn that off with `BROWSER_FALLBACK=0` in `.env`.

## Landlord contact

- **Pararius:** the number in the listing's "contact the agent" box, plus the agent's own website.
- **Huurwoningen:** the landlord is hidden behind an account there, so the finder reads the agent's domain from the photo link and takes the phone number from that agent's own website.
- Portal numbers are never shown. No number found means: react via the listing.
- **Funda** hides the number; the finder reuses the number it learned for the same agent on Pararius.
- The same home on two sites is sent once (street + price), Pararius first because it has the number.

## Alerts

- 🔥 **NEW, CALL NOW**: the site was read fine at most 12 minutes earlier, so the listing went online minutes ago and there is a number. Call first, then send the reaction.
- 🕒 **found late**: the site was unreadable for a while, so the listing may already have many reactions.
- **📋 Copy reaction NL / Copy EN**: copies a short reaction filled in with the street and price. The text comes from `profile.json` (copy `profile.example.json`, fill in every TODO). Not in git, the repo is public. No buttons until it is filled in.
- A site added later is first learned silently, so you do not get everything already online.

## Setup (once)

1. Telegram bot: [@BotFather](https://t.me/BotFather) gives the token. Send the bot a message, then `https://api.telegram.org/bot<TOKEN>/getUpdates` shows your `chat.id`.
2. Put both in `.env` next to `main.py`:
   ```
   TELEGRAM_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
3. Install and start:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   launchctl load ~/Library/LaunchAgents/com.jericho.housing-finder.plist
   ```
   The plist runs `.venv/bin/python main.py --once` every 180 s and logs to `logs/finder.log`.
4. Keep the Mac awake on the charger: `launchctl load ~/Library/LaunchAgents/com.jericho.housing-finder.awake.plist` (runs `caffeinate -s`: no system sleep while plugged in; the screen still turns off). Closing the lid still sleeps the Mac.
5. Fill in `profile.json` for the reaction buttons. `python main.py --test-alert` sends a sample.

The first run remembers what is online now and sends one "live" message. After that you only get new studios.

## Day to day

```bash
tail -f logs/finder.log                                                  # what it is doing
launchctl unload ~/Library/LaunchAgents/com.jericho.housing-finder.plist  # stop
launchctl load ~/Library/LaunchAgents/com.jericho.housing-finder.plist    # start
.venv/bin/python main.py --site Pararius                                  # what one site returns, and why things are skipped
```

- **A site breaks** (8 failed checks in a row): one ⚠️ message naming the site, one ✅ when it works again.
- **Settings:** budget is `MAX_PRICE` (env, default 1100, set to 1250 in .env), the place filter is `AREA` and the studio size cap is `SMALL_APARTMENT_M2` at the top of `main.py`.
- **Mac asleep:** no checks; it catches up on wake and marks those listings 🕒. The awake agent prevents sleep on the charger, not with the lid closed or on battery.

## Auto-apply

`apply.py` reacts on a fresh listing in your name, in the bot's own Chrome profile, with the message from `profile.json`.

| `AUTO_APPLY` in `.env` | What happens |
|---|---|
| `rehearse` (default) | Fills in the form, sends you a screenshot, sends nothing |
| `live` | Fills in, sends, screenshots the confirmation to Telegram |
| `off` | Alerts only |

- **Sites:** Pararius and Funda (free account each; log in once with `.venv/bin/python main.py --login`; the login is kept across restarts). 123Wonen and HouseHunting show an "I'm not a robot" CAPTCHA, which the bot never solves: alert only. Huurwoningen (paid account), Vesteda and ikwilhuren (own flows): alert only.
- **Never:** twice on the same listing, more than `DAILY_CAP` (20) per day, on a listing with a 🚩 scam sign, when your income is below the listing's stated requirement, on 🕒 late listings, or anything that asks for payment.
- Every reaction is logged in `applications.json` and screenshotted in `logs/screens/`.
- Try one listing without sending: `.venv/bin/python main.py --rehearse <listing url>`.

## Telegram control (bot.py)

Runs all the time (`com.jericho.housing-finder.bot`). `/status` (sites, last check, today's numbers), `/apps` (open reactions and viewings), `/pause`, `/resume`. Buttons under each alert: ✅ Reacted, 📅 Viewing, ❌ No. After 48 hours without an answer it reminds you to call.

## Alert facts

Each alert shows what it could read from the listing: furnished / upholstered / bare, service costs, the income rule (e.g. "✅ income 3.5× rent = €3.360", checked against `income_gross_month` in your profile), 🚩 scam signs (Western Union, keys by post, landlord abroad, WhatsApp only, pay before viewing) and ⚠️ unusually cheap.
