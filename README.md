# Housing finder

Watches rental sites around Eindhoven (15-20 km) for **self-contained studios up to €1100** and sends each new one to Telegram with photo, price, size and **the landlord's or agent's own phone number**.

Runs on your Mac every 5 minutes in the background (launchd), while the Mac is awake. Not in the cloud: Pararius and Huurwoningen block GitHub's servers (Cloudflare 403), but not a home connection.

## What counts

- Studios, plus apartments that are one room or at most 40 m² (studios under another name).
- No rooms in shared houses, no houses, no antikraak, no house swaps (ruilwoning.nl).

## Sites

| Site | Why |
|---|---|
| Pararius | Professional agents only, free to contact, biggest source |
| Huurwoningen | Same platform, extra listings copied from agent websites |
| 123Wonen | Agent network |
| ikwilhuren (MVGM) | Large landlord, register on their site |

Left out on purpose: Kamernet and Huurstunt (pay to react, private landlords, most scams), Rentola and Directwonen (paywalled copies). Not possible: Funda and Holland2Stay (bot checks), Vesteda and Interhouse (load by JavaScript).

## Landlord contact

- **Pararius:** the number in the listing's "contact the agent" box, plus the agent's own website.
- **Huurwoningen:** the landlord is hidden behind an account there, so the finder reads the agent's domain from the photo link and takes the phone number from that agent's own website.
- Portal numbers are never shown. No number found means: react via the listing.
- The same home on two sites is sent once (street + price), Pararius first because it has the number.

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
   The plist runs `.venv/bin/python main.py --once` every 300 s and logs to `logs/finder.log`.

The first run remembers what is online now and sends one "live" message. After that you only get new studios.

## Day to day

```bash
tail -f logs/finder.log                                                  # what it is doing
launchctl unload ~/Library/LaunchAgents/com.jericho.housing-finder.plist  # stop
launchctl load ~/Library/LaunchAgents/com.jericho.housing-finder.plist    # start
.venv/bin/python main.py --site Pararius                                  # what one site returns, and why things are skipped
```

- **A site breaks** (6 failed checks in a row): one ⚠️ message naming the site, one ✅ when it works again.
- **Settings:** budget is `MAX_PRICE` (env, default 1100), towns are `AREA` and the studio size cap is `SMALL_APARTMENT_M2` at the top of `main.py`.
- **Mac asleep:** no checks; it catches up on wake. Keep it plugged in and awake (e.g. Amphetamine) during the search for the fastest alerts.
